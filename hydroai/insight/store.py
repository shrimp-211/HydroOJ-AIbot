"""学情长期记录库（SQLite，线程安全，``CREATE IF NOT EXISTS`` 式迁移）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    domain TEXT, uid INTEGER, uname TEXT, updated_at REAL, PRIMARY KEY (domain, uid));
CREATE TABLE IF NOT EXISTS submissions (
    rid TEXT PRIMARY KEY, domain TEXT, uid INTEGER, pid TEXT, status INTEGER, score REAL,
    lang TEXT, submit_at REAL, code_hash TEXT, code TEXT);
CREATE INDEX IF NOT EXISTS ix_sub_user ON submissions (domain, uid, pid);
CREATE TABLE IF NOT EXISTS analyses (
    rid TEXT PRIMARY KEY, domain TEXT, uid INTEGER, pid TEXT, data TEXT, model TEXT,
    created_at REAL);
CREATE INDEX IF NOT EXISTS ix_ana_user ON analyses (domain, uid);
CREATE TABLE IF NOT EXISTS skill_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, uid INTEGER, tag TEXT, rid TEXT,
    error_type TEXT, distance REAL, solved INTEGER, at REAL);
CREATE INDEX IF NOT EXISTS ix_ev_user ON skill_events (domain, uid, tag);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def submit_time(rdoc: dict) -> float:
    """记录的提交时间：ObjectId 前 4 字节为 Unix 时间戳；退化到 judgeAt。"""
    try:
        return float(int(str(rdoc.get("_id", ""))[:8], 16))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(str(rdoc.get("judgeAt", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class InsightStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)",
                             (str(SCHEMA_VERSION),))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()) -> None:
        with self._lock:
            self._db.execute(sql, args)
            self._db.commit()

    # ---- 学生 / 提交缓存 ----
    def upsert_student(self, domain: str, uid: int, uname: str = "") -> None:
        self._x("INSERT INTO students VALUES (?,?,?,?) ON CONFLICT(domain, uid) DO UPDATE "
                "SET uname=CASE WHEN excluded.uname!='' THEN excluded.uname ELSE uname END, "
                "updated_at=excluded.updated_at", (domain, int(uid), uname, time.time()))

    def student_name(self, domain: str, uid: int) -> str:
        rows = self._q("SELECT uname FROM students WHERE domain=? AND uid=?", (domain, int(uid)))
        return rows[0]["uname"] if rows else ""

    def students(self, domain: str) -> list[int]:
        return [r["uid"] for r in self._q("SELECT uid FROM students WHERE domain=?", (domain,))]

    def upsert_submission(self, domain: str, rdoc: dict, code: str | None = None,
                          code_hash: str = "") -> None:
        """缓存记录元数据；``code`` 为 None 时不覆盖已缓存的代码。"""
        row = (str(rdoc.get("_id")), domain, int(rdoc.get("uid", 0)), str(rdoc.get("pid", "")),
               int(rdoc.get("status", 0) or 0), float(rdoc.get("score", 0) or 0),
               rdoc.get("lang", ""), submit_time(rdoc), code_hash, code)
        self._x("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rid) DO "
                "UPDATE SET status=excluded.status, score=excluded.score, "
                "code_hash=CASE WHEN excluded.code_hash!='' THEN excluded.code_hash "
                "ELSE code_hash END, code=COALESCE(excluded.code, code) "
                "WHERE submissions.domain=excluded.domain AND submissions.uid=excluded.uid "
                "AND submissions.pid=excluded.pid", row)

    def submission(self, rid: str) -> dict | None:
        rows = self._q("SELECT * FROM submissions WHERE rid=?", (rid,))
        return dict(rows[0]) if rows else None

    def submissions(self, domain: str, uid: int, pid=None) -> list[dict]:
        sql, args = "SELECT * FROM submissions WHERE domain=? AND uid=?", [domain, int(uid)]
        if pid is not None:
            sql += " AND pid=?"
            args.append(str(pid))
        return [dict(r) for r in self._q(sql + " ORDER BY submit_at", args)]

    # ---- 分析 ----
    def has_analysis(self, rid: str) -> bool:
        return bool(self._q("SELECT 1 FROM analyses WHERE rid=?", (rid,)))

    def analyzed_rids(self, domain: str, uid: int) -> set[str]:
        rows = self._q("SELECT rid, data FROM analyses WHERE domain=? AND uid=?", (domain, int(uid)))
        return {r["rid"] for r in rows if (lambda a: a.get("ai_used") or a.get("status") == 1)(json.loads(r["data"]))}

    def save_analysis(self, domain: str, analysis: dict, model: str = "") -> None:
        """写入分析并重新生成该记录对应的 skill_events（重复分析不会重复计数）。"""
        rid, uid = analysis["rid"], int(analysis["uid"])
        now = time.time()
        with self._lock:
            existing = self._db.execute("SELECT domain, uid, pid FROM analyses WHERE rid=?", (rid,)).fetchone()
            if existing and (existing["domain"] != domain or existing["uid"] != uid or
                             existing["pid"] != str(analysis["pid"])):
                raise ValueError("提交 ID 已属于其他域、学生或题目，拒绝覆盖")
            self._db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?,?,?,?)",
                             (rid, domain, uid, str(analysis["pid"]),
                              json.dumps(analysis, ensure_ascii=False), model, now))
            self._db.execute("DELETE FROM skill_events WHERE rid=?", (rid,))
            solved = int(analysis.get("status") == 1)
            for tag in analysis.get("knowledge_tags") or ["未分类"]:
                self._db.execute(
                    "INSERT INTO skill_events (domain, uid, tag, rid, error_type, distance, "
                    "solved, at) VALUES (?,?,?,?,?,?,?,?)",
                    (domain, uid, tag, rid, analysis.get("error_type", ""),
                     analysis.get("distance"), solved, now))
            self._db.commit()

    def get_analysis(self, rid: str, domain: str | None = None) -> dict | None:
        if domain is None:
            rows = self._q("SELECT data FROM analyses WHERE rid=?", (str(rid),))
        else:
            rows = self._q("SELECT data FROM analyses WHERE rid=? AND domain=?", (str(rid), domain))
        return json.loads(rows[0]["data"]) if rows else None

    def analyses(self, domain: str, uid: int, pid=None) -> list[dict]:
        sql, args = "SELECT data, created_at FROM analyses WHERE domain=? AND uid=?", [domain, int(uid)]
        if pid is not None:
            sql += " AND pid=?"
            args.append(str(pid))
        out = []
        for r in self._q(sql + " ORDER BY created_at", args):
            d = json.loads(r["data"])
            d["_at"] = r["created_at"]
            out.append(d)
        return out

    def events(self, domain: str, uid: int) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT * FROM skill_events WHERE domain=? AND uid=? ORDER BY at", (domain, int(uid)))]
