"""处理记录（SQLite）：去重、限频、排错。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class AssistantStore:
    def __init__(self, path: str | Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "msg_id TEXT, domain TEXT, did TEXT, author TEXT, status TEXT, replies INTEGER, "
            "dry_run INTEGER, tools TEXT, elapsed REAL, cost REAL, error TEXT, at REAL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS runs_thread ON runs (domain, did)")
        self._db.commit()

    def record(self, *, msg_id: str, domain: str, did: str, author: str, status: str,
               replies: int = 0, dry_run: bool = False, tools: list | None = None,
               elapsed: float = 0.0, cost: float = 0.0, error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (msg_id,domain,did,author,status,replies,dry_run,tools,"
                "elapsed,cost,error,at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg_id, domain, did, author, status, replies, int(dry_run),
                 json.dumps(tools or [], ensure_ascii=False), elapsed, cost, error[:500],
                 time.time()))
            self._db.commit()

    def thread_replies(self, domain: str, did: str) -> int:
        """该讨论里已真实发出的自动回复条数（dry-run 不计）。"""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(replies),0) FROM runs WHERE domain=? AND did=? AND dry_run=0",
                (domain, did)).fetchone()
        return int(row[0])

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT msg_id,domain,did,author,status,replies,dry_run,elapsed,cost,error,at "
                "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
