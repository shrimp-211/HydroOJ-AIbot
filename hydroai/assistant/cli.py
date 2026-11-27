"""assistant 命令行：``hydro.py assistant run|handle|status``。"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from hydroai.assistant.events import event_for_discussion
from hydroai.config.paths import data_dir


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hydro.py assistant", description="@ 助手 Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="常驻监听系统 @ 通知")
    r.add_argument("--once", action="store_true", help="处理一轮后退出")
    r.add_argument("--dry-run", action="store_true", help="只打印回复，不发送")
    r.add_argument("--max-age", type=float, help="只处理多少秒内的通知（默认 3600）")
    h = sub.add_parser("handle", help="手工模拟一次 @（调试）")
    h.add_argument("target", help="讨论 URL 或讨论 id")
    h.add_argument("--domain", help="域（target 为 id 时必填，默认取 assistant_domains 首项）")
    h.add_argument("--as-user", type=int, required=True, help="提问者 UID")
    h.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("status", help="最近处理记录")
    s.add_argument("-n", type=int, default=20)
    return p


def main(argv: list[str] | None = None) -> int:
    from hydroai.config.io import load_dotenv
    from hydroai.config.manager import ConfigManager
    from hydroai.oj.api import OJApi, OJError
    from hydroai.oj.common import setup_logging
    load_dotenv()
    args = _parser().parse_args(argv)
    setup_logging(name="assistant")
    config = ConfigManager()
    if args.cmd == "status":
        from hydroai.assistant.store import AssistantStore
        for r in AssistantStore(data_dir() / "assistant.db").recent(args.n):
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(r["at"]))
            print(f"{when} {r['domain']}/{r['did'][:8]} {r['author']} {r['status']} "
                  f"回复{r['replies']}{' dry' if r['dry_run'] else ''} {r['elapsed']:.1f}s ¥{r['cost']:.4f} {r['error']}")
        return 0
    from hydroai.assistant.service import AssistantService
    api = OJApi.from_config(config)
    if not api.login():
        print("登录失败", file=sys.stderr)
        return 1
    service = AssistantService(api, config, dry_run=True if args.dry_run else None)
    try:
        if args.cmd == "handle":
            domains = config.get("assistant_domains", []) or []
            domain = args.domain or (domains[0] if domains else "system")
            event = event_for_discussion(args.target, domain, args.as_user)
            res = service.handle_event(event)
            print(f"状态={res.status} {res.reason} 回复={res.replies} 步数={res.steps} 费用=¥{res.cost:.4f}")
            return 0 if res.status in ("replied", "ignored", "limited") else 1
        if args.max_age:
            service.max_age = args.max_age
        n = service.run(once=args.once)
        if args.once:
            print(f"本轮处理 {n} 条通知")
        return 0
    except OJError as exc:
        print(f"OJ 错误: {exc}", file=sys.stderr)
        return 1
