"""统一发送出口：隔离私信上下文，校验数量、长度与内容。"""
from __future__ import annotations

import re

from hydroai.assistant.prompts import SIGNATURE
from hydroai.oj.notify import MAX_MESSAGE
from hydroai.oj.resources import mention

DISCUSS_LIMIT = 3500
_FENCE = re.compile(r"(`{3,}|~{3,})[^\n]*\n(.*?)(?:\1|\Z)", re.S)
_PROGRAM = re.compile(r"\bmain\s*\(|__name__\s*==|^\s*#include\s*[<\"]|\bpublic\s+class\s+Main\b", re.M)
_EXTRA_MENTIONS = re.compile(r"@\[[^\]\r\n]*\]\([^\)\r\n]*\)")


def has_full_code(text: str) -> bool:
    """拦截常见完整程序形态；短小的伪代码和片段仍可使用。"""
    return bool(_PROGRAM.search(text)) or any(m.group(2).count("\n") >= 8 for m in _FENCE.finditer(text))


class Outbox:
    def __init__(self, ctx, notifier, *, reveal_code: bool, dry_run: bool, thread_budget: int):
        self.api, self.notifier = ctx.api, notifier
        self.did, self.author_uid, self.private = ctx.event.did, ctx.author_uid, ctx.private
        self.reveal_code, self.dry_run = reveal_code, dry_run
        self.thread_budget = thread_budget
        self.sent: list[dict] = []
        self.solution_url = ""

    @property
    def replies(self) -> int:
        return sum(entry["kind"] != "private" for entry in self.sent)

    def _guard(self, content: str, limit: int) -> str:
        if not content.strip():
            return "内容为空"
        if len(content) > limit - 150:
            return "内容过长，请精简后重新发送"
        if not self.reveal_code and has_full_code(content):
            return "请只给关键代码片段和讲解，不要给完整程序"
        if _EXTRA_MENTIONS.search(content):
            return "请移除文中的 @，系统会自动 @ 提问者"
        return ""

    def _emit(self, kind: str, target, text: str, action) -> str:
        if self.dry_run:
            print(f"\n[dry-run:{kind}→{target}]\n{text}\n", flush=True)
        elif not action():
            return "发送失败，OJ 未确认送达"
        self.sent.append({"kind": kind, "target": target, "text": text})
        return "已发送（仅预览）" if self.dry_run else "已发送"

    def _public_error(self, content: str, *, solution: bool = False) -> str:
        if self.private:
            return "本次答疑只能私信本人"
        if self.replies >= self.thread_budget:
            return "本讨论自动回复已达上限"
        kinds = ("solution_reply",) if solution else ("reply", "tail_reply")
        if any(s["kind"] in kinds for s in self.sent):
            return "本次通知已在这里回复"
        return self._guard(content, DISCUSS_LIMIT)

    def reply_discussion(self, content: str, *, drid: str = "") -> str:
        if not self.did:
            return "没有关联讨论"
        if self.solution_url and self.solution_url not in content:
            content += f"\n\n[查看题解区的补充讲解]({self.solution_url})"
        if err := self._public_error(content):
            return err
        text = f"{mention(self.author_uid, '')} {content}\n\n{SIGNATURE}"
        if drid:
            return self._emit("tail_reply", drid, text,
                              lambda: self.api.discussions.tail_reply(self.did, drid, text))
        return self._emit("reply", self.did, text, lambda: self.api.discussions.reply(self.did, text))

    def reply_in_thread(self, drid: str, content: str) -> str:
        return self.reply_discussion(content, drid=drid)

    def send_private(self, uid: int, content: str) -> str:
        if uid <= 1 or uid != self.author_uid:
            return "只能私信提问者本人"
        if any(s["kind"] == "private" for s in self.sent):
            return "本次通知已发过私信"
        if err := self._guard(content, MAX_MESSAGE):
            return err
        text = f"{content}\n\n{SIGNATURE}"
        return self._emit("private", uid, text, lambda: self.notifier.send(uid, text))

    def post_solution_reply(self, pid, psid: str, content: str) -> str:
        if self.sent:
            return "请先在题解下补充，再到讨论中附上链接"
        if self.thread_budget < 2:
            return "回复额度不足，请直接在讨论里附上已有题解链接"
        if err := self._public_error(content, solution=True):
            return err
        text = f"{content}\n\n{SIGNATURE}"
        return self._emit("solution_reply", psid, text, lambda: self.api.solutions.reply(pid, psid, text))
