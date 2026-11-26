"""HydroOJ 登录、题面读取、提交与评测接口。"""
import base64
import html
import json
import logging
import re
import time

import requests

from hydroai.config.manager import ConfigManager
from hydroai.oj.common import create_session, load_cookies, smart_login

log = logging.getLogger(__name__)


class OJClient:
    CASE_STATUS = {0: "PENDING", 1: "AC", 2: "WA", 3: "TLE", 4: "MLE",
                   5: "RE", 6: "CE", 7: "SE", 8: "OLE", 9: "CANCELED"}

    def __init__(self, config: ConfigManager):
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
        # ConfigManager 已解析命令行、环境与 JSON 的优先级。
        username, password = self.config.username, self.config.password
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

        返回 list[str]：每个元素是 data:image/xxx;base64,<data>；下载失败的图片会被
        丢弃，避免把不可访问的 URL 交给模型导致 400。
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
        """相对/绝对 URL → 绝对 URL（基于题目页或 OJ 根）。

        Hydro 题面里的 ``file://name`` 是文件占位符，真实地址形如
        ``https://host/p/<pid>/file/<name>?type=additional_file``。
        """
        if url.startswith("http://") or url.startswith("https://") or url.startswith("data:"):
            return url
        if url.startswith("file://"):
            name = url[len("file://"):].lstrip("/").split("?", 1)[0]
            origin = re.match(r'^(https?://[^/]+)', page_url or self.api_base)
            pid = re.search(r'/p/([^/?#]+)', page_url or "")
            if name and origin and pid:
                return (f"{origin.group(1)}/p/{pid.group(1)}/file/{name}"
                        "?type=additional_file")
            return ""
        if url.startswith("//"):
            return "https:" + url
        base = page_url or f"{self.api_base}/p/"
        # page_url 形如 https://host/d/domain/p/123，目录取到 /d/domain/ 便于相对路径
        m = re.match(r'^(https?://[^/]+/d/[^/]+/)', base)
        prefix = m.group(1) if m else (page_url or self.api_base)
        return prefix.rstrip("/") + "/" + url.lstrip("/")

    def _download_images(self, urls: list[str]) -> list[str]:
        """下载图片为 base64 data URL；失败则跳过，避免无效 URL 导致模型 400。"""
        results = []
        for u in urls:
            if not u:
                continue
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
            # 不保留原始 URL：不可访问的图片会让多模态请求直接 400。
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
                log.info("[+] 提交成功，记录: %s/record/%s", self.api_base, m.group(1))
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
