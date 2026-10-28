#!/usr/bin/env python3
"""OJ 自动解题 & 题解发布脚本 — Hydro OJ 平台"""

import os
import sys
import re
import json
import time
import html
import base64
import logging
import threading
import argparse
import requests
from datetime import datetime
from pathlib import Path
from oj_common import (load_dotenv, create_session, save_cookies, load_cookies,
                        parse_problem_url, oj_login, smart_login,
                        accum_get, accum_add)
from config_manager import ConfigManager

log = logging.getLogger(__name__)
Config = ConfigManager

from ai_client import AIClient, DEFAULT_PROMPTS, _hour_in_range, _extract_tag_prefix


# ═══════════════════════════════════════════════════════════════
# OJClient — OJ API 交互（含 HTTP 重试）
# ═══════════════════════════════════════════════════════════════
class OJClient:
    CASE_STATUS = {0: "PENDING", 1: "AC", 2: "WA", 3: "TLE", 4: "MLE",
                   5: "RE", 6: "CE", 7: "SE", 8: "OLE", 9: "CANCELED"}

    def __init__(self, config: Config):
        self.root = config["oj_root"].rstrip("/")
        self.api_base = config["oj_base"].rstrip("/")
        self.config = config
        self.session = create_session(verify_ssl=False)
        self.logged_in = False
        jar_path = config.get("cookie_jar", "")
        if jar_path and load_cookies(self.session, jar_path):
            self.logged_in = True
        self.verify_timeout = config["verify_timeout"]

    # ---- 登录 ----
    def login(self, max_retries: int = 3) -> bool:
        if self.logged_in:
            # 验证 cookie 是否仍然有效
            try:
                r = self.session.get(f"{self.api_base}/p/1",
                                     headers={"Accept": "application/json"}, timeout=10)
                if r.status_code == 200:
                    log.info("[*] 已登录（cookie 复用）"); return True
                log.warning("[!] Cookie 已过期，重新登录")
                self.logged_in = False
            except Exception:
                log.warning("[!] Cookie 验证失败，重新登录")
                self.logged_in = False
        log.info("[*] 登录 %s ...", self.root)
        # 凭据优先级：环境变量（.env / 守护进程传递）> config.json。
        # config.json 的 username 常为占位空值，必须回退环境变量，
        # 否则 cookie 失效后子进程必然登录失败。
        username = os.environ.get("OJ_USERNAME") or self.config.get("username", "")
        password = os.environ.get("OJ_PASSWORD") or self.config.get("password", "")
        if not username or not password:
            log.error("[-] 未配置 OJ 凭据（检查 OJ_USERNAME/OJ_PASSWORD 环境变量或 config.json）")
            return False
        jar_path = self.config.get("cookie_jar", "") or ".oj_cookies.json"
        if smart_login(self.session, self.root, username, password, jar_path):
            log.info("[+] 登录成功"); self.logged_in = True; return True
        log.error("[-] 登录失败"); return False

    # ---- 获取题目 ----
    def get_problem(self, pid: str) -> dict | None:
        log.info("[*] 获取题目 #%s ...", pid)
        try:
            resp = self.session.get(f"{self.api_base}/p/{pid}",
                                    headers={"Accept": "application/json"}, timeout=15)
        except requests.RequestException as e:
            log.error("[-] 获取题目网络异常: %s", e); return None
        if resp.status_code != 200:
            log.error("[-] 获取失败，状态码 %d", resp.status_code); return None
        try:
            data = resp.json()
        except ValueError:
            log.error("[-] 题目接口返回的不是 JSON（可能需要登录，或域不存在）")
            return None
        pdoc = data.get("pdoc", {})
        content_raw = pdoc.get("content", ""); zh = ""
        # 题面图片：从 HTML/Markdown 提取 <img> / ![]() 并下载为 base64 data URL
        images = self._extract_images(content_raw, resp.url)
        if isinstance(content_raw, str):
            stripped = content_raw.strip()
            if stripped.startswith("{"):
                # 多语言题面：{"zh": "...", "en": "..."}
                try:
                    parsed = json.loads(stripped)
                    zh = parsed.get("zh", content_raw) if isinstance(parsed, dict) else content_raw
                except json.JSONDecodeError:
                    zh = content_raw
            elif stripped.startswith("<"):
                # HTML 题面 → 纯文本。用 html.unescape 覆盖全部实体，
                # 旧写法只处理 &[a-z]+;，&#39; 这类数字实体会原样进入提示词。
                zh = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", stripped,
                            flags=re.DOTALL | re.IGNORECASE)
                zh = re.sub(r'<[^>]+>', ' ', zh)
                zh = html.unescape(zh)
                zh = re.sub(r'\s+', ' ', zh).strip()
            else:
                zh = content_raw
        elif isinstance(content_raw, dict): zh = content_raw.get("zh", str(content_raw))
        title = pdoc.get("title", f"P{pid}")
        # 从 JSON pdoc 提取 tag/config 信息（优先），回退到 HTML
        pdoc_config = pdoc.get("config", {}) or {}
        time_limit = (f"{pdoc_config.get('timeMax', 0)}ms"
                      if pdoc_config.get("timeMax") else "")
        memory_limit = (f"{pdoc_config.get('memoryMax', 0)}MiB"
                        if pdoc_config.get("memoryMax") else "")
        tags = []
        if "tag" in pdoc and pdoc["tag"]:
            tags = pdoc["tag"] if isinstance(pdoc["tag"], list) else [pdoc["tag"]]
        # HTML 补充（仅在 JSON 数据缺失时回退）
        html_tags = self._fetch_tags(pid) if not time_limit or not memory_limit or not tags else {}
        log.info("[+] 题目: #%s %s, tags=%s", pid, title, tags or html_tags.get("tags", []))
        return {"pid": pid, "title": title, "content": zh,
                "tags": tags or html_tags.get("tags", []),
                "time_limit": time_limit or html_tags.get("time_limit", ""),
                "memory_limit": memory_limit or html_tags.get("memory_limit", ""),
                "io_method": html_tags.get("io_method", ""),
                "url": f"{self.api_base}/p/{pid}",
                "images": images}  # 题面图片 base64 data URL 列表（多模态模型使用）

    def _fetch_tags(self, pid: str) -> dict:
        """从网页 HTML 提取 problem__tags 中的有用信息。
        返回 {tags, time_limit, memory_limit, io_method}"""
        result = {"tags": [], "time_limit": "", "memory_limit": "", "io_method": ""}
        try:
            resp = self.session.get(f"{self.api_base}/p/{pid}", timeout=10)
            html = resp.text
            # 提取所有 tag-item
            items = re.findall(r'problem__tag-item[^>]*>([^<]+)', html)
            for item in items:
                item = item.strip()
                if not item: continue
                if item.startswith("ID:"): continue
                if re.match(r'^\d+ms$', item):
                    result["time_limit"] = item
                elif re.match(r'^\d+MiB$', item) or re.match(r'^\d+MB$', item):
                    result["memory_limit"] = item
                elif item.startswith("文件IO") or item.startswith("文件 IO"):
                    result["io_method"] = item
                else:
                    result["tags"].append(item)
        except Exception:
            pass
        return result

    # ---- 题面图片提取（多模态） ----
    def _extract_images(self, content_raw, page_url: str = "") -> list[str]:
        """从题面原始内容（HTML/Markdown）提取图片 URL，转绝对地址并下载为 base64。

        返回 list[str]：每个元素是 data:image/xxx;base64,<data> 或失败时保留原 URL。
        支持 <img src=...>、![alt](url)、[![alt](url)](link) 三类形式。
        """
        if isinstance(content_raw, dict):
            content_raw = content_raw.get("zh", "")
        if not isinstance(content_raw, str) or not content_raw:
            return []
        # 提取 <img src> 与 markdown 图片
        srcs = []
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', content_raw, re.I):
            srcs.append(m.group(1))
        for m in re.finditer(r'!\[[^\]]*\]\(([^)\s]+)', content_raw):
            if not m.group(1).startswith("<"):  # 排除 HTML 实体
                srcs.append(m.group(1))
        if not srcs:
            return []
        # 去重并转绝对地址
        seen, urls = set(), []
        for s in srcs:
            s = s.strip()
            if s.startswith("data:"):  # 已内嵌 base64
                urls.append(s); continue
            abs_u = self._abs_url(s, page_url)
            if abs_u and abs_u not in seen:
                seen.add(abs_u); urls.append(abs_u)
        if not urls:
            return []
        log.info("    [图片] 题面含 %d 张图: %s", len(urls), ", ".join(u[:60] for u in urls))
        # 下载为 base64 data URL（限制大小，失败保留原 URL 供 vision 模型直接拉取）
        max_images = int(self.config.get("max_problem_images", 4))
        return self._download_images(urls[:max_images])

    def _abs_url(self, url: str, page_url: str = "") -> str:
        """相对/绝对 URL → 绝对 URL（基于题目页或 OJ 根）"""
        if url.startswith("http://") or url.startswith("https://") or url.startswith("data:"):
            return url
        if url.startswith("//"):
            return "https:" + url
        base = page_url or f"{self.api_base}/p/"
        # page_url 形如 https://host/d/domain/p/123，目录取到 /d/domain/ 便于相对路径
        m = re.match(r'^(https?://[^/]+/d/[^/]+/)', base)
        prefix = m.group(1) if m else (page_url or self.api_base)
        return prefix.rstrip("/") + "/" + url.lstrip("/")

    def _download_images(self, urls: list[str]) -> list[str]:
        """下载图片为 base64 data URL。失败时保留原 URL（由 API 服务端抓取）。"""
        results = []
        for u in urls:
            if u.startswith("data:"):
                results.append(u); continue
            try:
                r = self.session.get(u, timeout=15)
                if r.status_code == 200:
                    ctype = r.headers.get("Content-Type", "image/png").split(";")[0]
                    if ctype.startswith("image/"):
                        b64 = base64.b64encode(r.content).decode()
                        results.append(f"data:{ctype};base64,{b64}")
                        log.info("    [图片] 下载成功 %s (%d bytes)", u, len(r.content))
                        continue
                log.warning("    [!] 图片下载失败 %d: %s", r.status_code, u)
            except Exception as e:
                log.warning("    [!] 图片下载异常 %s: %s", u, e)
            results.append(u)  # 失败保留原 URL
        return results

    # ---- 提交代码 ----
    def submit_code(self, pid: str, code: str, contest_id: str = "") -> str | None:
        log.info("[*] 提交代码到 #%s ...", pid)
        data = {"lang": self.config["lang"], "code": code}
        if contest_id:
            data["tid"] = contest_id
            log.info("    (关联比赛 %s)", contest_id[:12])
        try:
            resp = self.session.post(f"{self.api_base}/p/{pid}/submit",
                data=data, allow_redirects=False, timeout=15)
        except requests.RequestException as e:
            log.error("[-] 提交异常: %s", e); return None
        if resp.status_code in (302, 303):
            m = re.search(r"/record/([a-f0-9]+)", resp.headers.get("Location", ""))
            if m:
                log.info("[+] 提交成功，记录: %s/record/%s", self.root, m.group(1))
                return m.group(1)
        if resp.status_code == 429:
            log.warning("[-] 提交限流(429)，%ds 后重试", 3)
            time.sleep(3)
            try:
                resp = self.session.post(f"{self.api_base}/p/{pid}/submit",
                    data=data, allow_redirects=False, timeout=15)
                if resp.status_code in (302, 303):
                    m = re.search(r"/record/([a-f0-9]+)", resp.headers.get("Location", ""))
                    if m: return m.group(1)
            except requests.RequestException: pass
        log.error("[-] 提交失败，status=%d", resp.status_code); return None

    # ---- 验证评测 ----
    def verify_submission(self, rid: str) -> dict | None:
        log.info("[*] 等待评测完成 (rid=%s) ...", rid)
        url = f"{self.api_base}/record/{rid}"
        waited, delay = 0, 2
        rdoc = None
        while waited < self.verify_timeout:
            # 1) 网络请求
            try:
                resp = self.session.get(url, headers={"Accept": "application/json"}, timeout=10)
            except requests.RequestException as e:
                log.warning("    网络异常: %s，%ds 后重试", str(e)[:40], delay)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256)
                continue

            # 2) HTTP 状态码
            if resp.status_code == 429:
                time.sleep(3); waited += 3; continue
            if resp.status_code in (404, 500, 502, 503, 504):
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue
            if resp.status_code != 200:
                log.error("    未知状态码 %d", resp.status_code)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 3) 解析响应
            data = resp.json()
            rdoc = data.get("rdoc", {})
            st = rdoc.get("status", -1)
            cases = rdoc.get("testCases", [])

            # 4) 状态判断
            if st == -1:
                log.warning("    rdoc 中无 status 字段")
                time.sleep(delay); waited += delay; continue

            if not cases:
                # 无测试点数据 → 评测未完成或异常，继续等待
                if st in (0, 1):
                    log.info("    %s ... (已等 %ds)",
                             "排队中" if st == 0 else "评测中", waited)
                else:
                    log.info("    status=%d 无测试点，继续等待 ... (已等 %ds)", st, waited)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 有测试点数据 → 检查是否为终态
            # 终态判断：score>0 或 有时间/内存数据 或 有编译错误
            # 另加一条：所有测试点都已不在 PENDING(0) 状态 —— 某些评测机在
            # 全 RE/全 WA 且未记录耗时的情况下 score/time/memory 全为 0，
            # 只看前三项会一直等到超时，最终把已经拿到的评测结果丢掉。
            score = rdoc.get("score", 0)
            has_timing = rdoc.get("time", 0) > 0 or rdoc.get("memory", 0) > 0
            has_ce = bool(rdoc.get("compilerTexts", []))
            # 必须同时满足「整体状态不再是 PENDING(0)」和「所有测试点都有终态」，
            # 避免评测机逐个回填测试点时提前拿到不完整的结果。
            all_settled = st != 0 and all(c.get("status", 0) != 0 for c in cases)
            is_terminal = score > 0 or has_timing or has_ce or all_settled

            if not is_terminal:
                log.info("    评测中，等待最终结果 ... (已等 %ds, score=%d)", waited, score)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 终态
            break

        if waited >= self.verify_timeout or rdoc is None:
            log.error("[-] 评测超时（%ds 未完成）", self.verify_timeout); return None

        # 5) 校验数据完整性
        cases = rdoc.get("testCases", [])
        if not cases:
            log.warning("    无测试点数据！rdoc keys=%s score=%s status=%s",
                        list(rdoc.keys())[:10], rdoc.get("score"), rdoc.get("status"))

        return self._parse_verdict(rdoc)

    def _parse_verdict(self, rdoc: dict) -> dict:
        score, time_ms, memory_kb = rdoc.get("score", 0), rdoc.get("time", 0), rdoc.get("memory", 0)
        cases = rdoc.get("testCases", [])
        compiler_text = rdoc.get("compilerTexts", [])
        judge_text = rdoc.get("judgeTexts", [])

        # 构建 subtask 索引
        subtasks = rdoc.get("subtasks", [])
        subtask_map = {}
        if isinstance(subtasks, list):
            for st in subtasks:
                if isinstance(st, dict):
                    subtask_map[st.get("id", st.get("_id"))] = st

        ac_count, failures_by_type, fail_details = 0, {}, []
        subtask_stats = {}
        for c in cases:
            s, cid = c.get("status", 0), c.get("id", "?")
            t, mem = c.get("time", 0), c.get("memory", 0)
            label = self.CASE_STATUS.get(s, f"ERR({s})")
            st_id = c.get("subtaskId")
            if st_id is not None:
                st = subtask_stats.setdefault(st_id, {"ac": 0, "fail": 0, "score": 0, "label": ""})
                st["label"] = subtask_map.get(st_id, {}).get("title", f"子任务{st_id}")
                if s == 1: st["ac"] += 1
                else: st["fail"] += 1
                st["score"] += c.get("score", 0)
            if s == 1: ac_count += 1
            else:
                failures_by_type.setdefault(label, []).append(cid)
                msg = c.get("message", "")
                t_safe = float(t or 0); mem_safe = float(mem or 0)
                fail_details.append(f"  #{cid}: {label} | 耗时 {t_safe:.0f}ms | 内存 {mem_safe:.0f}KB"
                                    + (f" — {msg}" if msg else ""))

        case_summary = " ".join(self.CASE_STATUS.get(c.get("status", 0), "?") for c in cases)
        ac_rate = f"{ac_count}/{len(cases)}" if cases else "0/0"
        log.info("[+] 评测完成 — 得分: %d, AC: %s", score, ac_rate)
        log.info("    总耗时: %.0fms, 内存: %.0fKB", time_ms, memory_kb)
        log.info("    各测试点: %s", case_summary)
        for d in fail_details: log.info(d)

        errors_lines = []
        if compiler_text:
            ct = compiler_text if isinstance(compiler_text, str) else (compiler_text[0] if compiler_text else "")
            errors_lines.append(f"编译错误: {str(ct)[:600]}")
            log.info("    编译信息: %s", str(ct)[:300])
        if fail_details:
            errors_lines.append(f"通过率: {ac_rate} ({ac_count}AC / {len(cases)}总)")
            # 子任务汇总
            if len(subtask_stats) > 1:
                st_lines = []
                for st_id in sorted(subtask_stats):
                    st = subtask_stats[st_id]
                    st_lines.append(f"  {st['label']}: AC {st['ac']}/{(st['ac']+st['fail'])}, 得分 {st['score']}")
                if st_lines:
                    errors_lines.append("子任务汇总:"); errors_lines.extend(st_lines)
            for label, case_ids in failures_by_type.items():
                errors_lines.append(f"{label}: 共{len(case_ids)}个 — 用例 {', '.join(str(x) for x in case_ids)}")
            errors_lines.append("详细:"); errors_lines.extend(fail_details)
            hints = []
            if "TLE" in failures_by_type: hints.append("时间超限 — 需优化算法复杂度或剪枝")
            if "WA" in failures_by_type: hints.append("答案错误 — 检查边界条件、特殊情况和输出格式")
            if "RE" in failures_by_type: hints.append("运行错误 — 检查数组越界、空指针、除零等")
            if "MLE" in failures_by_type: hints.append("内存超限 — 需优化内存使用")
            if "CE" in failures_by_type: hints.append("编译错误 — 检查语法和头文件")
            if hints: errors_lines.append("分析提示: " + "; ".join(hints))
        if judge_text:
            jt = judge_text
            if isinstance(jt, list):
                jt = "; ".join(j if isinstance(j, str) else j.get("text", str(j)) for j in jt)
            errors_lines.append(f"评测机信息: {str(jt)[:500]}")

        all_ac = (cases and all(c.get("status", 0) == 1 for c in cases)
                  and (score > 0 or time_ms > 0))  # 排除虚假 AC (score=0,time=0)
        # 检测评测机故障：
        # - SE(7)/CANCELED(9): 时间内存全0 → 故障
        # - TLE(3): time=0 → 故障（超时应有时长）
        # - MLE(4): memory=0 → 故障（超内存应有内存占用）
        is_sys_err = False
        if cases and not all_ac:
            failed = [c for c in cases if c.get("status", 0) not in (0, 1)]
            if failed and all(c.get("status", 0) in {7, 9} and c.get("time", 0) == 0 and c.get("memory", 0) == 0 for c in failed):
                is_sys_err = True
            elif any(c.get("status", 0) == 3 and c.get("time", 0) == 0 for c in failed):
                is_sys_err = True  # TLE 但无耗时
            elif any(c.get("status", 0) == 4 and c.get("memory", 0) == 0 for c in failed):
                is_sys_err = True  # MLE 但无内存占用
        if is_sys_err:
            log.warning("    ⚠️ 疑似评测机故障：失败测试点数据异常")
            errors_lines.insert(0, "[系统疑似故障] 测试点数据异常(SE/CANCELED/TLE无耗时/MLE无内存)，可能为评测机错误")

        return {
            "score": score, "time_ms": time_ms, "memory_kb": memory_kb,
            "cases": cases, "case_summary": case_summary, "ac_rate": ac_rate,
            "failures_by_type": failures_by_type, "fail_details": fail_details,
            "errors_text": "\n".join(errors_lines),
            "is_ac": all_ac,
            "is_system_error": is_sys_err,
            "compiler_text": compiler_text, "judge_text": judge_text,
        }

    # ---- 发布题解 ----
    def post_solution(self, pid: str, solution_md: str) -> str | None:
        log.info("[*] 发布题解到 P%s 题解区 ...", pid)
        try:
            resp = self.session.post(f"{self.api_base}/p/{pid}/solution",
                json={"operation": "submit", "content": solution_md},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15)
        except requests.RequestException as e:
            log.error("[-] 发布题解网络异常: %s", e); return None
        if resp.status_code == 200:
            data = resp.json(); psid = data.get("psid", "")
            if psid:
                log.info("[+] 题解发布成功，psid=%s", psid)
                log.info("    题解页面: %s/p/%s/solution", self.api_base, pid)
                return psid
        log.error("[-] 题解发布失败，status=%d, body=%s", resp.status_code, resp.text[:200]); return None


