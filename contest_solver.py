#!/usr/bin/env python3
"""比赛批量解题 — 多线程并行求解，支持多比赛同时处理。"""

import os, re, sys, json, time, logging, argparse, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════
# 限流器 — 同服务请求至少间隔 min_interval 秒
# ═══════════════════════════════════════════════
class RateLimiter:
    def __init__(self, min_interval: float = 2.0):
        self._min = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            gap = self._min - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

# 全局限流器实例（延迟模式时启用）
_oj_limiter = RateLimiter(2.0)
_ai_limiter = RateLimiter(2.0)
_delay_mode = False

from oj_common import (load_dotenv, create_session, parse_contest_or_problem,
                        parse_problem_url, fetch_user_id, oj_login)
from config_manager import ConfigManager

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

_push_session = None
_push_lock = threading.Lock()

def _push(text: str, to_uid: int = 0, event: str = ""):
    """推送消息。使用共享推送函数。线程安全。"""
    global _push_session
    if event:
        try:
            pe = json.loads(os.environ.get("OJ_PUSH_EVENTS", "{}"))
            if not pe.get(event, True): return
        except Exception: pass
    try:
        from oj_common import create_session, oj_login, push_oj_message
        with _push_lock:
            if _push_session is None:
                _push_session = create_session(verify_ssl=False)
                root = os.environ.get("OJ_ROOT", "https://oj.yuanyicode.com")
                if not oj_login(_push_session, root,
                                os.environ.get("OJ_USERNAME",""),
                                os.environ.get("OJ_PASSWORD","")):
                    _push_session = None; return
        root = os.environ.get("OJ_ROOT", "https://oj.yuanyicode.com")
        requester = int(os.environ.get("OJ_REQUESTER", 0))
        # 构建推送列表
        pids_raw = os.environ.get("OJ_PUSH_LIST", "")
        push_uids = [int(x.strip()) for x in pids_raw.split(",") if x.strip().isdigit()]
        push_oj_message(_push_session, root, text,
                        push_uids=push_uids, requester=to_uid or requester)
    except Exception as e:
        log.debug("[!] push failed: %s", e)


def has_existing_solution(session, base_url, domain_id, pid, uid):
    try:
        r = session.get(f"{base_url}/d/{domain_id}/p/{pid}/solution",
                        headers={"Accept": "application/json"}, timeout=15)
        if r.status_code != 200: return False
        for doc in r.json().get("psdocs", []):
            if doc.get("owner") == uid: return True
    except Exception as e:
        log.debug("[!] 检查已有题解异常: %s", e)
    return False


def attend_contest(session, base_url, domain_id, contest_id):
    try:
        r = session.post(f"{base_url}/d/{domain_id}/contest/{contest_id}",
                         json={"operation": "attend"},
                         headers={"Accept": "application/json"}, timeout=15)
        return r.status_code == 200
    except Exception: return False


def _has_testdata(session, base_url, domain, pid):
    """检查题目是否有测试数据"""
    try:
        r = session.get(f"{base_url}/d/{domain}/p/{pid}", timeout=10)
        return not ("没有测试数据" in r.text and "blockquote" in r.text and "warn" in r.text)
    except Exception:
        return True  # 不确定时假定有数据


def _run_supplement(problem_url):
    """运行测试数据补充"""
    try:
        from testdata_supplement import TestDataSupplement
        td = TestDataSupplement(problem_url)
        td.supplement()
    except Exception as e:
        log.warning("[!] 测试数据补充失败: %s", e)


def solve_one(problem_url, cookie_jar, submit, contest_id=""):
    """求解单道题，返回 {pid, ok, score, is_ac, time_ms, tokens, cost, model, ...}"""
    from oj_solver import Config, OJClient, AIClient, SolverOrchestrator
    requester = int(os.environ.get("OJ_REQUESTER", 0))
    if _delay_mode:
        _oj_limiter.wait()
    try:
        root, api_base, pid = parse_problem_url(problem_url)
        config = Config(cli_overrides={"oj_root": root, "oj_base": api_base, "cookie_jar": cookie_jar})
        oj, ai = OJClient(config), AIClient(config)
        result = SolverOrchestrator(oj, ai, config).solve(
            pid, submit=submit, post=submit, use_stream=False, contest_id=contest_id)
        verdict = result.get("final_verdict") if result and isinstance(result, dict) else None
        usage = result.get("total_usage", {}) if result else {}
        return {
            "pid": pid, "ok": True,
            "score": verdict["score"] if verdict else None,
            "is_ac": verdict.get("is_ac") if verdict else False,
            "time_ms": verdict.get("time_ms") if verdict else 0,
            "tokens_in": usage.get("input", 0),
            "tokens_out": usage.get("output", 0),
            "cache_hit": usage.get("cache_hit", 0),
            "cost": result.get("total_cost", 0) if result else 0,
            "elapsed_s": result.get("total_elapsed", 0) if result else 0,
            "model": result.get("model", "?") if result else "?",
            "is_cost_capped": result.get("is_cost_capped", False) if result else False,
        }
    except Exception as e:
        log.error("[!] P? 异常: %s", e)
        return {"pid": "?", "ok": False, "error": str(e)}


