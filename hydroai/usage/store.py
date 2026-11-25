"""逐请求用量账本（SQLite）。

仿照 cc-switch 的 Usage 面板：每一次 AI 请求都记录模型、供应商、任务、
输入/输出/缓存 token、费用、耗时与输出速度，供趋势、热力图、模型分布和
额度计算复用。写入失败只记日志，绝不阻断 AI 调用本身。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from hydroai.config.paths import data_file

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL    NOT NULL,
    day              TEXT    NOT NULL,
    hour             INTEGER NOT NULL,
    model            TEXT    NOT NULL DEFAULT '',
    provider         TEXT    NOT NULL DEFAULT '',
    task             TEXT    NOT NULL DEFAULT '',
    problem_id       TEXT    NOT NULL DEFAULT '',
    ok               INTEGER NOT NULL DEFAULT 1,
    stream           INTEGER NOT NULL DEFAULT 0,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens     INTEGER NOT NULL DEFAULT 0,
    cost             REAL    NOT NULL DEFAULT 0,
    cost_known       INTEGER NOT NULL DEFAULT 0,
    elapsed_s        REAL    NOT NULL DEFAULT 0,
    output_speed     REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_day ON requests(day);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider);
CREATE TABLE IF NOT EXISTS quota_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    quota_id   TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_ts ON quota_snapshots(ts);
"""

GROUP_FIELDS = {"model", "provider", "task", "day", "hour", "problem_id"}


