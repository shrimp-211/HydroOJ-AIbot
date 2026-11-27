"""insight 命令行。

    hydro.py insight --domain root_test scan --uid 35 --dry-run
    hydro.py insight --domain root_test scan --all --days 30
    hydro.py insight --domain root_test analyze <rid>
    hydro.py insight --domain root_test profile --uid 35
    hydro.py insight --domain root_test report --uid 35 [--send [--to UID]]
    hydro.py insight --domain root_test class
"""
from __future__ import annotations

import argparse
import json
import sys

from hydroai.oj.api import OJApi, OJError


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hydro.py insight", description="学情诊断")
    p.add_argument("--domain", help="域 ID（默认取 oj_base）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="扫描并分析")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--uid", type=int)
    g.add_argument("--all", action="store_true", help="扫描域内活跃学生")
    s.add_argument("--days", type=float); s.add_argument("--max", type=int, dest="max_n")
    s.add_argument("--max-users", type=int, default=20)
    s.add_argument("--dry-run", action="store_true", help="只显示选出的提交，不调用 AI")
    s = sub.add_parser("analyze", help="分析单条记录"); s.add_argument("rid")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("profile", help="画像 JSON"); s.add_argument("--uid", type=int, required=True)
    s = sub.add_parser("report", help="学情报告")
    s.add_argument("--uid", type=int, required=True)
    s.add_argument("--send", action="store_true", help="私信发送报告")
    s.add_argument("--to", type=int, help="接收人 UID（默认该学生）")
    sub.add_parser("class", help="班级概览")
    return p


def _show(a) -> str:
    score = f"{a.score:g}分" if a.score is not None else "分数未知"
    return (f"题目{a.pid} rid={a.rid} [{a.status}/{score}] {a.error_type} | "
            f"定性判断（待验证）：{a.distance_level} | 标签 {'、'.join(a.knowledge_tags) or '—'}\n"
            f"    原因：{a.root_cause}\n" +
            "".join(f"    待修项（待验证）：{s}\n" for s in a.repair_items) +
            "".join(f"    相关概念（待验证）：{s}\n" for s in a.missing_concepts) +
            "".join(f"    建议：{s}\n" for s in a.suggestions))


def run(svc, args) -> int:
    if args.cmd == "scan":
        if args.dry_run:
            uids = [args.uid] if args.uid else svc.active_users(
                args.days or svc.config.get("insight_lookback_days", 30), args.max_users)
            for uid in uids:
                sels = svc.plan(uid, args.days, args.max_n)
                print(f"== uid={uid}：选出 {len(sels)} 条")
                for s in sels:
                    print(f"  rid={s.rid} P{s.pid} 状态{s.status} {s.score:g}分 优先级{s.priority}  {s.reason}")
            return 0
        if args.uid:
            results = {args.uid: svc.scan_user(args.uid, args.days, args.max_n)}
        else:
            results = svc.scan_domain(args.max_users, args.days, args.max_n)
        for uid, items in results.items():
            print(f"== uid={uid}：分析 {len(items)} 条")
            for a in items:
                print("  " + _show(a))
        print(f"AI 调用 {svc.analyzer.usage['calls']} 次，费用 ¥{svc.analyzer.usage['cost']:.4f}")
    elif args.cmd == "analyze":
        print(_show(svc.analyze_record(args.rid, force=args.force)))
    elif args.cmd == "profile":
        print(json.dumps(svc.profile(args.uid), ensure_ascii=False, indent=2))
    elif args.cmd == "report":
        text = svc.report(args.uid)
        print(text)
        if args.send:
            from hydroai.oj.notify import Notifier
            Notifier(svc.api).send(args.to or args.uid, text)
            print(f"\n已私信发送给 uid={args.to or args.uid}")
    elif args.cmd == "class":
        print(svc.class_report())
    return 0


def main(argv: list[str] | None = None) -> int:
    from hydroai.config.io import load_dotenv
    from hydroai.config.manager import ConfigManager
    from hydroai.insight.service import InsightService
    load_dotenv()
    args = _parser().parse_args(argv)
    config = ConfigManager()
    api = OJApi.from_config(config, args.domain)
    if not api.login():
        print("登录失败", file=sys.stderr)
        return 1
    try:
        return run(InsightService(api, config), args)
    except OJError as exc:
        print(f"OJ 错误: {exc}", file=sys.stderr)
        return 1
