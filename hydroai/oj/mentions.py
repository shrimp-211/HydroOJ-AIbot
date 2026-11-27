"""解析 Hydro 系统消息（uid=1 发来的 @ 提醒等）与站内链接。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

SYSTEM_UID = 1
_ID = r"[A-Za-z0-9_\-][A-Za-z0-9_\-.]*"
_OID = r"[0-9a-f]{24}"
_LINKS = [
    ("discuss", re.compile(rf"(?:/d/({_ID}))?/discuss/({_OID})(?:/{_OID})?")),
    ("solution", re.compile(rf"(?:/d/({_ID}))?/p/({_ID})/solution")),
    ("record", re.compile(rf"(?:/d/({_ID}))?/record/({_OID})")),
    ("problem", re.compile(rf"(?:/d/({_ID}))?/p/({_ID})")),
    ("contest", re.compile(rf"(?:/d/({_ID}))?/contest/({_ID})")),
]
_LINK_TOKEN = re.compile(r"(?:https?://[^\s/<>)\]]+)?/(?:d/[^\s/]+/)?(?:discuss|p|record|contest)/[^\s<>\])，。；、]+")


@dataclass
class Link:
    kind: str          # discuss / solution / record / problem / contest
    domain: str        # 链接里没有域时为 system
    ident: str         # discuss/record/contest 为 24 位 id；problem/solution 为 pid
    url: str = ""


def parse_link(url: str) -> Link | None:
    """把站内链接（相对或绝对）解析成 :class:`Link`；认不出返回 ``None``。"""
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("", "http", "https") or (parsed.netloc and not parsed.scheme):
        return None
    path = parsed.path.rstrip("/")
    for kind, pattern in _LINKS:
        m = pattern.fullmatch(path)
        if m:
            return Link(kind, m.group(1) or "system", m.group(2), url)
    return None


def find_links(text: str) -> list[Link]:
    """从一段文字里找出所有站内链接（私信里可能夹带绝对地址）。"""
    out = []
    for token in _LINK_TOKEN.findall(text or ""):
        link = parse_link(token)
        if link:
            out.append(link)
    return out


@dataclass
class SystemNotice:
    """一条系统通知：``template`` 是 Hydro 的 i18n 模板，``params`` 为参数。"""
    msg_id: str
    template: str
    params: list = field(default_factory=list)
    is_mention: bool = False
    author: str = ""         # 触发者用户名（@ 你的人）
    link: Link | None = None


def parse_system_message(msg: dict) -> SystemNotice | None:
    """解析 ``from == 1`` 的 JSON 消息；不是系统 JSON 通知返回 ``None``。"""
    if msg.get("from") not in (SYSTEM_UID, str(SYSTEM_UID)):
        return None
    try:
        body = json.loads(msg.get("content", ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("message"), str):
        return None
    template, params = body["message"], body.get("params", [])
    if not isinstance(params, list):
        return None
    notice = SystemNotice(msg.get("_id", ""), template, params)
    notice.is_mention = template == "User {0} mentioned you in {1:link}"
    if params:
        notice.author = str(params[0])
    for p in params:
        link = parse_link(str(p))
        if link:
            notice.link = link
            break
    return notice


def msg_time(msg_id: str) -> float:
    """消息 _id 是 ObjectId，前 4 字节为 Unix 时间戳。"""
    try:
        return float(int(str(msg_id)[:8], 16))
    except ValueError:
        return 0.0
