#!/usr/bin/env python3
"""仪表盘 — 活动追踪、状态查询、统计汇总。预留 Web 接入接口。"""

import json, time, threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict


@dataclass
class ProblemRecord:
    pid: str
    title: str = ""
    status: str = "pending"     # pending | ac | fail | error
    score: int = 0
    time_ms: float = 0
    memory_kb: float = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: int = 0
    cost: float = 0.0
    retries: int = 0
    outer_retries: int = 0
    elapsed_s: float = 0
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self): return asdict(self)


@dataclass
class ContestRecord:
    cid: str
    title: str = ""
    domain: str = ""
    total: int = 0
    ac: int = 0
    fail: int = 0
    elapsed_s: float = 0
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self): return asdict(self)


class Dashboard:
    """活动仪表盘 — 线程安全，预留 JSON 导出接口。
    使用方式：守护进程持有唯一实例，子进程结果通过 daemon 调用 update 方法写入。"""

    def __init__(self, save_path: str = "dashboard.json", max_records: int = 500):
        self._lock = threading.Lock()
        self._save_path = Path(save_path)
        self._max = max_records
        self._last_save = 0.0
        self._dirty = False
        self.problems: dict[str, ProblemRecord] = {}
        self.contests: dict[str, ContestRecord] = {}
        self.history: list[dict] = []
        self._load()

    # ── 追踪 API ──
    def problem_start(self, pid: str, title: str = ""):
        with self._lock:
            self.problems[pid] = ProblemRecord(
                pid=pid, title=title,
                started_at=datetime.now().strftime("%m-%d %H:%M:%S"))
            self._mark_dirty()

    def problem_update(self, pid: str, **kwargs):
        with self._lock:
            r = self.problems.get(pid)
            if not r: return
            for k, v in kwargs.items():
                if hasattr(r, k): setattr(r, k, v)
            r.finished_at = datetime.now().strftime("%m-%d %H:%M:%S")
            self.history.append(r.to_dict())
            if len(self.history) > self._max:
                self.history = self.history[-self._max:]
            self._mark_dirty()

    def contest_start(self, cid: str, title: str = "", domain: str = "", total: int = 0):
        with self._lock:
            self.contests[cid] = ContestRecord(
                cid=cid, title=title, domain=domain, total=total,
                started_at=datetime.now().strftime("%m-%d %H:%M:%S"))
            self._mark_dirty()

    def contest_update(self, cid: str, **kwargs):
        with self._lock:
            r = self.contests.get(cid)
            if not r: return
            for k, v in kwargs.items():
                if hasattr(r, k): setattr(r, k, v)
            r.finished_at = datetime.now().strftime("%m-%d %H:%M:%S")
            self.history.append({**r.to_dict(), "type": "contest"})
            self._mark_dirty()

    # ── 查询 API（供控制台/私聊/Web 使用）──
    def pending(self) -> list[dict]:
        """正在求解的题目。过滤掉历史中已完成的和超时的（>30min）。"""
        import time as _time
        with self._lock:
            # 从 history 中提取已完成的 pid
            done = {h["pid"] for h in self.history}
            result = []
            for r in self.problems.values():
                if r.status != "pending":
                    continue
                if str(r.pid) in done:
                    continue  # 历史中已有记录
                # 超过 30 分钟视为已结束
                if r.started_at:
                    try:
                        t = _time.mktime(_time.strptime(r.started_at, "%m-%d %H:%M:%S"))
                        if _time.time() - t > 1800:
                            continue
                    except (ValueError, OSError):
                        pass
                result.append(r.to_dict())
            return result

    def recent(self, n: int = 10) -> list[dict]:
        """最近 N 条记录"""
        with self._lock:
            return self.history[-n:]

    def stats(self) -> dict:
        """汇总统计"""
        with self._lock:
            total = len(self.history)
            ac = sum(1 for h in self.history if h.get("status") == "ac")
            total_tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in self.history)
            total_cache = sum(h.get("cache_hit", 0) for h in self.history)
            total_cost = sum(h.get("cost", 0) for h in self.history)
            total_time = sum(h.get("elapsed_s", 0) for h in self.history)
            return {
                "total": total, "ac": ac, "fail": total - ac,
                "ac_rate": f"{ac}/{total}" if total else "0/0",
                "total_tokens": total_tokens, "cache_hit": total_cache,
                "total_cost": total_cost, "total_time_s": total_time,
                "pending": len(self.pending()), "contests": len(self.contests),
            }

    def today(self) -> dict:
        """今日统计"""
        today = datetime.now().strftime("%m-%d")
        with self._lock:
            today_records = [h for h in self.history
                           if h.get("finished_at", "").startswith(today)]
            ac = sum(1 for h in today_records if h.get("status") == "ac")
            tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in today_records)
            cache = sum(h.get("cache_hit", 0) for h in today_records)
            cost = sum(h.get("cost", 0) for h in today_records)
            return {
                "date": today, "total": len(today_records),
                "ac": ac, "tokens": tokens, "cache_hit": cache, "cost": cost,
            }

    # ── 格式化输出 ──
    def format_pending(self) -> str:
        p = self.pending()
        if not p: return "📋 无正在求解的题目"
        lines = ["📋 正在求解:"]
        for r in p:
            try:
                elapsed = time.time() - time.mktime(
                    time.strptime(r["started_at"], "%m-%d %H:%M:%S")
                ) if r.get("started_at") else 0
            except (ValueError, OSError):
                elapsed = 0
            tinfo = f" | Token {r['tokens_in']}i/{r['tokens_out']}o"
            if r.get("cache_hit"): tinfo += f" (缓存{r['cache_hit']})"
            if r.get("cost"): tinfo += f" ¥{r['cost']:.4f}"
            lines.append(f"  #{r['pid']} {r.get('title','')[:15]} [{r.get('status','?')}]{tinfo} | {elapsed:.0f}s")
        return "\n".join(lines)

    def format_stats(self) -> str:
        s = self.stats()
        cache_info = f" | 缓存命中: {s['cache_hit']}" if s.get('cache_hit') else ""
        cost_info = f" | 预估费用: ¥{s['total_cost']:.4f}" if s.get('total_cost') else ""
        return (
            f"📊 总计 {s['total']} 题 | AC {s['ac']} | AC率 {s['ac_rate']}\n"
            f"💰 Token: {s['total_tokens']}{cache_info}{cost_info}\n"
            f"⏱️ 耗时: {s['total_time_s']:.0f}s | 🔄 进行中: {s['pending']} | 🏆 比赛: {s['contests']}"
        )

    def format_today(self) -> str:
        t = self.today()
        return f"📅 {t['date']} | {t['total']}题 | AC {t['ac']} | Token {t['tokens']} | 缓存{t.get('cache_hit',0)} | ¥{t.get('cost',0):.4f}"

    def format_recent(self, n: int = 5) -> str:
        items = self.recent(n)
        if not items: return "暂无记录"
        lines = [f"📜 最近 {len(items)} 条:"]
        for h in items[-n:]:
            ts = h.get("finished_at", "?")
            pid = h.get("pid", "?")
            st = h.get("status", "?")
            emoji = "✅" if st == "ac" else ("❌" if st == "fail" else "⏳")
            lines.append(f"  [{ts}] {emoji} #{pid} {st}")
        return "\n".join(lines)

    # ── 持久化（防抖 + 原子写入）──
    def _mark_dirty(self):
        """标记需保存，每 2s 最多写一次（持有锁保护完整流程）"""
        with self._lock:
            self._dirty = True
            now = time.time()
            if now - self._last_save >= 2:
                self._flush_locked()

    def _flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        """调用前必须已持有 _lock"""
        if not self._dirty: return
        self._dirty = False
        self._last_save = time.time()
        try:
            data = {
                "problems": {k: v.to_dict() for k, v in self.problems.items()},
                "contests": {k: v.to_dict() for k, v in self.contests.items()},
                "history": self.history,
            }
            tmp = str(self._save_path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            Path(tmp).replace(self._save_path)  # 原子写入
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("[仪表盘] 保存失败: %s", e)

    def save(self):
        """手动触发保存"""
        with self._lock:
            self._flush()

    def _load(self):
        try:
            with open(self._save_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.get("problems", {}).items():
                self.problems[k] = ProblemRecord(**v)
            for k, v in data.get("contests", {}).items():
                self.contests[k] = ContestRecord(**v)
            self.history = data.get("history", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass
