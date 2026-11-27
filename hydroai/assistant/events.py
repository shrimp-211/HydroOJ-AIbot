"""把收件箱里的系统 @ 通知转成 :class:`MentionEvent`。"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit
from dataclasses import dataclass

from hydroai.oj.api import OJApi, OJError
from hydroai.oj.mentions import Link, SystemNotice, parse_link, parse_system_message

log = logging.getLogger(__name__)


@dataclass
class MentionEvent:
    msg_id: str
    author_name: str
    link: Link
    author_uid: int = 0        # 提问者；推断失败为 0
    manual: bool = False       # 手工模拟（cli handle）

    @property
    def domain(self) -> str:
        return self.link.domain

    @property
    def did(self) -> str:
        return self.link.ident if self.link.kind == "discuss" else ""


def parse_event(msg: dict) -> MentionEvent | None:
    """解析系统通知；不是 @ 通知或认不出链接返回 ``None``（调用方记日志后忽略）。"""
    notice: SystemNotice | None = parse_system_message(msg)
    if notice is None or not notice.is_mention:
        return None
    if notice.link is None:
        log.info("[助手] @ 通知里没有可识别的链接: %s", notice.params)
        return None
    return MentionEvent(notice.msg_id, notice.author, notice.link)


def event_for_discussion(did_or_url: str, domain: str, author_uid: int) -> MentionEvent:
    """手工构造事件（调试用）。"""
    link = parse_link(did_or_url) if "/" in did_or_url else None
    if link is None and re.fullmatch(r"[0-9a-f]{24}", did_or_url):
        link = Link("discuss", domain, did_or_url)
    if link is None or link.kind != "discuss" or author_uid <= 1:
        raise ValueError("需要有效的讨论链接或 ID，以及真实提问者 UID")
    return MentionEvent(f"manual-{link.ident}", "", link, author_uid, manual=True)


def same_site(url: str, root: str) -> bool:
    if not url:
        return True
    try:
        target, origin = urlsplit(url), urlsplit(root)
    except ValueError:
        return False
    if not target.scheme and not target.netloc:
        return url.startswith("/") and not url.startswith("//")
    return (target.scheme, target.netloc.lower()) == (origin.scheme, origin.netloc.lower())


def resolve_author(api: OJApi, event: MentionEvent, discussion: dict) -> int:
    """优先用通知用户名与讨论参与者匹配，不把其他人的较新 @ 当作触发者。"""
    if event.manual:
        return event.author_uid
    if not event.author_name:
        return 0
    for uid, udoc in (discussion.get("udict") or {}).items():
        if str(udoc.get("uname", "")).casefold() == event.author_name.casefold():
            return int(uid)
    try:
        return api.users.resolve_uid(event.author_name)
    except OJError:
        return 0