def main():
    from oj_common import setup_logging
    setup_logging(name="contest_solver")
    parser = argparse.ArgumentParser(description="比赛批量解题（多线程，支持多比赛并行）")
    parser.add_argument("urls", nargs="+", help="比赛或题目链接（可多个）")
    parser.add_argument("--no-submit", action="store_true", help="跳过提交")
    parser.add_argument("--force", action="store_true", help="忽略已有题解")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数（默认4）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = ConfigManager()
    _no_data_skips = []  # 无数据跳过的题目列表
    username = os.environ.get("OJ_USERNAME") or cfg.username
    password = os.environ.get("OJ_PASSWORD") or cfg.password
    root = cfg.oj_root

    s = create_session(verify_ssl=False)
    log.info("[*] 登录 OJ ...")
    if not oj_login(s, root, username, password):
        log.error("[-] 登录失败"); sys.exit(1)
    user_id = fetch_user_id(s, root)
    if not user_id: log.error("[-] 获取用户 ID 失败"); sys.exit(1)
    log.info("[+] 用户 ID: %d", user_id)

    # Cookie jar
    jar = str(Path(__file__).parent / ".oj_cookies_batch.json")
    with open(jar, "w", encoding="utf-8") as f:
        json.dump([{"name": c.name, "value": c.value, "domain": c.domain} for c in s.cookies], f)

    # 解析所有 URL
    contests_info = []
    for url in args.urls:
        try:
            info = parse_contest_or_problem(url)
            contests_info.append((url, info))
        except ValueError as e:
            log.error("[-] %s", e)

    if not contests_info:
        log.error("[-] 无有效链接"); sys.exit(1)

    submit = not args.no_submit
    all_failed = []

    # 多线程处理所有比赛的所有题目
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending_tasks = []
        for url, info in contests_info:
            title = info.get("title_prefix") or info.get("contest_id") or info.get("training_id", "?")
            base, domain = info["base_url"], info["domain_id"]
            if info["type"] == "problem":
                pids = info["pids"]
                for pid in pids:
                    if not args.force and has_existing_solution(s, base, domain, pid, user_id):
                        log.info("  #%s — 已有题解，跳过", pid)
                        if not os.environ.get("OJ_REQUESTER"):
                            _push(f"⏭️ #{pid} 跳过(已有题解)", event="problem_skip")
                        continue
                    problem_url = f"{base}/d/{domain}/p/{pid}"
                    if not _has_testdata(s, base, domain, pid):
                        if cfg.auto_supplement_testdata:
                            _run_supplement(problem_url)
                        else:
                            _no_data_skips.append(pid)
                            continue
                    _push(f"🔄 #{pid} 开始", event="problem_start")
                    f = pool.submit(solve_one, problem_url, jar, submit, "")
                    pending_tasks.append((f, pid, title))
            elif info["type"] == "training":
                # 训练：/d/{domain}/training/{id} 或 /training/{id}（system 域）
                api_url = f"{base}/d/{domain}/training/{info['training_id']}" if domain != "system" else url
                r = s.get(api_url, headers={"Accept": "application/json"}, timeout=15)
                if r.status_code != 200: continue
                data = r.json()
                tdoc = data.get("tdoc", {})
                title = tdoc.get("title", info["training_id"])
                pids = data.get("pids", [])
                log.info("[+] 训练: %s, pids=%s", title, pids)
                for pid in pids:
                    if not args.force and has_existing_solution(s, base, domain, pid, user_id):
                        log.info("  #%s — 已有题解，跳过", pid)
                        if not os.environ.get("OJ_REQUESTER"):
                            _push(f"⏭️ #{pid} 跳过(已有题解)", event="problem_skip")
                        continue
                    problem_url = f"{base}/d/{domain}/p/{pid}"
                    if not _has_testdata(s, base, domain, pid):
                        if cfg.auto_supplement_testdata:
                            _run_supplement(problem_url)
                        else:
                            _no_data_skips.append(pid)
                            continue
                    _push(f"🔄 #{pid} 开始", event="problem_start")
                    f = pool.submit(solve_one, problem_url, jar, submit, "")
                    pending_tasks.append((f, pid, title))
            else:
                # 比赛：先参加+获取pids，再提交任务
                attend_contest(s, base, domain, info["contest_id"])
                r = s.get(f"{base}/d/{domain}/contest/{info['contest_id']}",
                          headers={"Accept": "application/json"}, timeout=15)
                if r.status_code != 200: continue
                tdoc = r.json().get("tdoc", {})
                title = tdoc.get("title", info["contest_id"])
                pids = tdoc.get("pids", [])
                cid = info["contest_id"]
                for pid in pids:
                    if not args.force and has_existing_solution(s, base, domain, pid, user_id):
                        log.info("  #%s — 已有题解，跳过", pid)
                        continue
                    problem_url = f"{base}/d/{domain}/p/{pid}"
                    # 检查无测试数据
                    if not _has_testdata(s, base, domain, pid):
                        if cfg.auto_supplement_testdata:
                            log.info("  #%s — 无测试数据，自动补充 ...", pid)
                            _run_supplement(problem_url)
                        else:
                            log.info("  #%s — 无测试数据，跳过（不标记比赛失败）", pid)
                            _no_data_skips.append(pid)
                            continue
                    f = pool.submit(solve_one, problem_url, jar, submit, cid)
                    pending_tasks.append((f, pid, title))

    # 延迟模式：>=20 题时开启，同服务请求至少间隔 2s
    global _delay_mode
    _delay_mode = len(pending_tasks) >= 20
    if _delay_mode:
        log.info("[!] 延迟模式已开启 (≥20题)，同服务请求间隔 ≥2s")
        os.environ["OJ_DELAY_MODE"] = "1"
    log.info("[*] 已提交 %d 个任务，%d 线程并行处理中...", len(pending_tasks), args.workers)
    requester = int(os.environ.get("OJ_REQUESTER", 0))
    results = {}
    if requester:
        _push(f"共 {len(pending_tasks)} 题待处理")

    if pending_tasks:
        task_map = {t[0]: (t[1], t[2]) for t in pending_tasks}
        # 全局统计
        total_tokens_in = total_tokens_out = total_cache = total_cost = total_elapsed = 0.0
        model_stats = {}  # {model: {tokens_in, tokens_out, cache, cost, count, ac}}
        for f in as_completed(task_map):
            pid, title = task_map[f]
            if title not in results:
                results[title] = {"success": [], "failed": [], "total": len([x for x in pending_tasks if x[2] == title])}
            try:
                r = f.result()
                rcnt = r.get("retry_count", 0)
                # 累计统计
                ti, to, ch, ct = r.get("tokens_in", 0), r.get("tokens_out", 0), r.get("cache_hit", 0), r.get("cost", 0)
                total_tokens_in += ti; total_tokens_out += to; total_cache += ch; total_cost += ct
                total_elapsed += r.get("elapsed_s", 0)
                model = r.get("model", "?")
                if model not in model_stats:
                    model_stats[model] = {"tokens_in": 0, "tokens_out": 0, "cache": 0, "cost": 0, "count": 0, "ac": 0}
                ms = model_stats[model]
                ms["tokens_in"] += ti; ms["tokens_out"] += to; ms["cache"] += ch; ms["cost"] += ct; ms["count"] += 1
                if r.get("ok") and r.get("is_ac"):
                    results[title]["success"].append(str(pid))
                    ms["ac"] += 1
                    log.info("[+] #%s ✓ AC | 得分 %s | %sms | Token %di/%do ¥%.4f",
                             pid, r.get("score"), r.get("time_ms", 0), ti, to, ct)
                    _push(f"✅ #{pid} AC ({r.get('time_ms',0):.0f}ms  ¥{ct:.4f})" +
                          (f" 经{rcnt}次修正" if rcnt > 0 else ""), event="problem_ac")
                elif r.get("ok"):
                    if r.get("is_cost_capped"):
                        # 费用超上限 — 视为完成（不阻止比赛标记完成）
                        results[title]["success"].append(str(pid))
                        log.warning("[!] #%s ⊗ 费用超限 ¥%.4f | Token %di/%do",
                                    pid, ct, ti, to)
                        _push(f"💰 #{pid} 费用超限 ¥{ct:.2f}，已停止", event="problem_ac")
                    else:
                        results[title]["failed"].append(str(pid))
                        log.warning("[-] #%s ✗ 得分 %s | Token %di/%do ¥%.4f",
                                    pid, r.get("score"), ti, to, ct)
                        _push(f"❌ #{pid} 得分{r.get('score')} 修正{rcnt}次未AC", event="problem_fail")
                else:
                    results[title]["failed"].append(str(pid))
                    log.error("[-] P%s 异常: %s", pid, r.get("error", "?"))
            except Exception as e:
                results[title]["failed"].append(str(pid))
                log.error("[-] P%s 线程异常: %s", pid, e)

    # 汇总
    log.info("\n" + "=" * 60)
    for title, r in results.items():
        s_count = len(r["success"]); f_count = len(r["failed"])
        log.info("  %s: %d/%d 成功%s", title, s_count, s_count + f_count,
                 f" — 失败: {r['failed']}" if f_count else "")
        if f_count: all_failed.extend(r["failed"])
    log.info("-" * 50)
    log.info("💰 Token 总计: %di / %do | 缓存命中: %d | 预估费用: ¥%.4f | 耗时: %.0fs",
             total_tokens_in, total_tokens_out, total_cache, total_cost, total_elapsed)
    # 模型排行
    if model_stats:
        log.info("-" * 40)
        log.info("📊 模型 Token/费用排行:")
        ranked = sorted(model_stats.items(), key=lambda x: x[1]["cost"], reverse=True)
        for i, (m, s) in enumerate(ranked, 1):
            log.info("  #%d %s: %d题 AC%d | Token %di/%do | 缓存%d | ¥%.4f",
                     i, m, s["count"], s["ac"], s["tokens_in"], s["tokens_out"], s["cache"], s["cost"])
    log.info("=" * 60)
    if all_failed: sys.exit(1)


if __name__ == "__main__":
    main()
