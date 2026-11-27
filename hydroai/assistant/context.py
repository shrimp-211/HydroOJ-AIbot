"""为一次 @ 构建 Agent 上下文：讨论、题目、提问者的提交、已有题解、画像。

所有来自 OJ 的文本都视为不可信数据，渲染时放入 :func:`prompts.fence` 围栏。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from hydroai.assistant.events import MentionEvent, resolve_author, same_site
from hydroai.assistant.prompts import fence
from hydroai.oj.api import OJApi, OJError
from hydroai.oj.mentions import find_links
from hydroai.oj.resources import STATUS_AC, status_name

log = logging.getLogger(__name__)
PROBLEM_NODE = 10


@dataclass
class Context:
    event: MentionEvent
    api: OJApi
    author_uid: int = 0
    author_name: str = ""
    discussion: dict = field(default_factory=dict)
    pid: str = ""
    problem: dict | None = None
    student: dict | None = None
    solutions: list[dict] = field(default_factory=list)
    names: dict = field(default_factory=dict)
    text: str = ""
    private: bool = False
    internal_pid: str = ""


def make_insight(api: OJApi, config):
    """尝试创建 InsightService；模块不可用时返回 ``None``，调用方走降级路径。"""
    try:
        from hydroai.insight import InsightService
        return InsightService(api, config)
    except Exception as exc:  # ImportError 或其初始化失败
        log.info("[助手] insight 不可用，使用简化提交查询: %s", type(exc).__name__)
        return None


def fallback_student_context(api: OJApi, uid: int, pid) -> dict:
    """insight 不可用时：取最后一条非 CE 提交（没有就取最后一条）。"""
    out = {"uid": uid, "pid": pid, "best_submission": None, "attempts": 0,
           "solved": False, "analysis": None, "profile_summary": ""}
    try:
        rdocs = api.records.list(uid=uid, pid=pid).get("rdocs", [])
    except OJError:
        return out
    out["attempts"] = len(rdocs)
    out["solved"] = any(r.get("status") == STATUS_AC for r in rdocs)
    pick = next((r for r in rdocs if r.get("status") != 7), rdocs[0] if rdocs else None)
    if pick:
        try:
            rdoc = api.records.get(pick["_id"])["rdoc"]
            if str(rdoc.get("uid")) != str(uid) or str(rdoc.get("pid")) != str(pid) or rdoc.get("domainId") != api.domain:
                return out
            out["best_submission"] = {
                "rid": rdoc["_id"], "status": rdoc.get("status"),
                "status_name": status_name(rdoc.get("status")), "score": rdoc.get("score"),
                "lang": rdoc.get("lang"), "code": rdoc.get("code", ""),
                "submit_at": rdoc.get("judgeAt", "")}
        except OJError:
            pass
    return out


def student_context(api: OJApi, insight, uid: int, pid) -> dict | None:
    if not uid or not pid:
        return None
    if insight is not None:
        try:
            return insight.student_context(uid, pid, analyze=False)
        except Exception as exc:
            log.warning("[助手] insight.student_context 失败，改用简化查询: %s", type(exc).__name__)
    return fallback_student_context(api, uid, pid)


def solution_overview(api: OJApi, pid, limit: int = 10) -> list[dict]:
    try:
        data = api.solutions.list(pid)
    except OJError:
        return []
    udict = data.get("udict", {})
    out = []
    for s in data.get("psdocs", [])[:limit]:
        owner = s.get("owner")
        out.append({"psid": s["_id"], "owner": (udict.get(str(owner)) or {}).get("uname", owner),
                    "chars": len(s.get("content") or ""), "content": (s.get("content") or "")[:4800],
                    "has_code": "```" in (s.get("content") or ""),
                    "url": api.solutions.url(pid)})
    return out


def number_lines(code: str, limit: int = 200) -> str:
    lines = (code or "").splitlines()
    shown = "\n".join(f"{i:>3}| {l}" for i, l in enumerate(lines[:limit], 1))
    return shown + (f"\n…（共 {len(lines)} 行）" if len(lines) > limit else "")


def load_event(base_api: OJApi, event: MentionEvent) -> Context:
    """先读取讨论和身份，权限闸门通过后才补充任何学生数据。"""
    api = base_api.with_domain(event.domain)
    ctx = Context(event, api)
    link = event.link
    if event.did:
        ctx.discussion = api.discussions.get_full(event.did)
        ddoc = ctx.discussion["ddoc"]
        if not ddoc or ddoc.get("domainId") != api.domain:
            raise OJError("讨论不在当前域")
        ctx.names = {int(k): v.get("uname", str(k)) for k, v in ctx.discussion["udict"].items()}
        if ddoc.get("parentType") == PROBLEM_NODE and ddoc.get("parentId"):
            ctx.pid = str(ddoc["parentId"])
    ctx.author_uid = resolve_author(api, event, ctx.discussion)
    ctx.author_name = ctx.names.get(ctx.author_uid) or event.author_name
    return ctx


def _author_posts(ctx: Context):
    if not ctx.discussion:
        return
    ddoc = ctx.discussion["ddoc"]
    posts = [ddoc]
    for reply in ctx.discussion.get("drdocs", []):
        posts.append(reply)
        posts.extend(reply.get("reply") or [])
    yield from (p.get("content", "") for p in reversed(posts)
                if str(p.get("owner")) == str(ctx.author_uid))


def _related_links(ctx: Context):
    if not ctx.event.did:
        yield ctx.event.link
    for text in _author_posts(ctx):
        for link in find_links(text):
            if link.domain == ctx.api.domain and same_site(link.url, ctx.api.root):
                yield link


def _load_linked_record(ctx: Context, links: list) -> dict | None:
    for link in (link for link in links if link.kind == "record"):
        try:
            record = ctx.api.records.get(link.ident)["rdoc"]
        except OJError:
            continue
        if record.get("domainId") == ctx.api.domain and str(record.get("uid")) == str(ctx.author_uid):
            return record
    return None


def enrich_context(ctx: Context, insight=None) -> None:
    api = ctx.api
    links = list(_related_links(ctx))[:10]
    record = _load_linked_record(ctx, links)
    if not ctx.pid:
        ctx.pid = (str(record["pid"]) if record else
                   next((link.ident for link in links if link.kind in ("problem", "solution")), ""))
    ctx.private = not ctx.event.did or bool(ctx.discussion.get("ddoc", {}).get("hidden"))
    if ctx.pid:
        try:
            ctx.problem = api.problems.detail(ctx.pid)
            ctx.internal_pid = str(ctx.problem.get("docId") or ctx.pid)
            ctx.private |= bool((ctx.problem.get("raw") or {}).get("hidden"))
        except OJError:
            # 无法核实题面可见性时，不公开题目相关上下文。
            ctx.private = True
        if ctx.problem is not None:
            ctx.solutions = solution_overview(api, ctx.pid)
            student = student_context(api, insight, ctx.author_uid, ctx.internal_pid) or {}
            if record and str(record.get("pid")) == ctx.internal_pid:
                student["best_submission"] = {"rid": record["_id"], "status_name": status_name(record.get("status")),
                                              "score": record.get("score"), "code": record.get("code", ""),
                                              "lang": record.get("lang")}
                student["analysis"] = None  # 代表提交的旧诊断不适用于这条指定记录
            if student.get("best_submission"):
                ctx.private = True
                ctx.student = student
    ctx.text = render(ctx)


def render_discussion(ctx: Context) -> str:
    d = ctx.discussion
    ddoc = d.get("ddoc", {})
    name = lambda uid: ctx.names.get(int(uid or 0), str(uid))  # noqa: E731
    lines = [f"标题: {ddoc.get('title', '')}  发帖人: {name(ddoc.get('owner'))}",
             f"主帖:\n{ddoc.get('content', '')}"]
    for r in d.get("drdocs", []):
        lines.append(f"\n[回复 {r['_id']}] {name(r.get('owner'))}: {r.get('content', '')}")
        for t in r.get("reply", []):
            lines.append(f"    ↳ {name(t.get('owner'))}: {t.get('content', '')}")
    return fence("讨论", "\n".join(lines), 8000)


def render(ctx: Context) -> str:
    ev = ctx.event
    parts = [f"## 本次 @\n域: {ev.domain}  提问者 UID: {ctx.author_uid}"
             f"  链接类型: {ev.link.kind}  对象: {ev.link.ident}"
             f"\n你自己的 uid: {ctx.api.uid}（讨论里你的发言不需要再回应）"]
    if ctx.discussion:
        recent = list(_author_posts(ctx))[:3]
        if recent:
            parts.append(fence("提问者最近发言（从新到旧）", "\n\n".join(recent), 3000))
        parts.append("## 讨论\n" + render_discussion(ctx))
    if ctx.problem:
        p = ctx.problem
        cfg = p.get("config") or {}
        limits = f"时限 {cfg.get('timeMin', '?')}ms 内存 {cfg.get('memoryMin', '?')}MB"
        parts.append(f"## 题目 P{ctx.pid}  {limits}\n"
                     + fence("题面", f"{p['title']}\n标签：{p['tags']}\n{p['content']}", 4500))
    if ctx.student:
        s = ctx.student
        sub = s.get("best_submission")
        head = (f"## 提问者在该题的提交情况\n提交次数 {s.get('attempts', 0)}，"
                f"{'已通过' if s.get('solved') else '尚未通过'}")
        if sub:
            head += (f"\n选取的提交 rid={sub['rid']} 结果={sub.get('status_name')} "
                     f"得分={sub.get('score')} 语言={sub.get('lang')}\n"
                     + fence("学生代码(带行号)", number_lines(sub.get("code", "")), 6000))
        else:
            head += "\n（没有可用提交）"
        if s.get("analysis"):
            head += "\n" + fence("已有分析", s["analysis"], 1200)
        parts.append(head)
    elif ctx.pid:
        parts.append("## 提问者在该题的提交情况\n（暂无可用代码）")
    if ctx.pid:
        if ctx.solutions:
            rows = "\n".join(f"- psid={s['psid']} {s['chars']}字"
                             f"{' 含代码' if s['has_code'] else ''} {s['url']}" for s in ctx.solutions)
            parts.append(f"## 该题已有题解\n{rows}")
        else:
            parts.append(f"## 该题已有题解\n无（题解区: {ctx.api.solutions.url(ctx.pid)}）")
    return "\n\n".join(parts)
