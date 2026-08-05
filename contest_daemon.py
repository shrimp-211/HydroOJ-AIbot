#!/usr/bin/env python3
"""比赛守护进程 — 监测比赛结束，自动编写题解。"""

import os
import re
import sys
import json
import queue
import time
import signal
import logging
import threading
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone
import requests
from oj_common import (load_dotenv, load_config, create_session, oj_login,
                        parse_contest_or_problem, parse_problem_url, smart_login)

log = logging.getLogger(__name__)

if sys.platform == "win32" and sys.stdout.isatty():
    import io
    try: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception: pass


def _script_dir() -> Path:
    """脚本目录。兼容 __file__ 缺失的嵌入/面板运行环境（如 MCSManager exec 启动）。
    优先返回包含本脚本的目录，避免 argv[0] 指向解释器时路径错误。"""
    candidates = []
    f = globals().get("__file__")
    if f:
        candidates.append(Path(f).resolve().parent)
    if sys.argv and sys.argv[0]:
        candidates.append(Path(sys.argv[0]).resolve().parent)
    candidates.append(Path.cwd())
    for d in candidates:
        if (d / "contest_daemon.py").exists():
            return d
    return candidates[0]


# ═══════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════
def _get_defaults():
    cfg = load_config()
    mon = cfg.get("monitor", {})
    return (
        cfg.get("monitor_domains") or mon.get("domains", ["system"]),
        cfg.get("monitor_interval") or mon.get("interval", 120),
        cfg.get("monitor_state_file") or mon.get("state_file", "processed_contests.json"),
    )
DEFAULT_DOMAINS, DEFAULT_INTERVAL, DEFAULT_STATE_FILE = _get_defaults()


# ═══════════════════════════════════════════════════
# 比赛列表
# ═══════════════════════════════════════════════════
def list_contests(session, root: str, domain: str, max_pages: int = 20) -> list:
    all_contests, page = [], 1
    while page <= max_pages:
        r = session.get(f"{root}/d/{domain}/contest",
                        headers={"Accept": "application/json"},
                        params={"page": page, "limit": 50}, timeout=30)
        if r.status_code != 200: break
        tdocs = r.json().get("tdocs", [])
        if not tdocs: break
        all_contests.extend(tdocs)
        if len(tdocs) < 50: break
        page += 1
    return all_contests


# ═══════════════════════════════════════════════════
# 状态
# ═══════════════════════════════════════════════════
def load_state(path: str) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f: return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError): return set()

_state_lock = threading.Lock()

def save_state(path: str, s: set):
    with _state_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(s), f, indent=2)
        Path(tmp).replace(path)


# ═══════════════════════════════════════════════════
# 核心
# ═══════════════════════════════════════════════════
def check_contests(root: str, domains: list, processed: set,
                   username: str, password: str, include_ongoing: bool = True) -> list:
    session = create_session(verify_ssl=False)
    log.info("[*] 登录 %s ...", root)
    if not smart_login(session, root, username, password):
        log.error("[-] 登录失败")
        _push_event("contest_login_fail", "比赛检查登录失败")
        return []
    now = datetime.now(timezone.utc); pending = []
    for domain in domains:
        log.info("[*] 检查 domain=%s ...", domain)
        try:
            contests = list_contests(session, root, domain)
        except requests.RequestException as e:
            log.warning("  [!] 获取列表异常: %s", e); continue
        log.info("    找到 %d 个比赛", len(contests))
        for tdoc in contests:
            cid = tdoc.get("_id", ""); end_s = tdoc.get("endAt", "")
            if not cid or not end_s: continue
            try: end_at = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
            except ValueError: continue
            ended = end_at <= now; done = cid in processed
            tag = "已处理 ✓" if done else ("已结束 ✓" if ended else "进行中")
            log.info("    [%s] %s  end=%s  pids=%s", tag, tdoc.get("title", cid)[:30],
                     end_s[:19], tdoc.get("pids", []))
            if not done and (ended or include_ongoing):
                pending.append({"domain": domain, "cid": cid,
                    "title": tdoc.get("title", cid), "pids": tdoc.get("pids", []),
                    "contest_url": f"{root}/d/{domain}/contest/{cid}"})
    return pending


# 推送事件（模块级状态，main() 初始化后供各线程使用）
_msg_be = None
_push_events = {}

def _push_event(event: str, text: str):
    """推送事件到私信后端。_push_events 控制各事件开关（线程安全）。"""
    be = _msg_be
    if be is None or not _push_events.get(event, True):
        return
    try:
        be.push(text)
    except Exception as e:
        log.debug("[!] push_event %s 异常: %s", event, e)

def process_contest(contest: dict, solver_path: str, push_list: set = None) -> bool:
    url = contest["contest_url"]
    title = contest["title"]
    ts = datetime.now().strftime("%H:%M:%S")
    log.info("\n" + "=" * 60 + f"\n  [处理] {title}\n  URL: {url}\n  题目: {contest['pids']}\n" + "=" * 60)
    _push_event("contest_start", f"▶️ {title[:30]} 开始 ({len(contest['pids'])}题)")
    env = os.environ.copy()
    env["OJ_USERNAME"] = os.environ.get("OJ_USERNAME", "")
    env["OJ_PASSWORD"] = os.environ.get("OJ_PASSWORD", "")
    env["OJ_ROOT"] = os.environ.get("OJ_ROOT",
        contest.get("contest_url", "").split("/d/")[0] if "/d/" in contest.get("contest_url","") else "https://oj.yuanyicode.com")
    if push_list:
        env["OJ_PUSH_LIST"] = ",".join(str(x) for x in push_list)
    r = subprocess.run([sys.executable, solver_path, url],
                       cwd=str(Path(solver_path).parent), env=env)
    if r.returncode == 0:
        log.info("[+] 完成: %s", title); return True
    log.error("[-] 失败 (exit %d): %s", r.returncode, title); return False