def _int(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _model_filter(models) -> tuple[str, list]:
    """Return an SQL fragment and params for an optional model allow-list."""
    if not models:
        return "", []
    cleaned = [str(m) for m in models if isinstance(m, str) and m]
    if not cleaned:
        return "", []
    placeholders = ",".join("?" for _ in cleaned)
    return f" AND model IN ({placeholders})", cleaned


class UsageStore:
    """线程/进程安全的逐请求账本。每次操作使用独立连接，兼容多进程求解。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(data_file("usage.db"))
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init_error = ""

    # ── 基础设施 ──
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextmanager
    def _db(self):
        self._ensure()
        con = self._connect()
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _ensure(self):
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            try:
                con = self._connect()
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.execute("PRAGMA synchronous=NORMAL")
                    con.executescript(SCHEMA)
                    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    con.commit()
                finally:
                    con.close()
            except sqlite3.Error as exc:
                self._init_error = type(exc).__name__
                log.warning("[用量] 数据库初始化失败: %s", self._init_error)
            self._initialized = True

    @property
    def available(self) -> bool:
        self._ensure()
        return not self._init_error

    # ── 写入 ──
    def record(self, *, model="", provider="", task="", problem_id="", usage=None,
               cost=0.0, cost_known=False, elapsed_s=0.0, stream=False, ok=True,
               ts=None) -> bool:
        """记录一次请求。任何异常都被吞掉，返回 False，不影响主流程。"""
        usage = usage if isinstance(usage, dict) else {}
        timestamp = _float(ts if ts is not None else time.time()) or time.time()
        input_tokens = _int(usage.get("input"))
        output_tokens = _int(usage.get("output"))
        cache_hit = min(_int(usage.get("cache_hit")), input_tokens)
        total_tokens = _int(usage.get("total")) or (input_tokens + output_tokens)
        elapsed = _float(elapsed_s)
        speed = (output_tokens / elapsed) if elapsed > 0 and output_tokens else 0.0
        row = (
            timestamp, _local_day(timestamp), datetime.fromtimestamp(timestamp).hour,
            str(model or ""), str(provider or ""), str(task or ""), str(problem_id or ""),
            1 if ok else 0, 1 if stream else 0,
            input_tokens, output_tokens, cache_hit, total_tokens,
            _float(cost), 1 if cost_known else 0, elapsed, round(speed, 3),
        )
        try:
            with self._db() as con:
                con.execute(
                    "INSERT INTO requests (ts, day, hour, model, provider, task, problem_id,"
                    " ok, stream, input_tokens, output_tokens, cache_hit_tokens, total_tokens,"
                    " cost, cost_known, elapsed_s, output_speed)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            return True
        except sqlite3.Error as exc:
            log.warning("[用量] 写入失败: %s", type(exc).__name__)
            return False

    def save_quota_snapshot(self, quota_id: str, payload: dict, *, ts=None) -> bool:
        try:
            with self._db() as con:
                con.execute(
                    "INSERT INTO quota_snapshots (ts, quota_id, payload) VALUES (?,?,?)",
                    (_float(ts if ts is not None else time.time()) or time.time(),
                     str(quota_id), json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            return True
        except (sqlite3.Error, TypeError, ValueError):
            return False

    def last_quota_snapshot(self, quota_id: str):
        try:
            with self._db() as con:
                row = con.execute(
                    "SELECT ts, payload FROM quota_snapshots WHERE quota_id=?"
                    " ORDER BY ts DESC LIMIT 1", (str(quota_id),)).fetchone()
            if not row:
                return None
            return {"ts": row["ts"], "payload": json.loads(row["payload"])}
        except (sqlite3.Error, TypeError, ValueError):
            return None

    # ── 查询 ──
    @staticmethod
    def _bounds(since=None, until=None):
        start = _float(since) if since is not None else 0.0
        end = float(until) if until is not None else time.time() + 1
        return start, end

    def summary(self, since=None, until=None, models=None) -> dict:
        start, end = self._bounds(since, until)
        clause, params = _model_filter(models)
        row = None
        try:
            with self._db() as con:
                row = con.execute(
                    "SELECT COUNT(*) requests,"
                    " COALESCE(SUM(input_tokens),0) input_tokens,"
                    " COALESCE(SUM(output_tokens),0) output_tokens,"
                    " COALESCE(SUM(cache_hit_tokens),0) cache_hit_tokens,"
                    " COALESCE(SUM(total_tokens),0) total_tokens,"
                    " COALESCE(SUM(cost),0) cost,"
                    " COALESCE(SUM(cost_known),0) priced_requests,"
                    " COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) failed_requests,"
                    " COALESCE(SUM(elapsed_s),0) elapsed_s,"
                    " COALESCE(MIN(ts),0) first_ts,"
                    " COALESCE(MAX(ts),0) last_ts"
                    " FROM requests WHERE ts >= ? AND ts < ?" + clause,
                    [start, end, *params]).fetchone()
        except sqlite3.Error as exc:
            log.warning("[用量] 汇总失败: %s", type(exc).__name__)
            row = None
        data = dict(row) if row else {}
        requests = int(data.get("requests") or 0)
        input_tokens = int(data.get("input_tokens") or 0)
        output_tokens = int(data.get("output_tokens") or 0)
        cache_hit = int(data.get("cache_hit_tokens") or 0)
        elapsed = float(data.get("elapsed_s") or 0)
        return {
            "requests": requests,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_hit_tokens": cache_hit,
            "total_tokens": int(data.get("total_tokens") or 0),
            "cost": round(float(data.get("cost") or 0), 6),
            "cache_rate": round(cache_hit / input_tokens, 4) if input_tokens else 0.0,
            "priced_requests": int(data.get("priced_requests") or 0),
            "failed_requests": int(data.get("failed_requests") or 0),
            "avg_latency_s": round(elapsed / requests, 3) if requests else 0.0,
            "avg_output_speed": round(output_tokens / elapsed, 2) if elapsed > 0 and output_tokens else 0.0,
            "first_ts": float(data.get("first_ts") or 0),
            "last_ts": float(data.get("last_ts") or 0),
            "since": start, "until": end,
        }

    def breakdown(self, by="model", *, since=None, until=None, limit=20, models=None) -> list[dict]:
        if by not in GROUP_FIELDS:
            raise ValueError(f"不支持的统计维度: {by}")
        start, end = self._bounds(since, until)
        clause, params = _model_filter(models)
        order = "cost DESC, total_tokens DESC" if by != "hour" else "hour ASC"
        rows = []
        try:
            with self._db() as con:
                rows = con.execute(
                    f"SELECT {by} AS key, COUNT(*) requests,"
                    " COALESCE(SUM(input_tokens),0) input_tokens,"
                    " COALESCE(SUM(output_tokens),0) output_tokens,"
                    " COALESCE(SUM(cache_hit_tokens),0) cache_hit_tokens,"
                    " COALESCE(SUM(total_tokens),0) total_tokens,"
                    " COALESCE(SUM(cost),0) cost,"
                    " COALESCE(SUM(elapsed_s),0) elapsed_s"
                    " FROM requests WHERE ts >= ? AND ts < ?" + clause +
                    f" GROUP BY {by} ORDER BY {order} LIMIT ?",
                    [start, end, *params, int(limit)]).fetchall()
        except sqlite3.Error as exc:
            log.warning("[用量] 分组统计失败: %s", type(exc).__name__)
        result = []
        for row in rows:
            item = dict(row)
            inp = int(item.pop("input_tokens", 0) or 0)
            out = int(item.pop("output_tokens", 0) or 0)
            cache = int(item.pop("cache_hit_tokens", 0) or 0)
            elapsed = float(item.pop("elapsed_s", 0) or 0)
            item["input_tokens"] = inp
            item["output_tokens"] = out
            item["cache_hit_tokens"] = cache
            item["total_tokens"] = int(item.get("total_tokens") or 0)
            item["cost"] = round(float(item.get("cost") or 0), 6)
            item["cache_rate"] = round(cache / inp, 4) if inp else 0.0
            item["avg_output_speed"] = round(out / elapsed, 2) if elapsed > 0 and out else 0.0
            item["key"] = "" if item.get("key") is None else str(item["key"])
            result.append(item)
        return result

    def daily(self, days=30, *, until=None, models=None) -> list[dict]:
        days = max(1, min(int(days), 3660))
        end_dt = datetime.fromtimestamp(until) if until is not None else datetime.now()
        first_day = (end_dt - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = first_day.timestamp()
        rows = self.breakdown("day", since=start, until=until, limit=days + 2, models=models)
        found = {row["key"]: row for row in rows}
        series = []
        for offset in range(days):
            day = (first_day + timedelta(days=offset)).strftime("%Y-%m-%d")
            row = found.get(day, {})
            series.append({
                "day": day,
                "requests": int(row.get("requests") or 0),
                "input_tokens": int(row.get("input_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "cache_hit_tokens": int(row.get("cache_hit_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "cost": round(float(row.get("cost") or 0), 6),
            })
        return series

    def recent(self, limit=50, *, since=None, models=None) -> list[dict]:
        start, _ = self._bounds(since, None)
        clause, params = _model_filter(models)
        limit = max(1, min(int(limit), 500))
        try:
            with self._db() as con:
                rows = con.execute(
                    "SELECT * FROM requests WHERE ts >= ?" + clause +
                    " ORDER BY ts DESC LIMIT ?", [start, *params, limit]).fetchall()
        except sqlite3.Error as exc:
            log.warning("[用量] 读取最近请求失败: %s", type(exc).__name__)
            return []
        result = []
        for row in rows:
            item = dict(row)
            item["time"] = datetime.fromtimestamp(item["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            item["ok"] = bool(item.get("ok"))
            item["stream"] = bool(item.get("stream"))
            item["cost_known"] = bool(item.get("cost_known"))
            result.append(item)
        return result

    def heatmap(self, days=365, *, until=None, models=None) -> dict:
        """按星期(0=周日)/小时聚合，用于一年热力图。"""
        days = max(1, min(int(days), 3660))
        until_ts = float(until) if until is not None else time.time()
        since = until_ts - days * 86400
        clause, params = _model_filter(models)
        cells = []
        try:
            with self._db() as con:
                rows = con.execute(
                    "SELECT CAST(strftime('%w', ts, 'unixepoch', 'localtime') AS INTEGER) dow,"
                    " CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) hour,"
                    " COUNT(*) requests, COALESCE(SUM(total_tokens),0) tokens,"
                    " COALESCE(SUM(cost),0) cost"
                    " FROM requests WHERE ts >= ?" + clause +
                    " GROUP BY dow, hour", [since, *params]).fetchall()
                cells = [dict(row) for row in rows]
        except sqlite3.Error as exc:
            log.warning("[用量] 热力图统计失败: %s", type(exc).__name__)
        return {
            "days": days,
            "cells": cells,
            "max_requests": max((c["requests"] for c in cells), default=0),
            "total_requests": sum(c["requests"] for c in cells),
        }

    def prune(self, retention_days=365) -> int:
        retention_days = max(1, int(retention_days))
        cutoff = time.time() - retention_days * 86400
        try:
            with self._db() as con:
                cur = con.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        except sqlite3.Error as exc:
            log.warning("[用量] 清理失败: %s", type(exc).__name__)
            return 0


_stores: dict[str, UsageStore] = {}
_stores_lock = threading.Lock()


def get_store(path=None) -> UsageStore:
    """按数据库路径复用实例；默认写入当前数据目录的 usage.db。"""
    key = str(Path(path) if path else Path(data_file("usage.db")))
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = UsageStore(key)
            _stores[key] = store
        return store