# ═══════════════════════════════════════════════════════════════
# SolverOrchestrator — 编排完整流程
# ═══════════════════════════════════════════════════════════════
class SolverOrchestrator:
    def __init__(self, oj: OJClient, ai: AIClient, config: Config):
        self.oj = oj; self.ai = ai; self.config = config

    def solve(self, pid: str, *, submit: bool = True, post: bool = True,
              max_retries: int = 3, use_stream: bool = False, contest_id: str = "",
              outer_retries: int = 3, accumulate: bool = True):
        """一键解题。
        Phase1: flash 快速尝试(不修正) → Phase2: 难度判断
        → Phase3: 三层循环 (外层升级模型 × 中层换思路2次 × 内层修正2次)
        """
        log.info("\n" + "=" * 50 + f"\n  OJ Auto Solver — #{pid}\n" + "=" * 50)

        if not self.oj.login():
            self._notify_error(f"❌ #{pid} 登录失败，请检查 OJ 凭据")
            return
        problem = self.oj.get_problem(pid)
        if not problem:
            self._notify_error(f"❌ #{pid} 获取题目失败，可能无权限或链接错误")
            return

        total_usage, total_elapsed, total_cost = {}, 0.0, 0.0
        all_rids, all_verdicts = [], []
        final_verdict = None
        code, solution_md = "", ""
        is_ac = False
        is_cost_capped = False  # 费用超上限
        accum_enabled = accumulate and self.config.get("cost_accum_enable", True)
        accum_base = accum_get(pid) if accum_enabled else 0.0  # 历史累计（不含本次会话）
        difficulty = 0  # 0=未判断, 1-8 (8级制)
        candidate_tags = []  # 初步筛选的候选标签（难度评定，供 AI 参考，不出现在题解中）
        final_tags = []      # AI 最终选定的标签（二次筛选，出现在题解中）
        journal: list = []   # 求解过程记录（失败原因/换过哪些思路），写题解时作为素材
        MID_RETRIES = 2   # 中层默认：重试解题次数（flash 层仅 1 次）
        INNER_RETRIES = 2  # 内层：按错误修正次数

        # 多模型路由
        from model_router import ModelRouter, ModelTier
        router = ModelRouter(config_manager=self.config)
        route_model = router.default.model
        route_thinking = ""
        # 实际产出当前代码的模型（banner/footer 用它，而不是全局默认模型）
        used_model = self.config["ai_model"]
        used_effort = ""

        # 题面含图 → 优先多模态视觉模型（纯文本模型无法理解图片）
        has_image = bool(problem.get("images"))
        vision_model = self.config.get_vision_model() if has_image else ""
        if has_image and vision_model:
            log.info("[多模态] 题面含 %d 张图，视觉模型 %s 可用", len(problem["images"]), vision_model)

        # ═══ Agent 模式：本地编译 + 样例 + 对拍后再提交 ═══
        agent = self._build_agent(pid, contest_id, accum_enabled, accum_base,
                                  lambda: total_cost)

        # ═══ Phase 0: 免费模型快速尝试（不修正） ═══
        free_model = "glm-4.6v-flash"
        fallback_model = "deepseek-v4-flash"  # free 限流时切换到 flash
        # 题面含图：free 尝试直接用视觉模型（优先 deepseek 多模态）
        if has_image and vision_model:
            free_model = vision_model
            fallback_model = vision_model
        has_free = free_model in (self.config.cfg.models if self.config.cfg.models else {})
        if has_free:
            route_model = free_model
            route_thinking = ""
            log.info("[路由] Phase0 free: %s", route_model)
        log.debug("[路由] free_model=%s fallback=%s has_free=%s",
                  free_model, fallback_model, has_free)

        # agent 模式下跳过这次「盲提交」：本地验证会把同类问题挡在前面，
        # 白交一份代码只会浪费提交次数与评测时间。
        if agent is None:
            # flash 专属提示词：快速、直击核心
            tpl = self.ai._p("generate_flash", "") or DEFAULT_PROMPTS.get("generate_flash", "")
            if not tpl:
                tpl = self.ai._p("generate_easy", "") or DEFAULT_PROMPTS.get("generate_easy", "")
            ext = self.ai.config.lang_ext
            prompt = tpl.format(title=problem['title'],
                info=f"时限 {problem.get('time_limit','?')} | 内存 {problem.get('memory_limit','?')}",
                content=problem['content'], time_limit=problem.get('time_limit','?'),
                memory_limit=problem.get('memory_limit','?'),
            io_hint=problem.get('io_method') and f"IO方式: {problem['io_method']}" or '使用标准输入输出',
            ext=ext)
            result = self.ai._call_ai(prompt, self.ai.SYS_SOLVE_EASY, use_stream=use_stream,
                                      images=problem.get("images") or [],
                                      model=route_model, effort=route_thinking)
            # free 限流 → 自动切换 flash
            if result and result.get("_rate_limited"):
                log.info("[*] free 模型限流，切换 %s 重试", fallback_model)
                route_model, route_thinking = fallback_model, "high"
                result = self.ai._call_ai(prompt, self.ai.SYS_SOLVE_EASY, use_stream=use_stream,
                                          images=problem.get("images") or [],
                                          model=route_model, effort=route_thinking)
            if result and result.get("code"):
                code = result["code"]; solution_md = result["solution_md"]
                # 解析 AI 终选的标签
                parsed_tags, solution_md = _extract_tag_prefix(solution_md)
                if parsed_tags:
                    candidate_tags = parsed_tags
                fu = result.get("usage", {})
                for k in ("input", "output", "total", "cache_hit"):
                    total_usage[k] = total_usage.get(k, 0) + fu.get(k, 0)
                total_elapsed += result.get("elapsed_s", 0)
                total_cost += result.get("cost", 0)
                used_model = result.get("model", route_model)
                used_effort = route_thinking
                banner = self._code_banner_for(used_model, usage=fu,
                                               cost=result.get("cost", 0), effort=used_effort)
                if submit:
                    rid = self.oj.submit_code(pid, banner + code)
                    all_rids.append(rid)
                    if rid:
                        verdict = self.oj.verify_submission(rid)
                        all_verdicts.append(verdict)
                        final_verdict = verdict
                        if verdict and verdict.get("is_ac"):
                            is_ac = True
                            log.info("[+] Phase0 AC! 用时 %.0fms, 内存 %.0fKB",
                                     verdict["time_ms"], verdict["memory_kb"])

        # ═══ Phase 1: 难度判断（可用 difficulty_detect_enable 关闭） ═══
        detect_enabled = bool(self.config.get("difficulty_detect_enable", True))
        skip_model = (self.config.get("difficulty_skip_model") or "").strip()
        if not detect_enabled:
            # 跳过难度评估这一次 AI 调用，直接用配置的模型（或分层默认）开始求解
            if skip_model:
                log.info("[路由] 已关闭难度判断，使用专用模型: %s", skip_model)
            else:
                log.info("[路由] 已关闭难度判断，未配置 difficulty_skip_model，"
                         "沿用分层默认: %s", router.default.model)
        else:
            diff_model = free_model if has_free else self.config["ai_model"]
            log.info("[路由] Phase1 难度判断 (模型=%s) ...", diff_model)
            diff_prompt = router.DIFFICULTY_PROMPT.format(content=problem.get("content","")[:3000])
            diff_result = self.ai._call_ai(diff_prompt, "你是一个题目难度评估专家。仅回复数字。",
                                           use_stream=False, images=problem.get("images") or [],
                                           model=diff_model)
            if diff_result and diff_result.get("_rate_limited") and has_free:
                log.info("[*] free 模型限流，切换 %s 判断难度", fallback_model)
                diff_model = fallback_model
                diff_result = self.ai._call_ai(diff_prompt, "你是一个题目难度评估专家。仅回复数字。",
                                               use_stream=False, images=problem.get("images") or [],
                                               model=diff_model, effort="high")
            if diff_result:
                difficulty, parsed_tags = ModelRouter.parse_diff_and_tags(diff_result.get("raw", ""))
                if not candidate_tags:
                    candidate_tags = parsed_tags
            log.info("[路由] 难度判定: %d 级, 标签: %s", difficulty, candidate_tags)

        # ═══ Phase 2: 三层循环 ═══
        # 外层：升级模型 | 中层：重新解题(换思路) | 内层：按错误修正
        # 起始层级由 ModelRouter 统一决定（1-2→flash, 3-5→pro, 6-8→max）
        custom_tier = None
        if not detect_enabled and skip_model:
            found = router.tier_index_for_model(skip_model)
            if found is None:
                # 指定的模型不在 model_router 分层里 → 只用它解题，不做分层升级
                custom_tier = ModelTier("custom", skip_model, [])
                log.info("[路由] %s 不在 model_router 分层中，本次仅使用该模型", skip_model)
            else:
                custom_tier = None
                tier_start = router.clamp_index(found)
        else:
            tier_start = router.clamp_index(router.tier_index_for_difficulty(difficulty))

        if custom_tier is not None:
            tier_plan = [(0, custom_tier)]
        else:
            tier_plan = [(i, router.tiers[i]) for i in range(tier_start, len(router.tiers))]

        for tier_idx, tier in tier_plan:
            if is_ac: break
            # 题面含图 → 所有层优先视觉模型（纯文本 pro/max 无法理解图片）
            if has_image and vision_model:
                route_model = vision_model
                log.info("[多模态] 含图题目，层 %s 改用视觉模型 %s", tier.name, vision_model)
            else:
                route_model = tier.model
            # flash 层（索引 0）中层仅 1 次，其他层 2 次；专用模型层按 2 次。
            # agent 模式自带「实现→本地验证→修正」的内循环，每层只跑一轮，
            # 避免外层层级与内层步数相乘导致费用失控。
            if agent is not None:
                mid_retries = 1
            else:
                mid_retries = 1 if (tier_idx == 0 and custom_tier is None) else MID_RETRIES

            for mid in range(mid_retries):
                if is_ac: break
                if self._is_cost_capped(accum_enabled, accum_base, total_cost):
                    is_cost_capped = True; break
                route_thinking = tier.current_thinking(mid)
                log.info("[三层] 模型=%s 中层=%d/%d 难度=%d",
                         route_model, mid + 1, mid_retries, difficulty)

                # ── agent 分支：方案 → 实现 → 本地编译/样例/对拍 → 提交 ──
                if agent is not None:
                    outcome = agent.attempt(
                        problem, model=route_model, effort=route_thinking,
                        difficulty=difficulty, candidate_tags=candidate_tags,
                        submit=submit, use_stream=use_stream, contest_id=contest_id)
                    journal.extend(outcome.ideas and
                                   [f"思路：{x}" for x in outcome.ideas] or [])
                    journal.extend(outcome.journal)
                    for k in ("input", "output", "total", "cache_hit"):
                        total_usage[k] = total_usage.get(k, 0) + outcome.usage.get(k, 0)
                    total_cost += outcome.cost
                    total_elapsed += outcome.elapsed
                    all_rids.extend(outcome.rids)
                    all_verdicts.extend(outcome.verdicts)
                    if outcome.verdict:
                        final_verdict = outcome.verdict
                    if outcome.code:
                        code = outcome.code
                        solution_md = outcome.solution_md
                        parsed_tags, solution_md = _extract_tag_prefix(solution_md)
                        if parsed_tags:
                            final_tags = parsed_tags
                        used_model, used_effort = outcome.model, outcome.effort
                        banner = self._code_banner_for(used_model, usage=outcome.usage,
                                                       cost=outcome.cost, effort=used_effort)
                    if outcome.is_ac:
                        is_ac = True
                        break
                    if outcome.is_cost_capped:
                        is_cost_capped = True
                        break
                    continue

                result = self.ai.generate(problem, use_stream=use_stream, difficulty=difficulty,
                                          candidate_tags=candidate_tags,
                                          model=route_model, effort=route_thinking)
                if result and result.get("_rate_limited"):
                    # 降级模型: max→pro, pro→flash；含图时保持视觉模型（降级也沿用 vision）
                    prev_tier = tier_idx - 1
                    # 专用模型层没有「上一层」，回退到分层默认（最便宜的一层），
                    # 否则限流后这一层会直接失败，整个求解就此中断。
                    if prev_tier >= 0:
                        fb_tier = router.tiers[prev_tier]
                    elif custom_tier is not None and router.tiers:
                        fb_tier = router.tiers[0]
                    else:
                        fb_tier = None
                    if has_image and vision_model:
                        log.warning("[!] %s 限流，含图题目保持视觉模型 %s 重试",
                                    tier.name, vision_model)
                        route_model = vision_model
                        route_thinking = tier.current_thinking(mid)
                        result = self.ai.generate(problem, use_stream=use_stream, difficulty=difficulty,
                                          candidate_tags=candidate_tags,
                                          model=route_model, effort=route_thinking)
                    elif fb_tier is not None:
                        log.warning("[!] %s 限流，降级到 %s 重试", tier.name, fb_tier.model)
                        route_model = fb_tier.model
                        route_thinking = fb_tier.current_thinking(mid)
                        result = self.ai.generate(problem, use_stream=use_stream, difficulty=difficulty,
                                          candidate_tags=candidate_tags,
                                          model=route_model, effort=route_thinking)
                    else:
                        log.warning("[!] 已经是底层模型，无备选")
                if not result or not result.get("code"):
                    log.warning("[-] AI 未生成代码"); break
                code = result["code"]; solution_md = result["solution_md"]
                # 解析 AI 从候选标签中最终选定的标签（二次筛选）
                parsed_tags, solution_md = _extract_tag_prefix(solution_md)
                if parsed_tags:
                    final_tags = parsed_tags
                fu = result.get("usage", {})
                for k in ("input", "output", "total", "cache_hit"):
                    total_usage[k] = total_usage.get(k, 0) + fu.get(k, 0)
                total_elapsed += result.get("elapsed_s", 0)
                total_cost += result.get("cost", 0)
                used_model = result.get("model", route_model)
                used_effort = route_thinking
                banner = self._code_banner_for(used_model, usage=fu,
                                               cost=result.get("cost", 0), effort=used_effort)

                history = [
                    {"role": "system", "content": self.ai.SYS_SOLVE},
                    {"role": "user", "content": f"请解决这道题：\n{problem['content']}"},
                    {"role": "assistant", "content": solution_md},
                ]

                for fix_i in range(INNER_RETRIES + 1):
                    if not submit or not code: break
                    rid = self.oj.submit_code(pid, banner + code)
                    all_rids.append(rid)
                    if not rid: break
                    verdict = self.oj.verify_submission(rid)
                    all_verdicts.append(verdict)
                    if not verdict: break
                    final_verdict = verdict
                    if verdict.get("is_ac"):
                        is_ac = True
                        log.info("[+] AC! 用时 %.0fms, 内存 %.0fKB",
                                 verdict["time_ms"], verdict["memory_kb"]); break
                    if fix_i >= INNER_RETRIES:
                        log.warning("[-] 内层修正 %d 次未 AC，得分 %d",
                                    INNER_RETRIES, verdict["score"]); break
                    # 费用上限检查
                    if self._is_cost_capped(accum_enabled, accum_base, total_cost):
                        is_cost_capped = True; break
                    if verdict.get("is_system_error"):
                        log.warning("[!] 疑似评测机故障，直接重试提交")
                        time.sleep(3); continue
                    log.info("\n[*] 得分 %d，第 %d 次修正 (中层%d/%d 内层%d/%d) ...",
                             verdict["score"], fix_i + 1,
                             mid + 1, MID_RETRIES, fix_i + 1, INNER_RETRIES)
                    fix = self.ai.fix(problem, code, solution_md, verdict, fix_i + 1,
                                      use_stream=use_stream, history=history, difficulty=difficulty,
                                      model=route_model, effort=route_thinking)
                    if not fix: log.warning("[-] 修正失败"); break
                    code = fix["code"]; solution_md = fix["solution_md"]
                    # 解析修正后 AI 最终选定的标签（二次筛选）
                    parsed_tags, solution_md = _extract_tag_prefix(solution_md)
                    if parsed_tags:
                        final_tags = parsed_tags
                    history.append({"role": "user", "content": f"评测: 得分{verdict['score']}, {verdict.get('case_summary','')}"})
                    history.append({"role": "assistant", "content": solution_md})
                    fu = fix.get("usage", {})
                    for k in ("input", "output", "total", "cache_hit"):
                        total_usage[k] = total_usage.get(k, 0) + fu.get(k, 0)
                    total_elapsed += fix.get("elapsed_s", 0)
                    total_cost += fix.get("cost", 0)
                    if fix.get("model"):
                        used_model = fix["model"]

        retry_count = max(0, len(all_verdicts) - 1); outer_count = retry_count; psid = None

        if not is_ac:
            log.warning("[-] 未 AC，跳过发布题解（仅满分才发布）"); post = False

        # 代码混淆（仅 AC 后、发布题解前）
        obf_code = None
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                obf_config = json.load(f).get("code_obfuscate", {})
        except Exception:
            obf_config = {}
        if is_ac and code and obf_config.get("enabled"):
            # 用免费模型做混淆（显式传参，不改动全局配置）
            log.info("[*] 调用 AI 混淆代码 (免费模型: %s) ...", free_model)
            obf = self.ai.obfuscate(code, model=free_model, effort="")
            if obf and obf.get("_rate_limited") and has_free:
                log.info("[*] free 模型限流，切换 %s 混淆", fallback_model)
                obf = self.ai.obfuscate(code, model=fallback_model, effort="high")
            if obf and obf.get("code"):
                obf_code = obf["code"]
                log.info("[+] 混淆完成，%d 字符 → %d 字符", len(code), len(obf_code))
                # 替换题解中的代码块为混淆版本，解题内容不变
                ext = self.config.lang_ext
                solution_md = re.sub(
                    rf"```(?:{ext}|c\+\+|c)\s*\n.+?```",
                    f"```{ext}\n{obf_code}\n```",
                    solution_md, count=1, flags=re.DOTALL)
            else:
                log.warning("[-] 混淆失败，使用原始代码")

        # ═══ 题解产出：AC 之后单独写一篇结构化题解 ═══
        # 求解输出里混着试错过程与口语，不适合直接发布；这里以「已通过评测的代码」
        # 为准重写题解，并在发布前做结构与代码一致性校验。
        if post and is_ac and code:
            solution_md = self._write_editorial(
                problem, code, final_verdict, difficulty, candidate_tags,
                solution_md, used_model, used_effort, journal,
                time_limit=problem.get("time_limit", ""))

        if post and solution_md:
            # 插入难度标签
            if difficulty > 0:
                header = ModelRouter.difficulty_tag(difficulty)
                if candidate_tags:
                    header += "\n" + ModelRouter.tags_tag(candidate_tags)
                solution_md = header + "\n\n" + solution_md
            footer = self._build_footer(total_elapsed, total_usage, retry_count, total_cost,
                                        used_model, used_effort)
            psid = self.oj.post_solution(pid, solution_md + footer)

        # 混淆后重新提交到原题/比赛
        if obf_code:
            if obf_config.get("resubmit_problem"):
                log.info("[*] 提交混淆代码到原题 ...")
                rid2 = self.oj.submit_code(pid, banner + obf_code)
                if rid2: log.info("[+] 混淆提交成功: %s/record/%s", self.oj.root, rid2)
            if obf_config.get("resubmit_contest") and contest_id:
                log.info("[*] 提交混淆代码到比赛 ...")
                self.oj.submit_code(pid, banner + obf_code, contest_id=contest_id)

        # 比赛同步递交（未混淆但 AC 时）
        if contest_id and is_ac and code and not obf_code:
            log.info("[*] 向比赛 %s 同步递交 ...", contest_id[:12])
            crid = self.oj.submit_code(pid, banner + code, contest_id=contest_id)
            if crid:
                log.info("[+] 比赛递交成功: %s/record/%s", self.oj.root, crid)

        log.info("\n" + "=" * 50 + f"\n  完成!\n  题目: {problem['title']}\n  链接: {problem['url']}")
        if all_rids: log.info("  评测: %s/record/%s", self.oj.root, all_rids[-1])
        if final_verdict:
            ac = "✓ AC" if is_ac else f"得分 {final_verdict['score']}"
            log.info("  结果: %s | 耗时: %.0fms | 内存: %.0fKB", ac, final_verdict["time_ms"], final_verdict["memory_kb"])
        if psid: log.info("  题解: %s/p/%s/solution", self.oj.api_base, pid)
        log.info("=" * 50)

        # 写入 dashboard 记录
        self._record_result(problem, is_ac, final_verdict, total_usage, total_elapsed,
                            total_cost, retry_count)

        # 私信通知结果
        self._notify_solve_result(problem, is_ac, final_verdict, total_usage, total_cost)

        # 单题累计费用写回（本次会话费用累加到历史）
        if accum_enabled and total_cost > 0:
            accum_add(pid, total_cost)

        return {"final_verdict": final_verdict, "psid": psid,
                "retry_count": retry_count, "outer_count": retry_count,
                "total_usage": total_usage, "total_cost": total_cost,
                "total_elapsed": total_elapsed, "is_ac": is_ac, "pid": pid,
                "model": used_model,
                "is_cost_capped": is_cost_capped}

    def _write_editorial(self, problem: dict, code: str, verdict: dict | None,
                         difficulty: int, tags: list, fallback_md: str,
                         used_model: str, used_effort: str, journal: list,
                         time_limit: str = "") -> str:
        """AC 之后重写题解：结构固定、代码与 AC 版本逐字一致。

        与原行为的关系：求解时模型的输出（含试错过程）只作为兜底；
        写题解失败时仍然照旧发布它，不会因为新流程失败而丢失题解。
        """
        if not self.config.get("editorial_enable", True):
            return fallback_md
        try:
            from editorial import EditorialWriter
        except ImportError as e:
            log.warning("[题解] 模块加载失败，使用求解时的原始输出: %s", e)
            return fallback_md
        model = (self.config.get("editorial_model") or "").strip() or used_model
        effort = used_effort if model == used_model else ""
        log.info("[题解] 生成结构化题解（模型=%s）...", model or self.config["ai_model"])
        try:
            md = EditorialWriter(self.ai, self.config).write(
                problem, code, verdict=verdict, difficulty=difficulty, tags=tags,
                journal=journal, model=model, effort=effort, fallback=fallback_md)
        except Exception as e:
            log.warning("[题解] 生成异常，使用求解时的原始输出: %s", e)
            return fallback_md
        return md or fallback_md

    def _build_agent(self, pid: str, contest_id: str, accum_enabled: bool,
                     accum_base: float, session_cost):
        """按配置装配 agent 求解器；不可用时返回 None（走原三层循环）。

        agent 的核心是「本地工具」：编译、跑样例、随机对拍。缺少工具链
        （例如没装 g++）时不做任何降级猜测，直接回到原来的流程。
        """
        if not self.config.get("agent_enabled", True):
            log.info("[Agent] 配置已关闭（agent_enabled=false），使用原三层循环")
            return None
        try:
            from local_judge import LocalJudge
            from solver_agent import SolverAgent
        except ImportError as e:                       # 模块缺失时不影响求解
            log.warning("[Agent] 模块加载失败: %s", e)
            return None
        try:
            judge = LocalJudge(self.config.lang_ext,
                               # 带 pid 后缀：多个进程同时解同一道题时互不覆盖编译产物
                               workdir=os.path.join("agent_work", f"p{pid}_{os.getpid()}"),
                               run_timeout=self.config.get("agent_run_timeout", 10) or 10)
        except Exception as e:
            log.warning("[Agent] 本地评测器初始化失败: %s", e)
            return None
        if not judge.available:
            log.warning("[Agent] 未检测到 %s 本地工具链，回退原三层循环"
                        "（安装 g++ 后自动启用本地验证）", self.config.lang_ext)
            return None
        log.info("[Agent] 本地验证已启用 | %s", judge.describe())
        return SolverAgent(
            self.ai, self.config, judge,
            submit_fn=lambda code: self.oj.submit_code(pid, code, contest_id=contest_id),
            judge_fn=self.oj.verify_submission,
            banner_fn=lambda model, effort, usage, cost: self._code_banner_for(
                model, usage=usage, cost=cost, effort=effort),
            cost_exceeded=lambda: self._is_cost_capped(
                accum_enabled, accum_base, session_cost()))

    def _is_cost_capped(self, enabled: bool, accum_base: float, session_cost: float) -> bool:
        """单题费用是否已达上限（累计历史 + 本次会话）。

        原先在求解循环的两处各写一遍这段判断，容易漏改一边；
        统一到这里，并附带一条 WARNING 日志。
        """
        if not enabled:
            return False
        cap = self.config.get("max_cost_per_problem", 5.0)
        try:
            cap = float(cap)
        except (TypeError, ValueError):
            return False
        total = accum_base + session_cost
        if total < cap:
            return False
        log.warning("[-] 费用已达上限 ¥%.4f (累计¥%.4f+本次¥%.4f, cap=¥%.2f)，标记为费用超限",
                    total, accum_base, session_cost, cap)
        return True

    def _notify_error(self, text: str):
        """求解失败时通知请求者（OJ_REQUESTER），避免静默失败无回复。"""
        try:
            requester = int(os.environ.get("OJ_REQUESTER", 0))
            if requester <= 0:
                return
            from oj_common import push_oj_message
            push_oj_message(self.oj.session, self.oj.root, text, push_uids=[requester])
        except Exception:
            pass

    def _notify_solve_result(self, problem: dict, is_ac: bool, verdict: dict | None,
                             usage: dict, cost: float):
        """通过私信通知求解结果"""
        try:
            uids_str = os.environ.get("OJ_PUSH_LIST", "")
            uids = [int(x.strip()) for x in uids_str.split(",") if x.strip().isdigit()]
            requester = int(os.environ.get("OJ_REQUESTER", 0))
            if requester > 0:
                uids.append(requester)
            if not uids:
                return
            from oj_common import push_oj_message
            pid = str(problem.get("pid", "?"))
            title = problem.get("title", "")[:20]
            emoji = "✅" if is_ac else "❌"
            score = verdict.get("score", 0) if verdict else 0
            parts = [f"{emoji} #{pid} {title}"]
            parts.append(f"  结果: {'AC' if is_ac else '得分'+str(score)} | Token: {usage.get('input',0)}i/{usage.get('output',0)}o")
            if cost > 0:
                parts.append(f"  费用: ¥{cost:.4f}")
            push_oj_message(self.oj.session, self.oj.root, "\n".join(parts), push_uids=uids)
        except Exception:
            pass

    def _record_result(self, problem: dict, is_ac: bool, verdict: dict | None,
                       usage: dict, elapsed: float, cost: float, retries: int):
        """追加解题记录到 dashboard.json（使用共享工具函数）"""
        from oj_common import append_dashboard_record
        record = {
            "pid": str(problem.get("pid", "?")),
            "title": problem.get("title", "")[:30],
            "status": "ac" if is_ac else "fail",
            "score": verdict.get("score", 0) if verdict else 0,
            "time_ms": verdict.get("time_ms", 0) if verdict else 0,
            "memory_kb": verdict.get("memory_kb", 0) if verdict else 0,
            "tokens_in": usage.get("input", 0),
            "tokens_out": usage.get("output", 0),
            "cache_hit": usage.get("cache_hit", 0),
            "cost": cost,
            "retries": retries,
            "elapsed_s": elapsed,
            "finished_at": datetime.now().strftime("%m-%d %H:%M:%S"),
        }
        append_dashboard_record(record)

    def _comment_prefix(self) -> str:
        """根据语言返回注释前缀"""
        ext = self.config.lang_ext
        return "#" if ext == "python" else "//"

    def _code_banner(self, usage: dict | None = None, cost: float = 0) -> str:
        """生成代码头部注释"""
        return self._code_banner_for(self.config["ai_model"], usage, cost)

    def _code_banner_for(self, model: str, usage: dict | None = None,
                         cost: float = 0, effort: str = "") -> str:
        """生成代码头部注释，显式指定模型（不再读取全局可变配置）"""
        c = self._comment_prefix()
        re_val = effort or self.config.get_model_reasoning_effort(model)
        banner = (
            f"{c} 模型: {model}  "
            f"| 语言: {self.config['lang']}  "
            f"| 推理强度: {re_val}\n"
            f"{c} 由 OJ Auto Solver 自动生成\n"
        )
        if usage:
            banner += (f"{c} Token: {usage.get('input',0)}i/{usage.get('output',0)}o/"
                       f"{usage.get('total',0)}t  ")
            if usage.get("cache_hit"):
                banner += f"| 缓存命中: {usage['cache_hit']} "
            if cost > 0:
                banner += f"| 费用: ¥{cost:.4f}"
            banner += "\n"
        return banner + "\n"

    def _build_footer(self, elapsed: float, usage: dict, retry_count: int, cost: float = 0,
                      model: str = "", effort: str = "") -> str:
        model = model or self.config["ai_model"]
        re_val = effort or self.config.get_model_reasoning_effort(model)
        lines = [
            f"\n\n---\n",
            f"> 本题解由 **{model}** 生成 "
            f"| 代码语言: **{self.config['lang']}**",
            f"> 推理强度: **{re_val}** "
            f"| 总耗时: {elapsed:.1f}s",
        ]
        if usage:
            lines.append(f"> Token 用量: {usage.get('input',0)} in / {usage.get('output',0)} out / "
                         f"{usage.get('total',0)} total"
                         + (f" (缓存命中 {usage['cache_hit']})" if usage.get("cache_hit") else ""))
        if cost > 0:
            lines.append(f"> 预估费用: ¥{cost:.4f}")
        if retry_count > 0:
            lines.append(f"> 修正次数: {retry_count}")
        return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def build_cli_overrides(args, parsed_root: str = "", parsed_api_base: str = "") -> dict:
    """把命令行参数转换成 ConfigManager 的覆盖字典。

    单独抽成函数有两个原因：一是可被单元测试直接覆盖，二是修复 `--api-key-env`
    一直失效的问题（旧实现写成 ai_api_key_env，ConfigManager 不认识这个字段，
    只会静默打一条「未知配置项」告警，导致命令行传入的密钥被忽略）。
    """
    cli: dict = {}
    for key in ("username", "password", "lang"):
        val = getattr(args, key, None)
        if val:
            cli[key] = val
    # --api-key-env 指定的是 API Key（可以是 sk-xxx 字面值，也可以是环境变量名）
    if getattr(args, "api_key_env", None):
        cli["ai_api_key"] = args.api_key_env
    if getattr(args, "base_url", None):
        cli["ai_base_url"] = args.base_url
    if getattr(args, "model", None):
        cli["ai_model"] = args.model
    if getattr(args, "base", None):
        cli["oj_base"] = args.base
    if parsed_root:
        cli["oj_root"] = parsed_root
        cli["oj_base"] = parsed_api_base
    if getattr(args, "cookie_jar", None):
        cli["cookie_jar"] = args.cookie_jar
    if getattr(args, "timeout", None):
        cli["verify_timeout"] = args.timeout
    # 难度判断随时可切换：--no-difficulty-detect 关闭，--difficulty-skip-model
    # 指定关闭时使用的模型（传了专用模型即视为要关闭，避免"配了模型却没生效"）
    if getattr(args, "difficulty_skip_model", None):
        cli["difficulty_skip_model"] = args.difficulty_skip_model
        cli["difficulty_detect_enable"] = False
    if getattr(args, "no_difficulty_detect", False):
        cli["difficulty_detect_enable"] = False
    if getattr(args, "no_agent", False):
        cli["agent_enabled"] = False
    if getattr(args, "agent_steps", None):
        cli["agent_max_steps"] = args.agent_steps
    if getattr(args, "no_stress", False):
        cli["agent_stress_enable"] = False
    cli["show_thinking"] = bool(getattr(args, "show_thinking", False))
    return cli


def main():
    load_dotenv()
    from oj_common import setup_logging
    parser = argparse.ArgumentParser(description="OJ Auto Solver — 自动解题并发布 Markdown 题解")
    parser.add_argument("pid", help="题目 ID 或完整链接")
    parser.add_argument("--base", help="OJ API 地址")
    parser.add_argument("--username", help="用户名")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--api-key-env", help="AI API Key")
    parser.add_argument("--base-url", help="AI API 地址")
    parser.add_argument("--model", help="AI 模型名")
    parser.add_argument("--lang", help="提交语言 ID")
    parser.add_argument("--timeout", type=int, help="评测超时（秒）")
    parser.add_argument("--ai-only", action="store_true", help="仅 AI 生成解答")
    parser.add_argument("--no-submit", action="store_true", help="不提交代码")
    parser.add_argument("--no-post", action="store_true", help="不发布题解")
    parser.add_argument("--dry-run", action="store_true", help="仅抓取题目内容")
    parser.add_argument("--stream", action="store_true", help="AI 流式输出")
    parser.add_argument("--no-difficulty-detect", action="store_true",
                        help="跳过难度判断（少一次 AI 调用），配合 --difficulty-skip-model 指定所用模型")
    parser.add_argument("--difficulty-skip-model", metavar="MODEL",
                        help="关闭难度判断时使用的模型（默认沿用分层首个模型）")
    parser.add_argument("--no-agent", action="store_true",
                        help="关闭 agent 求解（不做本地编译/样例/对拍，直接提交）")
    parser.add_argument("--agent-steps", type=int, metavar="N",
                        help="agent 模式下单题最多几轮生成/修正（默认 6）")
    parser.add_argument("--no-stress", action="store_true",
                        help="agent 模式下跳随机对拍（仍做本地编译与样例检查）")
    parser.add_argument("--show-thinking", action="store_true", help="显示 AI 思考过程（默认关闭）")
    parser.add_argument("--no-show-thinking", action="store_true", help=argparse.SUPPRESS)  # 兼容子进程调用
    parser.add_argument("--quiet", action="store_true", help="减少输出")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    parser.add_argument("--log-file", help="日志文件路径（支持 {date} 占位符，如 logs/oj_{date}.log）")
    parser.add_argument("--cookie-jar", help="Cookie jar 文件路径")
    parser.add_argument("--code", help="提交已有代码文件")
    parser.add_argument("--solution", help="发布已有题解文件")
    args = parser.parse_args()

    # 日志系统
    setup_logging(quiet=args.quiet, verbose=args.verbose,
                  log_file=args.log_file, name="oj_solver")

    parsed_root, parsed_api_base, parsed_pid = parse_problem_url(args.pid)
    args.pid = parsed_pid
    if parsed_root:
        log.info("[*] 目标题目: %s/p/%s", parsed_api_base, parsed_pid)

    cli = build_cli_overrides(args, parsed_root, parsed_api_base)

    config = Config(cli_overrides=cli)
    config.repair()  # 自动补齐缺失配置
    oj = OJClient(config)
    ai = AIClient(config)
    solver = SolverOrchestrator(oj, ai, config)

    if args.dry_run:
        if not oj.login(): return
        prob = oj.get_problem(args.pid)
        if prob:
            log.info("\n" + "=" * 50 + f"\n题目: {prob['title']}\n" + "=" * 50 + f"\n{prob['content']}")
        return

    if args.ai_only:
        if not oj.login(): return
        prob = oj.get_problem(args.pid)
        if prob:
            result = ai.generate(prob, use_stream=args.stream)
            if result:
                log.info("\n" + "=" * 50 + "\n" + result["solution_md"] + "\n--- code ---\n" + result["code"])
        return

    if args.code or args.solution:
        if not oj.login(): return
        if args.code:
            try: code = Path(args.code).read_text(encoding="utf-8"); oj.submit_code(args.pid, code)
            except (FileNotFoundError, UnicodeDecodeError, OSError) as e: log.error("[-] 读取失败: %s", e)
        if args.solution:
            try: md = Path(args.solution).read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError, OSError) as e: md = None; log.error("[-] 读取失败: %s", e)
            if md:
                footer = solver._build_footer(0, {}, 0)
                oj.post_solution(args.pid, md + footer)
        return

    try:
        solver.solve(args.pid, submit=not args.no_submit, post=not args.no_post,
                     use_stream=args.stream, accumulate=False)
    except Exception as e:
        log.error("[!] 求解异常: %s", e)
        requester = int(os.environ.get("OJ_REQUESTER", 0))
        if requester > 0:
            try:
                from oj_common import push_oj_message
                push_oj_message(oj.session, oj.root, f"❌ 求解异常: {str(e)[:100]}",
                                push_uids=[requester])
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
