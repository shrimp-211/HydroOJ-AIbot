"""通知：私信推送、事件开关、长文本分段。"""
from __future__ import annotations

import logging

from hydroai.oj.api import OJApi, OJError

log = logging.getLogger(__name__)
MAX_MESSAGE = 1800  # 单条私信安全长度，超出自动分段


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """按段落切分，尽量不在代码块/句子中间断开。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts, cur = [], ""
    for block in text.split("\n\n"):
        if len(block) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts += [block[i:i + limit] for i in range(0, len(block), limit)]
        elif len(cur) + len(block) + 2 > limit:
            parts.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    if cur:
        parts.append(cur)
    return parts


class Notifier:
    """向用户发私信；``push_list`` 为事件广播名单，``events`` 控制各事件是否推送。"""

    def __init__(self, api: OJApi, push_list=(), events: dict | None = None):
        self.api = api
        self.push_list = [int(u) for u in push_list]
        self.events = events or {}

    def send(self, uid: int, text: str) -> bool:
        ok = True
        for part in split_text(text):
            try:
                if self.api.messages.send(uid, part) is False:
                    ok = False
            except OJError as exc:
                log.warning("[通知] 发送给 %s 失败: %s", uid, exc)
                ok = False
        return ok

    def broadcast(self, event: str, text: str) -> int:
        """按事件开关向推送名单广播，返回成功人数。默认开启未配置的事件。"""
        if not self.events.get(event, True):
            return 0
        return sum(1 for uid in self.push_list if self.send(uid, text))
