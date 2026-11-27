"""ojkit 命令行：直接调用 OJ 能力，便于排查与脚本化。

    hydro.py ojkit me
    hydro.py ojkit --domain root_test problem 26
    hydro.py ojkit --domain root_test records --uid 35 --pid 26
    hydro.py ojkit --domain root_test discuss 6b08...   # 查看讨论
    hydro.py ojkit --domain root_test reply <did> "内容"
    hydro.py ojkit send 118 "私信内容"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hydroai.oj.api import OJApi, OJError
from hydroai.oj.resources import status_name


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hydro.py ojkit", description="OJ 交互工具")
    p.add_argument("--domain", help="域 ID（默认取 oj_base）")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("me", help="当前登录用户")
    s = sub.add_parser("problems", help="题目列表"); s.add_argument("-q", default=""); s.add_argument("--page", type=int, default=1)
    s = sub.add_parser("problem", help="题目详情"); s.add_argument("pid")
    s = sub.add_parser("records", help="提交记录")
    for a in ("--uid", "--pid", "--status", "--lang", "--tid"):
        s.add_argument(a)
    s.add_argument("--page", type=int, default=1)
    s = sub.add_parser("record", help="单条记录（含代码）"); s.add_argument("rid")
    s = sub.add_parser("discuss", help="查看讨论"); s.add_argument("did")
    s = sub.add_parser("discussions", help="讨论列表"); s.add_argument("--page", type=int, default=1)
    s = sub.add_parser("post", help="发帖"); s.add_argument("pid"); s.add_argument("title"); s.add_argument("content")
    s = sub.add_parser("reply", help="回复讨论"); s.add_argument("did"); s.add_argument("content")
    s = sub.add_parser("solutions", help="题解列表"); s.add_argument("pid")
    s = sub.add_parser("send", help="发私信"); s.add_argument("uid", type=int); s.add_argument("text")
    sub.add_parser("inbox", help="收件箱")
    s = sub.add_parser("contests", help="比赛列表"); s.add_argument("--page", type=int, default=1)
    for name, label in (("trainings", "训练列表"), ("homework", "作业列表"), ("ranking", "域排名")):
        sub.add_parser(name, help=label).add_argument("--page", type=int, default=1)
    sub.add_parser("scoreboard", help="比赛排行榜").add_argument("tid")
    sub.add_parser("user", help="读取用户资料").add_argument("who", help="UID 或用户名")
    s = sub.add_parser("submit", help="将本地代码文件提交给 OJ")
    s.add_argument("pid")
    s.add_argument("file", type=Path)
    s.add_argument("--lang", required=True, help="Hydro 语言 ID，例如 cc.cc14o2")
    s.add_argument("--tid", default="", help="比赛 ID")
    s = sub.add_parser("publish", help="从 Markdown 文件发布题解")
    s.add_argument("pid")
    s.add_argument("file", type=Path)
    return p


def _out(args, data, text: str = "") -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json or not text else text)


def run(api: OJApi, args) -> int:
    c = args.cmd
    if c == "me":
        _out(args, api.me, f"uid={api.uid} uname={api.me.get('uname')}")
    elif c == "problems":
        d = api.problems.list(args.page, args.q)
        _out(args, d, "\n".join(f"{p.get('pid') or p.get('docId'):>8}  {p.get('title')}  "
                                f"{p.get('nAccept', 0)}/{p.get('nSubmit', 0)}" for p in d.get("pdocs", [])))
    elif c == "problem":
        d = api.problems.detail(args.pid)
        _out(args, {k: v for k, v in d.items() if k != "raw"}, f"{d['title']}\n\n{d['content']}")
    elif c == "records":
        d = api.records.list(uid=args.uid, pid=args.pid, status=args.status, lang=args.lang or "",
                             tid=args.tid or "", page=args.page)
        _out(args, d, "\n".join(f"{r['_id']}  uid={r.get('uid')} pid={r.get('pid')} "
                                f"{r['status_name']:>5} {r.get('score')}分" for r in d.get("rdocs", [])))
    elif c == "record":
        r = api.records.get(args.rid)["rdoc"]
        _out(args, r, f"{status_name(r.get('status'))} {r.get('score')}分 {r.get('lang')}\n\n{r.get('code', '')}")
    elif c == "discuss":
        d = api.discussions.get_full(args.did)
        names = {int(k): v.get("uname", k) for k, v in d["udict"].items()}
        lines = [f"# {d['ddoc'].get('title')}  (by {names.get(d['ddoc'].get('owner'))})", d["ddoc"].get("content", "")]
        for r in d["drdocs"]:
            lines.append(f"\n[{r['_id']}] {names.get(r.get('owner'), r.get('owner'))}: {r.get('content')}")
            lines += [f"    ↳ {names.get(t.get('owner'), t.get('owner'))}: {t.get('content')}" for t in r.get("reply", [])]
        _out(args, d, "\n".join(lines))
    elif c == "discussions":
        d = api.discussions.list(args.page)
        _out(args, d, "\n".join(f"{x['_id']}  {x.get('title')}  回复{x.get('nReply', 0)}" for x in d.get("ddocs", [])))
    elif c == "post":
        print(api.discussions.url(api.discussions.create(args.title, args.content, node=args.pid)))
    elif c == "reply":
        print("drid:", api.discussions.reply(args.did, args.content))
    elif c == "solutions":
        d = api.solutions.list(args.pid)
        _out(args, d, "\n".join(f"{x['_id']}  by {x.get('owner')}  {len(x.get('content', ''))}字" for x in d.get("psdocs", [])))
    elif c == "send":
        print("已发送" if api.messages.send(args.uid, args.text) else "失败")
    elif c == "inbox":
        msgs = api.messages.inbox()
        _out(args, msgs, "\n".join(f"{m['_id']} from={m['peer']}({m['peer_name']}): {m.get('content', '')[:80]}" for m in msgs[-30:]))
    elif c == "contests":
        d = api.contests.list(args.page)
        _out(args, d, "\n".join(f"{t['_id']}  {t.get('rule')}  {t.get('title')}" for t in d.get("tdocs", [])))
    elif c in ("trainings", "homework", "ranking"):
        operation = {"trainings": api.trainings.list, "homework": api.homework.list,
                     "ranking": api.users.ranking}[c]
        _out(args, operation(args.page))
    elif c == "scoreboard":
        _out(args, api.contests.scoreboard(args.tid))
    elif c == "user":
        uid = int(args.who) if args.who.isdecimal() else api.users.resolve_uid(args.who)
        if not uid:
            raise OJError("用户未找到", 404)
        _out(args, api.users.get(uid).get("udoc"))
    elif c == "submit":
        rid = api.records.submit(args.pid, args.file.read_text(encoding="utf-8"), args.lang, args.tid)
        print(api.path(f"/record/{rid}"))
    elif c == "publish":
        psid = api.solutions.post(args.pid, args.file.read_text(encoding="utf-8"))
        if not psid:
            raise OJError("OJ 未返回新题解 ID")
        print(api.solutions.url(args.pid))
    return 0


def main(argv: list[str] | None = None) -> int:
    from hydroai.config.manager import ConfigManager
    from hydroai.config.io import load_dotenv
    load_dotenv()
    args = _build_parser().parse_args(argv)
    api = OJApi.from_config(ConfigManager(), args.domain)
    if not api.login():
        print("登录失败", file=sys.stderr)
        return 1
    try:
        return run(api, args)
    except OJError as exc:
        print(f"OJ 错误: {exc}", file=sys.stderr)
        return 1
