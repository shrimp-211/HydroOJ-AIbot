#!/usr/bin/env python3
"""
标程用户题解自动生成 — 从比赛排行榜检测标程用户满分题，
获取其提交代码，AI 解读后发布为标准题解。
"""

import os, re, sys, json, time, logging, argparse, tempfile, shutil, threading
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from oj_common import (load_dotenv, create_session, oj_login, load_config,
                       parse_contest_or_problem, parse_problem_url)

log = logging.getLogger("benchmark_solver")

DEFAULT_MODEL = os.environ.get("OJ_BENCHMARK_MODEL", "deepseek-v4-pro")


class BenchmarkSolver:
    """检测标程用户 AC 题，获取代码并 AI 解读发布题解"""

    def __init__(self, model: str = None):
        load_dotenv()
        self.root = os.environ.get("OJ_ROOT", "https://oj.yuanyicode.com")
        self.username = os.environ.get("OJ_USERNAME", "")
        self.password = os.environ.get("OJ_PASSWORD", "")
        self.model = model or DEFAULT_MODEL
        self.session = create_session(verify_ssl=False)
        self._oj_sem = threading.Semaphore(4)  # OJ 请求限流
        cfg = load_config()
        self.benchmark_users = set(str(x) for x in cfg.get("benchmark_users", [2]))
        self.processed = self._load_processed()

    def login(self) -> bool:
        return oj_login(self.session, self.root, self.username, self.password)

    # ── 持久化 ──
    def _load_processed(self) -> set:
        try:
            with open("benchmark_processed.json", "r") as f:
                raw = json.load(f)
            result = set()
            for x in raw:
                t = tuple(x)
                if len(t) == 2:
                    result.add((t[0], "system", t[1]))  # 旧格式: (uid, pid) → 默认 system 域
                elif len(t) >= 3:
                    result.add((t[0], t[1], t[2]))      # 新格式: (uid, domain, pid)
            return result
        except Exception:
            return set()

    def _save_processed(self):
        try:
            tmp = "benchmark_processed.json.tmp"
            with open(tmp, "w") as f:
                json.dump([list(x) for x in self.processed], f)
            Path(tmp).replace("benchmark_processed.json")
        except Exception:
            pass

    # ── 获取比赛排行榜 ──
    def get_scoreboard(self, domain: str, cid: str) -> dict | None:
        try:
            with self._oj_sem:
                r = self.session.get(
                    f"{self.root}/d/{domain}/contest/{cid}/scoreboard",
                    headers={"Accept": "application/json"}, timeout=15)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            log.warning("获取排行榜失败 %s/%s: %s", domain, cid[:12], e)
            return None

    # ── 扫描标程用户满分题 ──
    def scan_benchmark_ac(self, domain: str, cid: str) -> list[dict]:
        """扫描排行榜中标程用户的满分题。
        返回: [{uid, pid, record_id, score}, ...]
        """
        sb = self.get_scoreboard(domain, cid)
        if not sb:
            return []
        rows = sb.get("rows", [])
        if len(rows) < 2:
            return []

        header = rows[0]
        problem_cols = []
        for j, cell in enumerate(header):
            if isinstance(cell, dict) and cell.get("type") == "problem":
                problem_cols.append((j, cell.get("raw")))

        results = []
        for row in rows[1:]:
            if not isinstance(row, list):
                continue
            uid = str(row[1].get("raw", "")) if len(row) > 1 else ""
            if uid not in self.benchmark_users:
                continue
            for j, pid in problem_cols:
                if j >= len(row):
                    continue
                cell = row[j]
                if not isinstance(cell, dict):
                    continue
                score_val = str(cell.get("value", ""))
                if score_val != "100":
                    continue
                record_id = cell.get("raw", "")
                if not record_id:
                    continue
                # 处理相对链接: /d/domain/record/xxx → 提取纯 ID
                if "/" in str(record_id):
                    m = re.search(r"/record/([a-f0-9]+)", str(record_id))
                    record_id = m.group(1) if m else record_id
                key = (uid, domain, str(pid))
                if key not in self.processed:
                    results.append({
                        "uid": uid, "pid": str(pid),
                        "record_id": record_id, "score": 100,
                        "domain": domain,
                    })
        return results

    # ── 获取提交代码 ──
    def get_submission_code(self, record_id: str, domain: str = "system") -> dict | None:
        """获取提交记录的代码和题目信息。
        非 system 域需要 /d/{domain}/record/{id} 路径。
        """
        if "/" in record_id:
            m = re.search(r"/record/([a-f0-9]+)", record_id)
            record_id = m.group(1) if m else record_id
        try:
            with self._oj_sem:
                url = f"{self.root}/record/{record_id}" if domain == "system" else f"{self.root}/d/{domain}/record/{record_id}"
                r = self.session.get(url, headers={"Accept": "application/json"}, timeout=15)
            if r.status_code != 200:
                return None
            rdoc = r.json().get("rdoc", {})
            return {
                "code": rdoc.get("code", ""),
                "lang": rdoc.get("lang", "cc.cc14o2"),
                "pid": str(rdoc.get("pid", "?")),
                "uid": rdoc.get("uid", 0),
                "score": rdoc.get("score", 0),
                "status": rdoc.get("status", -1),
            }
        except Exception as e:
            log.warning("获取提交代码失败 %s: %s", record_id[:12], e)
            return None

    # ── 检查是否已有该用户的题解 ──
    def is_processed(self, uid: str, domain: str, pid: str) -> bool:
        """检查 (uid, domain, pid) 是否已处理过。"""
        return (uid, domain, pid) in self.processed

    def has_benchmark_solution(self, domain: str, pid: str, uid: str) -> bool:
        """检查题解中是否已有该标程用户的题解。
        严格匹配: 代码由 ... @[](/user/{uid}) ... 编写
        """
        pat = re.compile(
            r'代码由.*?@\[\]\(/user/' + re.escape(uid) + r'\).*?编写',
            re.DOTALL
        )
        try:
            with self._oj_sem:
                r = self.session.get(
                    f"{self.root}/d/{domain}/p/{pid}/solution",
                    headers={"Accept": "application/json"}, timeout=15,
                    params={"page": 1, "limit": 50})
            if r.status_code != 200:
                return False
            for psdoc in r.json().get("psdocs", []):
                if pat.search(psdoc.get("content", "")):
                    return True
        except Exception:
            pass
        return False

    # ── AI 解读代码 ──
    def interpret_code(self, problem: dict, code: str, author_uid: str) -> dict | None:
        """用 AI 解读标程用户代码，生成题解。
        返回: {solution_md, usage, cost, elapsed_s} 或 None
        """
        from openai import OpenAI
        config = load_config()
        base_url = config.get("ai_base_url", "https://api.deepseek.com")
        api_key = os.environ.get("AI_API_KEY", "")
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0, max_retries=2)

        tags = problem.get("tags", [])
        tags_str = ", ".join(tags) if tags else "无"
        limit_info = f"时限: {problem.get('time_limit', '?')} | 内存: {problem.get('memory_limit', '?')}"

        log.info("[*] AI 解读代码 (模型=%s) ...", self.model)
        t_start = time.monotonic()
        prompt = (
            f"## 题目信息\n"
            f"- 标题: 【{problem.get('title', '?')}】\n"
            f"- 标签: {tags_str}\n"
            f"- {limit_info}\n\n"
            f"## 题面\n{problem.get('content', '')[:3000]}\n\n"
            f"## 标程代码（由用户 @{author_uid} 编写）\n```cpp\n{code[:8000]}\n```\n\n"
            "## 任务\n请以「算法讲师」的身份，解读这份标程代码：\n"
            "1. 结合题目标签和题面，分析代码的算法思路\n"
            "2. 解释关键实现细节和时间/空间复杂度\n"
            "3. 说明代码中的巧妙之处和值得学习的技巧\n\n"
            "## 输出格式\n"
            "## 解题思路\n（算法分析 + 核心思路 + 标签关联 + 关键细节）\n\n"
            "## 代码解释\n（分段解释代码的关键部分）\n\n"
            "## 技巧总结\n（这份代码值得学习的点）\n\n"
            "## 代码\n```cpp\n{完整代码}\n```"
        )
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": "你是算法讲师，擅长解读代码并讲解算法。"},
                      {"role": "user", "content": prompt}],
            max_tokens=16384)
        elapsed = time.monotonic() - t_start
        content = resp.choices[0].message.content or ""
        if not content:
            log.warning("[-] AI 返回空内容"); return None

        # 提取 token 用量
        usage = {}
        u = resp.usage
        if u:
            usage = {
                "input": getattr(u, "prompt_tokens", 0),
                "output": getattr(u, "completion_tokens", 0),
                "total": getattr(u, "total_tokens", 0),
            }

        # 计算费用（从 models.{name}.pricing 读取，兼容旧 model_pricing）
        cost = 0.0
        try:
            cfg = load_config()
            models = cfg.get("models", {})
            md = models.get(self.model, {})
            pricing = md.get("pricing", {}) or cfg.get("model_pricing", {}).get(self.model, {})
            if pricing:
                inp = pricing.get("input", 0)
                out = pricing.get("output", 0)
                cost = (usage.get("input", 0) * inp + usage.get("output", 0) * out) / 1_000_000
        except Exception:
            pass

        log.info("[+] AI 解读完成 (%d 字符, Token %.0fi/%.0fo, 耗时 %.1fs, ¥%.4f)",
                 len(content), usage.get("input", 0), usage.get("output", 0),
                 elapsed, cost)
        # 难度判断 + 标签筛选
        difficulty, tags = 3, []
        try:
            from model_router import ModelRouter
            diff_prompt = ModelRouter.DIFFICULTY_PROMPT.format(content=problem.get("content", "")[:3000])
            diff_resp = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": "输出难度和标签。"},
                          {"role": "user", "content": diff_prompt}],
                max_tokens=128)
            difficulty, tags = ModelRouter.parse_diff_and_tags(diff_resp.choices[0].message.content or "")
        except Exception:
            pass
        from model_router import ModelRouter
        header = ModelRouter.difficulty_tag(difficulty)
        if tags:
            header += "\n" + ModelRouter.tags_tag(tags)
        content = header + "\n\n" + content

        return {"solution_md": content, "usage": usage, "cost": cost,
                "elapsed_s": elapsed, "difficulty": difficulty}

    # ── 获取题目信息 ──
    def get_problem(self, domain: str, pid: str) -> dict | None:
        try:
            r = self.session.get(
                f"{self.root}/d/{domain}/p/{pid}",
                headers={"Accept": "application/json"}, timeout=15)
            if r.status_code != 200:
                return None
            pdoc = r.json().get("pdoc", {})
            content = pdoc.get("content", "")
            if isinstance(content, dict):
                content = content.get("zh", str(content))
            config = pdoc.get("config", {}) or {}
            return {
                "title": pdoc.get("title", f"P{pid}"),
                "content": str(content),
                "pid": pid,
                "tags": pdoc.get("tag", []),
                "time_limit": f"{config.get('timeMax','?')}ms",
                "memory_limit": f"{config.get('memoryMax','?')}MiB",
            }
        except Exception as e:
            log.warning("获取题目失败 %s/%s: %s", domain, pid, e)
            return None

    # ── 发布题解 ──
    def post_solution(self, domain: str, pid: str, content: str) -> str | None:
        try:
            with self._oj_sem:
                r = self.session.post(
                f"{self.root}/d/{domain}/p/{pid}/solution",
                json={"operation": "submit", "content": content},
                headers={"Accept": "application/json"}, timeout=15)
            if r.status_code == 200:
                psid = r.json().get("psid", "")
                return psid
            log.warning("发布题解失败: status=%d", r.status_code)
        except Exception as e:
            log.warning("发布题解异常: %s", e)
        return None

    def _build_footer(self, model: str, author_uid: str, usage: dict = None,
                      cost: float = 0, elapsed: float = 0) -> str:
        """构建带有代码来源标注和统计信息的页脚"""
        lines = [
            f"\n\n---\n",
            f"> 代码由 @[](/user/{author_uid}) 编写\n",
            f"> 代码解释由 **{model}** 自动生成 | 总耗时: {elapsed:.1f}s",
        ]
        if usage:
            tinfo = f"> Token 用量: {usage.get('input', 0)} in / {usage.get('output', 0)} out"
            if usage.get('total'):
                tinfo += f" / {usage['total']} total"
            lines.append(tinfo)
        if cost > 0:
            lines.append(f"> 预估费用: ¥{cost:.4f}")
        return "\n".join(lines) + "\n"

    # ── 通知推送 ──
    def _push_notify(self, event: str, text: str):
        """推送通知到配置的推送名单"""
        try:
            pe_str = os.environ.get("OJ_PUSH_EVENTS", "{}")
            pe = json.loads(pe_str) if pe_str else {}
            if not pe.get(event, True):
                return
            uids_str = os.environ.get("OJ_PUSH_LIST", "")
            uids = [int(x.strip()) for x in uids_str.split(",") if x.strip().isdigit()]
            if not uids:
                return
            from oj_common import push_oj_message
            ts = datetime.now().strftime("%m-%d %H:%M:%S")
            push_oj_message(self.session, self.root, f"[{ts}] {text}", push_uids=uids)
        except Exception:
            pass

    # ── 处理单个 AC 记录 ──
    def process_record(self, rec: dict, force: bool = False) -> bool:
        """处理单个标程用户 AC 记录。
        force=True 时忽略已处理记录，强制执行。
        """
        uid, pid = rec["uid"], rec["pid"]
        domain = rec.get("domain", "system")
        log.info("处理: uid=%s pid=%s rid=%s%s", uid, pid, rec["record_id"][:12],
                 " [强制]" if force else "")

        if not force and self.is_processed(uid, domain, pid):
            log.info("  [-] 已处理（本地记录），跳过")
            self._push_notify("benchmark_skip",
                f"⏭️ 标程跳过: P{pid} (用户{uid}) — 已处理")
            return False

        # 检查题解中是否已有该用户的标程题解
        if not force and self.has_benchmark_solution(domain, pid, uid):
            log.info("  [-] 已有该用户标程题解，标记为已处理")
            self.processed.add((uid, domain, pid))
            self._save_processed()
            self._push_notify("benchmark_skip",
                f"⏭️ 标程跳过: P{pid} (用户{uid}) — 题解中已有引用")
            return False

        # 获取代码
        sub = self.get_submission_code(rec["record_id"], domain=domain)
        if not sub or not sub.get("code"):
            log.warning("  [-] 无法获取代码"); return False
        if sub.get("score", 0) < 100:
            log.warning("  [-] 非满分提交"); return False

        # 获取题目
        problem = self.get_problem(domain, pid)
        if not problem:
            log.warning("  [-] 无法获取题目"); return False

        # AI 解读
        result = self.interpret_code(problem, sub["code"], uid)
        if not result:
            return False
        solution = result["solution_md"]

        # 添加页脚
        solution += self._build_footer(self.model, uid,
                                       usage=result.get("usage"),
                                       cost=result.get("cost", 0),
                                       elapsed=result.get("elapsed_s", 0))

        # 发布
        psid = self.post_solution(domain, pid, solution)
        if psid:
            log.info("  [+] 题解已发布: %s/d/%s/p/%s/solution",
                     self.root, domain, pid)
            self.processed.add((uid, domain, pid))
            self._save_processed()
            self._push_notify("benchmark_done",
                f"📝 标程题解: P{pid} (用户{uid}) — {problem.get('title','')[:20]}")
            return True
        return False

    # ── 处理单个提交链接（手动命令）──
    def process_record_url(self, record_url: str) -> bool:
        """根据提交链接处理"""
        m = re.search(r"/record/([a-f0-9]+)", record_url)
        if not m:
            log.error("[-] 无法解析提交链接"); return False
        rid = m.group(1)
        # 尝试从 URL 提取域名
        dm = re.search(r"/d/([^/]+)/record/", record_url)
        domain = dm.group(1) if dm else "system"
        sub = self.get_submission_code(rid, domain=domain)
        if not sub:
            log.error("[-] 无法获取提交"); return False
        if sub.get("score", 0) < 100:
            log.warning("[-] 非满分提交 (score=%d)", sub.get("score", 0)); return False

        uid, pid = str(sub["uid"]), str(sub["pid"])
        # 从记录 URL 中提取域名
        dm = re.search(r"/d/([^/]+)/record/", record_url)
        domain = dm.group(1) if dm else "system"
        rec = {"uid": uid, "pid": pid, "record_id": rid, "domain": domain}
        return self.process_record(rec, force=True)

    # ── 扫描全部比赛 ──
    def scan_all_contests(self, domains: list[str] = None):
        """扫描所有比赛的标程用户满分题（多线程并发处理）"""
        if domains is None:
            cfg = load_config()
            domains = cfg.get("monitor_domains", ["system", "yuanyi__contestForPrimary"])
        self._push_notify("benchmark_start", f"🔍 开始扫描标程用户: {', '.join(domains)}")

        # 收集所有待处理记录
        all_records = []
        for domain in domains:
            log.info("[*] 扫描 domain=%s ...", domain)
            try:
                r = self.session.get(
                    f"{self.root}/d/{domain}/contest",
                    headers={"Accept": "application/json"}, timeout=15,
                    params={"page": 1, "limit": 50})
                if r.status_code != 200:
                    continue
                for tdoc in r.json().get("tdocs", []):
                    cid = tdoc["_id"]
                    log.info("  比赛: %s (%s)", tdoc.get("title", cid[:12])[:30], cid[:12])
                    records = self.scan_benchmark_ac(domain, cid)
                    log.info("    发现 %d 个标程满分", len(records))
                    all_records.extend(records)
            except Exception as e:
                log.warning("扫描异常 domain=%s: %s", domain, e)

        if not all_records:
            log.info("[*] 无标程满分记录"); return 0

        # 多线程并发处理
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock
        lock = Lock()
        total = 0
        workers = min(len(all_records), 4)
        log.info("[*] 共 %d 个记录，%d 线程并发处理", len(all_records), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.process_record, rec): rec for rec in all_records}
            for f in as_completed(futures):
                try:
                    if f.result():
                        with lock:
                            total += 1
                except Exception as e:
                    log.warning("处理异常: %s", e)
        log.info("[*] 扫描完成，共处理 %d 题", total)
        return total


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════
def main():
    load_dotenv()
    from oj_common import setup_logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(message)s",
                        datefmt="%m-%d %H:%M:%S")

    parser = argparse.ArgumentParser(description="标程用户题解自动生成")
    parser.add_argument("target", nargs="?", help="提交记录链接 或 比赛链接（无参数则扫描全部比赛）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="AI 模型")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    elif args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    bs = BenchmarkSolver(model=args.model)
    if not bs.login():
        log.error("[-] 登录失败"); sys.exit(1)

    if args.target:
        if "/record/" in args.target:
            ok = bs.process_record_url(args.target)
        else:
            info = parse_contest_or_problem(args.target)
            domain = info.get("domain_id", "system")
            cid = info.get("contest_id")
            if not cid:
                log.error("[-] 仅支持提交链接或比赛链接"); sys.exit(1)
            records = bs.scan_benchmark_ac(domain, cid)
            log.info("[*] 发现 %d 个标程满分，%d 线程并发处理", len(records), min(len(records), 4))
            if records:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(max_workers=min(len(records), 4)) as pool:
                    futures = {pool.submit(bs.process_record, r, True): r for r in records}
                    ok = any(f.result() for f in as_completed(futures))
            else:
                ok = False
    else:
        ok = bs.scan_all_contests() > 0

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
