#!/usr/bin/env python3
"""比赛批量解题 — 多线程并行求解，支持多比赛同时处理。"""

import argparse
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qsl, urlsplit

from hydroai.config.manager import ConfigManager
from hydroai.config.paths import data_file, project_root
from hydroai.oj.api import OJApi, OJError
from hydroai.oj.common import (
    RateLimiter, create_session, fetch_user_id, load_dotenv, parse_contest_or_problem,
    parse_problem_url, push_oj_message, smart_login,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s.%(msecs)03d %(message)s", datefmt="%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

# 全局 OJ 限流器（延迟模式时启用）
_oj_limiter = RateLimiter(2.0)
_delay_mode = False
_session_lock = threading.Lock()  # 共享 session 线程安全
_push_session = None  # main() 登录后设为共享主 session


def _set_delay_mode():
    """开启延迟模式：本进程内 OJ 请求限速 ≥2s，并传递给子进程。

    任务数达到阈值（默认 20）时调用。独立成函数是为了让「边入队边判断」
    与「入队结束后的兜底判断」共用同一处实现，避免两处各自设置状态。
    """
    global _delay_mode
    if _delay_mode:
        return
    _delay_mode = True
    os.environ["OJ_DELAY_MODE"] = "1"
    log.info("[!] 延迟模式已开启 (≥20题)，同服务请求间隔 ≥2s")

def _push(text: str, to_uid: int = 0, event: str = ""):
    """推送消息。复用主 session + oj_common 限速/403重试。线程安全。"""
    if event:
        try:
            pe = json.loads(os.environ.get("OJ_PUSH_EVENTS", "{}"))
            if not pe.get(event, True): return
        except Exception: pass
    session = _push_session
    if session is None:
        return
    try:
        root = os.environ.get("OJ_ROOT", "https://oj.yuanyicode.com")
        requester = int(os.environ.get("OJ_REQUESTER", 0))
        pids_raw = os.environ.get("OJ_PUSH_LIST", "")
        push_uids = [int(x.strip()) for x in pids_raw.split(",") if x.strip().isdigit()]
        push_oj_message(session, root, text,
                        push_uids=push_uids, requester=to_uid or requester)
    except Exception as e:
        log.debug("[!] push failed: %s", e)


def _gateway(session, base_url, domain):
    return OJApi(base_url, domain, session=session,
                 username=os.environ.get("OJ_USERNAME", ""),
                 password=os.environ.get("OJ_PASSWORD", ""))


def has_existing_solution(session, base_url, domain_id, pid, uid):
    try:
        with _session_lock:
            data = _gateway(session, base_url, domain_id).solutions.list(pid)
        for doc in data.get("psdocs", []):
            if doc.get("owner") == uid: return True
    except OJError as e:
        if e.status:
            return False
        log.warning("[!] 检查已有题解异常 #%s: %s — 保守跳过", pid, str(e)[:80])
        return True
    except Exception as e:
        log.warning("[!] 检查已有题解异常 #%s: %s — 保守跳过", pid, str(e)[:80])
        return True  # 不确定时跳过，防止重复
    return False


def attend_contest(session, base_url, domain_id, contest_id):
    try:
        return _gateway(session, base_url, domain_id).contests.attend(contest_id)
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
        from hydroai.solve.testdata import TestDataSupplement
        td = TestDataSupplement(problem_url)
        td.supplement()
    except Exception as e:
        log.warning("[!] 测试数据补充失败: %s", e)


def solve_one(problem_url, cookie_jar, submit, contest_id="", accum=True):
    """求解单道题，返回 {pid, ok, score, is_ac, time_ms, tokens, cost, model, ...}"""
    from hydroai.ai.client import AIClient
    from hydroai.oj.client import OJClient
    from hydroai.solve.solver import SolverOrchestrator
    if _delay_mode:
        _oj_limiter.wait()
    try:
        root, api_base, pid = parse_problem_url(problem_url)
        config = ConfigManager(cli_overrides={"oj_root": root, "oj_base": api_base, "cookie_jar": cookie_jar})
        oj, ai = OJClient(config), AIClient(config)
        result = SolverOrchestrator(oj, ai, config).solve(
            pid, submit=submit, post=submit, use_stream=False, contest_id=contest_id,
            accumulate=accum)
        verdict = result.get("final_verdict") if result and isinstance(result, dict) else None
        usage = result.get("total_usage", {}) if result else {}
        ai_health = result.get("ai_health") if result and isinstance(result, dict) else None
        return {
            "pid": pid, "ok": True,
            "ai_unavailable": bool(result.get("ai_unavailable")) if result else False,
            "ai_health": ai_health or {},
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


class ProblemDiscoveryError(RuntimeError):
    """已识别目标，但未能读取其题目列表。"""


def _read_target(session, url):
    try:
        parsed = urlsplit(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        response = _gateway(session, root, "system").get(
            parsed.path, domain=False, params=parse_qsl(parsed.query), raw=True, timeout=15)
    except OJError as exc:
        raise ProblemDiscoveryError(f"请求失败：{exc}") from exc
    if response.status_code != 200:
        raise ProblemDiscoveryError(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ProblemDiscoveryError("响应不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise ProblemDiscoveryError("响应应为 JSON 对象")
    return data


def discover_problems(session, url, info, *, attend=True):
    """发现目标题目；读取错误与空列表不同，只读模式不参加比赛。"""
    base, domain = info["base_url"], info["domain_id"]
    cid = ""
    if info["type"] == "problem":
        title, pids = info["title_prefix"], info["pids"]
    else:
        is_contest = info["type"] == "contest"
        if is_contest:
            cid = info["contest_id"]
            if attend:
                attend_contest(session, base, domain, cid)
            api_url = f"{base}/d/{domain}/contest/{cid}"
        else:
            tid = info["training_id"]
            api_url = f"{base}/d/{domain}/training/{tid}" if domain != "system" else url
        data = _read_target(session, api_url)
        document = data.get("tdoc", {})
        if not isinstance(document, dict):
            raise ProblemDiscoveryError("题目列表所属的赛事或训练文档格式无效")
        title = document.get("title")
        if not isinstance(title, str) or not title.strip():
            title = cid if is_contest else tid
        pids = (document if is_contest else data).get("pids")
        if not isinstance(pids, list) or not all(type(pid) in (int, str) and str(pid).strip() for pid in pids):
            raise ProblemDiscoveryError("缺少有效的题目列表（pids）")
        log.info("[+] %s: %s, pids=%s", "比赛" if is_contest else "训练", title, pids)
    return ((base, domain, pid, title, cid) for pid in pids)


def _for_each_problem(session, urls, visit, *, attend=True) -> bool:
    all_read = True
    for url in urls:
        try:
            info = parse_contest_or_problem(url)
            problems = discover_problems(session, url, info, attend=attend)
        except ValueError as exc:
            log.error("[-] %s", exc)
            all_read = False
            continue
        except ProblemDiscoveryError as exc:
            log.error("[-] 读取目标失败: %s（%s）", url, exc)
            all_read = False
            continue
        for problem in problems:
            visit(*problem)
    return all_read


def main():
    load_dotenv()
    from hydroai.oj.common import setup_logging
    parser = argparse.ArgumentParser(description="比赛批量解题（多线程，支持多比赛并行）")
    parser.add_argument("urls", nargs="+", help="比赛或题目链接（可多个）")
    parser.add_argument("--no-submit", action="store_true", help="跳过提交")
    parser.add_argument("--force", action="store_true", help="忽略已有题解")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数（默认4）")
    parser.add_argument("--no-accum", action="store_true", help="忽略单题金额累计（手动触发时使用）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        setup_logging(name="contest_solver")

    cfg = ConfigManager()
    root = cfg.oj_root

    s = create_session(verify_ssl=False)
    log.info("[*] 登录 OJ ...")
    if not smart_login(s, root, cfg.username, cfg.password):
        log.error("[-] 登录失败")
        sys.exit(1)
    if args.dry_run:
        def preview(base, domain, pid, title, _cid):
            log.info("[dry-run] %s: %s/d/%s/p/%s", title, base, domain, pid)

        if not _for_each_problem(s, args.urls, preview, attend=False):
            sys.exit(1)
        return

    user_id = fetch_user_id(s, root)
    if not user_id: log.error("[-] 获取用户 ID 失败"); sys.exit(1)
    log.info("[+] 用户 ID: %d", user_id)
    global _push_session
    _push_session = s  # 复用主 session 推送

    # Cookie jar
    jar = str(data_file(".oj_cookies_batch.json", project_root()))
    with open(jar, "w", encoding="utf-8") as f:
        json.dump([{"name": c.name, "value": c.value, "domain": c.domain} for c in s.cookies], f)

    submit = not args.no_submit
    all_failed = []

    # 多线程处理所有比赛的所有题目
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending_tasks = []

        def enqueue(base: str, domain: str, pid, title: str, cid: str = ""):
            """单题、训练和比赛共用查重、测试数据检查与入队规则。"""
            problem_url = f"{base}/d/{domain}/p/{pid}"
            if not args.force and has_existing_solution(s, base, domain, pid, user_id):
                log.info("  #%s — 已有题解，跳过", pid)
                if not os.environ.get("OJ_REQUESTER"):
                    _push(f"⏭️ #{pid} 跳过(已有题解)", event="problem_skip")
                return
            if not _has_testdata(s, base, domain, pid):
                if cfg.auto_supplement_testdata:
                    log.info("  #%s — 无测试数据，自动补充 ...", pid)
                    _run_supplement(problem_url)
                else:
                    log.info("  #%s — 无测试数据，跳过", pid)
                    return
            _push(f"🔄 #{pid} 开始", event="problem_start")
            # 任务数一旦达到 20 就立刻开启延迟模式；旧实现等全部入队后才设置，
            # 前 20 个任务会以无限速的方式抢跑。
            if not _delay_mode and len(pending_tasks) >= 20:
                _set_delay_mode()
            f = pool.submit(solve_one, problem_url, jar, submit, cid, not args.no_accum)
            pending_tasks.append((f, pid, title))

        discovery_ok = _for_each_problem(s, args.urls, enqueue)

    # 延迟模式兜底：入队过程中可能已提前开启，这里再确认一次
    if len(pending_tasks) >= 20:
        _set_delay_mode()
    log.info("[*] 已提交 %d 个任务，%d 线程并行处理中...", len(pending_tasks), args.workers)
    requester = int(os.environ.get("OJ_REQUESTER", 0))
    results = {}
    ai_failure_health = None
    # 全局统计（在 pending_tasks 块外初始化，避免全跳过时 UnboundLocalError）
    total_tokens_in = total_tokens_out = total_cache = total_cost = total_elapsed = 0.0
    model_stats = {}  # {model: {tokens_in, tokens_out, cache, cost, count, ac}}
    if requester:
        _push(f"共 {len(pending_tasks)} 题待处理")

    if pending_tasks:
        task_map = {t[0]: (t[1], t[2]) for t in pending_tasks}
        for f in as_completed(task_map):
            pid, title = task_map[f]
            if title not in results:
                results[title] = {"success": [], "failed": [], "total": len([x for x in pending_tasks if x[2] == title])}
            try:
                r = f.result()
                if r.get("ai_unavailable") and ai_failure_health is None:
                    ai_failure_health = r.get("ai_health") or {}
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
    if ai_failure_health:
        from hydroai.ai.health import failure_message, should_notify
        message = failure_message(ai_failure_health)
        log.error(message)
        channel = "push" if os.environ.get("OJ_PUSH_LIST") else "requester"
        if should_notify(cfg, ai_failure_health, channel=channel):
            _push(message, event="ai_unavailable")
        sys.exit(2)
    if all_failed or not discovery_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