# ═══════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════
def main():
    load_dotenv()
    from oj_common import setup_logging
    setup_logging(name="daemon")
    parser = argparse.ArgumentParser(description="比赛守护进程")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--domain", action="append", dest="domains")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--root", help="OJ 根地址")
    parser.add_argument("--ended-only", action="store_true", help="仅处理已结束的比赛")
    parser.add_argument("--no-interactive", action="store_true", help="禁用交互终端输入")
    parser.add_argument("--no-msg-backend", action="store_true", help="禁用私信后端")
    args = parser.parse_args()

    from config_manager import ConfigManager
    c = ConfigManager()
    # 配置完整性检查
    repaired = c.repair()
    if repaired:
        log.info("[配置] 已补齐 %d 个字段: %s", len(repaired), ", ".join(repaired))
    warnings = c.validate()
    if warnings:
        log.warning("[配置] %d 个警告 (详见上方)", len(warnings))
    else:
        log.info("[配置] 完整性检查通过")
    root = args.root or c.oj_root
    username = c.username or os.environ.get("OJ_USERNAME", "")
    password = c.password or os.environ.get("OJ_PASSWORD", "")
    include_ongoing = not args.ended_only
    domains = args.domains if args.domains else c.monitor_domains
    state_file = args.state_file or c.monitor_state_file
    interval = args.interval or c.monitor_interval

    # 找 contest_solver.py
    sp = _script_dir() / "contest_solver.py"
    if not sp.exists(): sp = Path("contest_solver.py")
    if not sp.exists(): log.error("[-] 找不到 contest_solver.py"); sys.exit(1)

    processed = load_state(state_file)
    log.info("[*] 已记录 %d 个已处理比赛", len(processed))
    log.info("[启动] 监控域: %s | 间隔: %ds | 凭据: %s",
             domains, interval, "已设置" if username else "未设置")

    # 私信后端
    msg_be = None
    msg_whitelist = set()
    msg_push_list = set()
    msg_superuser = set()
    if not args.no_msg_backend:
        from msg_backend import MsgBackend
        from oj_common import fetch_user_id
        msg_s = create_session(verify_ssl=False)
        if not smart_login(msg_s, root, username, password):
            log.error("[-] 消息后端登录失败")
        else:
            muid = fetch_user_id(msg_s, root) or 0
            msg_whitelist = set(str(x) for x in c.msg_whitelist)
            msg_push_list = set(c.msg_push_list)
            msg_superuser = set(str(x) for x in c.msg_superuser)
            msg_be = MsgBackend(msg_s, root, muid, msg_whitelist, msg_push_list,
                                c.msg_interval)
            msg_be.start_async()
            log.info("[*] 消息后端启动 | 白名单:%s | 推送:%s | SU:%s",
                     msg_whitelist, msg_push_list, msg_superuser)


    # 推送事件开关（按节点控制）
    # 仪表盘
    from dashboard import Dashboard
    dash = Dashboard()

    push_events = c.msg_push_events
    global _msg_be, _push_events
    _push_events = push_events
    _msg_be = msg_be if (msg_be and msg_push_list) else None
    # 将推送事件开关传给子进程
    os.environ["OJ_PUSH_EVENTS"] = json.dumps(push_events)
    _push_event("daemon_start", "守护进程已启动")
    if msg_be and msg_push_list:
        _push_event("msg_backend_start", f"消息后端已启动 (白名单:{len(msg_whitelist)}人)")

    def run_check():
        nonlocal processed
        log.info("\n" + "─" * 50 + f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 检查中...")
        pending = check_contests(root, domains, processed, username, password, include_ongoing=include_ongoing)
        if not pending: log.info("[*] 无待处理比赛"); return
        log.info("\n[*] 发现 %d 个待处理比赛（%d 线程并发）", len(pending), len(pending))

        # 分组展示
        by_domain = {}
        for c in pending: by_domain.setdefault(c["domain"], []).append(c)
        for d, clist in by_domain.items():
            log.info("  domain=%s: %d 个比赛", d, len(clist))

        # 多线程并发处理
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(len(pending), 4)) as pool:
            futures = {pool.submit(process_contest, c, str(sp), msg_push_list): c for c in pending}
            for f in as_completed(futures):
                c = futures[f]
                try:
                    ok = f.result()
                    if ok:
                        processed.add(c["cid"]); save_state(state_file, processed)
                        log.info("[+] 已记录: %s — %s", c['domain'], c['title'][:30])
                        _push_event("contest_done", f"✅ {c['title'][:30]} 完成")
                    else:
                        _push_event("contest_fail", f"⚠️ {c['title'][:30]} 部分未AC")
                except Exception as e:
                    log.error("[-] %s 处理异常: %s", c['title'][:30], e)
            # 标程扫描：处理完比赛后自动生成标程题解
            if processed:
                log.info("[*] 自动标程题解扫描 ...")
                sp_bm = str(_script_dir() / "benchmark_solver.py")
                try:
                    subprocess.run([sys.executable, sp_bm],
                                   cwd=str(Path(sp_bm).parent),
                                   env=os.environ.copy(), timeout=600)
                except Exception:
                    pass

    # 首次检查放在后台线程，避免阻塞交互
    threading.Thread(target=run_check, daemon=True).start()
    if args.once:
        # once 模式下等待检查完成
        time.sleep(5)
        return
    # 交互输入（适配所有终端：TTY/管道/IDE终端）
    has_tty = sys.stdin.isatty()
    if not args.no_interactive:
        log.info("\n[*] 守护模式，每 %ds 检查 | 交互求解：输入链接/ID", interval)
        log.info("    格式: PID | p/PID | 完整URL | d/域/contest/id | training/id")
        log.info("    命令: help | list | exit\n")
    else:
        log.info("\n[*] 守护模式，每 %ds 检查（无交互）\n", interval)

    input_queue = queue.Queue()
    if not args.no_interactive:
        def input_reader():
            while True:
                try:
                    line = sys.stdin.readline()
                    if not line:
                        break
                    line = line.strip()
                    if line:
                        input_queue.put(line)
                except (EOFError, OSError, ValueError, KeyboardInterrupt):
                    break
        threading.Thread(target=input_reader, daemon=True).start()

    blocked = set(c.msg_blocked_cmds)

    def dispatch(cmd: str, reply_uid: int = 0) -> str:
        """统一指令分发。reply_uid=0=控制台，>0=私信回复"""
        is_chat = reply_uid > 0  # 是否为私聊回复
        if is_chat and cmd.lower() in blocked:
            return "⛔ 此指令仅控制台可用"
        if cmd.lower() in ("exit", "quit", "q"):
            return "EXIT"

        parts = cmd.split()
        is_su = (reply_uid == 0) or (str(reply_uid) in msg_superuser)

        # ── 名单管理 ──
        if parts[0].lower() == "whitelist" and len(parts) >= 2:
            if not is_su: return "⛔ 仅超级管理员可用"
            action, target = parts[1].lower(), parts[2] if len(parts) > 2 else ""
            if action == "add" and target.isdigit():
                msg_whitelist.add(target); c.set_override(msg_whitelist=[int(x) for x in msg_whitelist])
                c.save(); return f"✅ 白名单 +{target} (已保存)"
            if action == "remove" and target.isdigit():
                msg_whitelist.discard(target); c.set_override(msg_whitelist=[int(x) for x in msg_whitelist])
                c.save(); return f"✅ 白名单 -{target} (已保存)"
            if action == "list": return f"👥 白名单: {msg_whitelist}"
            return "格式: whitelist add|remove|list <UID>"

        if parts[0].lower() == "pushlist" and len(parts) >= 2:
            if not is_su: return "⛔ 仅超级管理员可用"
            action, target = parts[1].lower(), parts[2] if len(parts) > 2 else ""
            if action == "add" and target.isdigit():
                msg_push_list.add(int(target)); c.set_override(msg_push_list=list(msg_push_list))
                c.save(); return f"✅ 推送 +{target} (已保存)"
            if action == "remove" and target.isdigit():
                msg_push_list.discard(int(target)); c.set_override(msg_push_list=list(msg_push_list))
                c.save(); return f"✅ 推送 -{target} (已保存)"
            if action == "list": return f"📢 推送名单: {msg_push_list}"
            return "格式: pushlist add|remove|list <UID>"

        if parts[0].lower() == "push" and len(parts) >= 2:
            if not is_su: return "⛔ 仅超级管理员可用"
            _push_event("user_broadcast", " ".join(parts[1:]))
            return "✅ 已推送"

        if cmd.lower() == "help":
            h = (
                "=== OJ AI 助手 指令列表 ===\n\n"
                "[求解]\n"
                "  1316 / p/1316 / /p/1316 — 按题号求解(system域)\n"
                "  d/域/p/题号 — 指定域题目\n"
                "  d/域/contest/比赛ID — 比赛\n"
                "  training/训练ID — 训练\n"
                "  完整URL — 按链接求解\n\n"
                "[查询]\n"
                "  stats / 统计 — 汇总统计(AC/Token/费用)\n"
                "  today / 今日 — 今日统计\n"
                "  recent / 最近 — 最近记录\n"
                "  pending / 进行中 — 正在求解\n"
                "  list — 已处理比赛+监控域\n\n"
                "[控制]\n"
                "  help — 此帮助\n"
                "  exit / quit / q — 退出(仅控制台)")
            if is_su:
                h += (
                    "\n\n[管理 超级用户]\n"
                    "  whitelist add/remove/list <UID>\n"
                    "  pushlist add/remove/list <UID>\n"
                    "  push <消息>\n"
                    "  td <链接> — 补充测试数据\n"
                    "  bm — 全扫标程题解\n"
                    "  bm <比赛/记录链接> — 指定范围")
            return h
        if cmd.lower() in ("stats", "统计"):
            return dash.format_stats()
        if cmd.lower() in ("today", "今日"):
            return dash.format_today()
        if cmd.lower() in ("recent", "最近"):
            return dash.format_recent(5)
        if cmd.lower() in ("pending", "进行中"):
            return dash.format_pending()
        if parts[0].lower() == "td" and len(parts) >= 2:
            if not is_su: return "⛔ 仅超级管理员可用"
            url = parts[1] if parts[1].startswith("http") else f"{root}/d/system/p/{parts[1]}"
            sp_td = str(_script_dir() / "testdata_supplement.py")
            env2 = os.environ.copy()
            if reply_uid > 0:
                env2["OJ_REQUESTER"] = str(reply_uid)
                env2["OJ_USERNAME"] = os.environ.get("OJ_USERNAME","")
                env2["OJ_PASSWORD"] = os.environ.get("OJ_PASSWORD","")
            subprocess.Popen([sys.executable, sp_td, url],
                            cwd=str(Path(sp_td).parent), env=env2)
            return f"🔄 测试数据补充已启动: {url}"

        if parts[0].lower() == "bm":
            sp_bm = str(_script_dir() / "benchmark_solver.py")
            target = parts[1] if len(parts) >= 2 else ""
            if target and not target.startswith("http"):
                target = f"{root}/record/{target}" if re.match(r'^[a-f0-9]+$', target) else target
            env2 = os.environ.copy()
            if reply_uid > 0:
                env2["OJ_REQUESTER"] = str(reply_uid)
                env2["OJ_USERNAME"] = os.environ.get("OJ_USERNAME","")
                env2["OJ_PASSWORD"] = os.environ.get("OJ_PASSWORD","")
            subprocess.Popen([sys.executable, sp_bm] + ([target] if target else []),
                            cwd=str(Path(sp_bm).parent), env=env2)
            return f"🔄 标程题解生成已启动" + (f": {target}" if target else " (全扫)")

        if cmd.lower() in ("list",):
            if is_chat:
                return (f"📊 已处理 **{len(processed)}** 场比赛\n"
                        f"   监控域: {', '.join(domains)}\n"
                        f"   输入 `stats` 查看详细统计")
            return f"已处理: {len(processed)} | 监控: {domains}"

        url = cmd
        if cmd.isdigit(): url = f"{root}/d/system/p/{cmd}"
        elif re.match(r'^p/?\d+$', cmd, re.IGNORECASE): url = f"{root}/d/system/{cmd}"
        elif re.match(r'^/p/\d+$', cmd): url = f"{root}{cmd}"
        elif re.match(r'^(d/|/d/)', cmd): url = f"{root}/{cmd.lstrip('/')}"
        elif re.match(r'^(training/|/training/)', cmd): url = f"{root}/{cmd.lstrip('/')}"
        elif not cmd.startswith("http"):
            return "❓ 无法识别，输入 help 查看帮助"
        try: info = parse_contest_or_problem(url)
        except ValueError: return "❌ 链接解析失败"

        sp_one = str(_script_dir() / "oj_solver.py")
        env = os.environ.copy()
        if reply_uid > 0:
            env["OJ_REQUESTER"] = str(reply_uid)
            # 环境变量缺失时回退到 config 读取的凭据，避免子进程登录失败
            env["OJ_USERNAME"] = os.environ.get("OJ_USERNAME") or username
            env["OJ_PASSWORD"] = os.environ.get("OJ_PASSWORD") or password

        if info["type"] == "problem":
            pid = info["pids"][0]
            log.info("[*] 求解: #%s", pid)
            dash.problem_start(pid, title=info.get("title", ""))
            try:
                subprocess.Popen([sys.executable, sp_one, url, "--no-show-thinking"],
                                cwd=str(Path(sp_one).parent), env=env)
            except OSError as e:
                return f"❌ #{pid} 启动失败: {e}"
            return f"🔄 #{pid} 已开始求解，完成后会自动回复结果"
        else:
            cid = info.get("contest_id") or info.get("training_id", url.split('/')[-1][:20])
            title = info.get("title", cid)
            log.info("[*] 求解: %s", title)
            try:
                subprocess.Popen([sys.executable, str(sp), url, "--no-accum"],
                                cwd=str(Path(sp).parent), env=env)
            except OSError as e:
                return f"❌ {title} 启动失败: {e}"
            return f"🔄 {title} 已启动（{len(info.get('pids',[]))}题），完成后会自动回复结果"

    def _push_event_impl(text: str):
        """推送事件给推送名单，自动加时间戳"""
        if msg_be and msg_push_list:
            ts = datetime.now().strftime("%m-%d %H:%M:%S")
            msg_be.push(f"[{ts}] {text}")

    last_check = 0
    while True:
        try:
            now = time.time()
            if now - last_check >= interval:
                threading.Thread(target=run_check, daemon=True).start()
                last_check = now

            # 统一读取：控制台 + 消息后端
            reply_uid = 0
            cmd = None
            for q in ([input_queue] if not args.no_interactive else []) + \
                     ([msg_be.cmd_queue] if msg_be else []):
                try:
                    item = q.get_nowait()
                    if isinstance(item, tuple):
                        cmd, reply_uid = item
                    else:
                        cmd = item
                    break
                except queue.Empty:
                    continue
            if cmd is None:
                try: cmd = input_queue.get(timeout=1)
                except queue.Empty: continue

            result = dispatch(cmd, reply_uid)
            if result == "EXIT":
                log.info("[*] 退出"); break

            # 回复
            if reply_uid and msg_be:
                msg_be.send(reply_uid, result)
            elif reply_uid == 0 and result:
                log.info("%s", result)

        except KeyboardInterrupt:
            log.info("\n[*] 退出"); break
        except (requests.RequestException, subprocess.SubprocessError, OSError, ValueError) as e:
            log.warning("[!] 异常: %s，继续运行", e)
            _push_event("api_error", f"API异常: {str(e)[:80]}")

    # 主循环退出（exit 指令 / Ctrl+C）：强制终止进程。
    # run_check 的 ThreadPoolExecutor 工作线程为非 daemon，若其 worker
    # 正卡在 contest_solver 子进程等待上，解释器退出会 join 等待 worker，
    # 导致 exit 指令卡住数分钟。
    log.info("[*] 守护进程退出")
    os._exit(0)


if __name__ == "__main__":
    main()
