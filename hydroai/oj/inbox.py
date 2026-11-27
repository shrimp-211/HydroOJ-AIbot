"""收件箱轮询：持久化去重 + 处理器分发。

助手模块用它监听系统 @ 通知；守护进程的指令后端仍在 ``monitor.messaging``，
两者互不干扰（后者忽略 uid=1 的系统消息）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

from hydroai.oj.api import OJApi, OJError
from hydroai.oj.mentions import msg_time

log = logging.getLogger(__name__)


class SeenStore:
    """已处理消息 id 的 SQLite 集合（进程重启后不重复处理）。"""

    def __init__(self, path: str | Path, namespace: str):
        self.ns = namespace
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS seen (ns TEXT, id TEXT, at REAL, "
                         "PRIMARY KEY (ns, id))")
        self._db.commit()

    def has(self, msg_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM seen WHERE ns=? AND id=?",
                                    (self.ns, msg_id)).fetchone() is not None

    def add(self, msg_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)",
                                    (self.ns, msg_id, time.time())).rowcount == 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._db.close()


class InboxPoller:
    """轮询收件箱，把「别人发给我、尚未处理」的消息交给 ``handler(msg)``。

    ``max_age``：忽略早于该秒数的历史消息，首次启动不会回放旧通知。
    handler 抛异常时消息仍记为已处理，避免坏消息无限重试；需要重试请在 handler 内处理。
    """

    def __init__(self, api: OJApi, handler: Callable[[dict], None], seen: SeenStore, *,
                 interval: float = 10, max_age: float = 3600,
                 accept: Callable[[dict], bool] | None = None):
        self.api, self.handler, self.seen = api, handler, seen
        self.interval, self.max_age = interval, max_age
        self.accept = accept or (lambda m: True)
        self._stop = threading.Event()

    def poll_once(self) -> int:
        handled = 0
        me = self.api.uid
        now = time.time()
        for msg in self.api.messages.inbox():
            mid = msg.get("_id", "")
            if not mid or int(msg.get("from", 0) or 0) == me or self.seen.has(mid):
                continue
            if not self.accept(msg):
                continue
            if self.max_age and now - msg_time(mid) > self.max_age:
                self.seen.add(mid)  # 过期历史消息：标记后跳过
                continue
            if not self.seen.add(mid):
                continue  # 并发进程中，只有已抢占该 ID 的进程能回复。
            try:
                self.handler(msg)
            except Exception:
                log.exception("[收件箱] 处理消息 %s 失败", mid)
            handled += 1
        return handled

    def run(self) -> None:
        log.info("[收件箱] 开始轮询，间隔 %ss", self.interval)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except OJError as exc:
                log.warning("[收件箱] 拉取失败: %s", exc)
            except Exception:
                log.exception("[收件箱] 轮询异常")
            self._stop.wait(self.interval)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="inbox-poller")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
