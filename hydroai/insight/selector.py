"""挑选「最值得分析」的提交（纯函数，无 IO）。

原则：不逐条分析。每个 (uid, pid) 至多选一条，再按学生整体取 top-N。

单题内的取舍（``pick_for_problem``）：
1. 全部 pending / 取消 → 不选；
2. 已 AC：一次过不分析；经历过失败则选「AC 之前最后一条有信息量的失败」，它暴露真正卡点；
3. 未 AC：非 CE 中「得分最高、同分取最近」；整题全是 CE 则取最后一条 CE；
4. 已分析过的记录及其之前记录不再分析；保守去重已缓存的相同代码。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from hydroai.insight.store import submit_time

AC, CE = 1, 7
PENDING = {0, 20, 21, 22}
IGNORED = {9, 13}  # 取消 / 忽略：没有分析价值
MAX_SCORE = 100.0


@dataclass
class Selection:
    rid: str
    uid: int
    pid: str
    reason: str
    priority: float
    status: int = 0
    score: float = 0.0


def code_hash(code: str) -> str:
    """只规范行尾空白，保留字符串、缩进和注释以避免误判不同程序。"""
    text = "\n".join(line.rstrip() for line in (code or "").replace("\r\n", "\n").split("\n"))
    return hashlib.sha256(text.encode("utf8", "ignore")).hexdigest()


def _key(r: dict) -> str:
    return str(r.get("_id") or r.get("rid"))


def _time(r: dict) -> float:
    return r.get("submit_at") or submit_time(r)


def _score(r: dict) -> float:
    try:
        return float(r.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _hash(r: dict, hashes: dict[str, str]) -> str:
    return hashes.get(_key(r)) or r.get("code_hash") or ""


def pick_for_problem(rdocs: list[dict], *, analyzed=frozenset(),
                     hashes: dict[str, str] | None = None) -> tuple[dict, str] | None:
    """在同一学生同一题的记录中选一条，返回 ``(rdoc, 理由)``；无值得分析者返回 None。"""
    hashes = hashes or {}
    recs = sorted((r for r in rdocs if int(r.get("status", 0) or 0) not in PENDING | IGNORED),
                  key=_time)
    if not recs:
        return None
    ac_idx = next((i for i, r in enumerate(recs) if r["status"] == AC), None)
    if ac_idx is not None:
        fails = recs[:ac_idx]
        if not fails:
            return None  # 一次通过
        ordered = list(reversed([r for r in fails if r["status"] != CE] or fails))
        why = f"最终 AC，之前失败 {len(fails)} 次；选 AC 之前最后一次失败（暴露真正卡点）"
    else:
        non_ce = [r for r in recs if r["status"] != CE]
        if non_ce:
            ordered = sorted(non_ce, key=lambda r: (_score(r), _time(r)), reverse=True)
            why = f"未通过（共 {len(recs)} 次）；选非编译错误中得分最高的最近一次"
        else:
            ordered = list(reversed(recs))
            why = f"全部为编译错误（{len(recs)} 次）；选最后一次 CE"
    done_hashes = {h for r in recs if _key(r) in analyzed and (h := _hash(r, hashes))}
    latest_done = max((_time(r) for r in recs if _key(r) in analyzed), default=-1)
    for r in ordered:
        if _key(r) in analyzed or _time(r) <= latest_done:
            continue
        h = _hash(r, hashes)
        if h and h in done_hashes:
            continue
        return r, why
    return None


def _priority(rdoc: dict, solved: bool, n_fail: int, now: float) -> float:
    """未通过 > 通过但曾失败；得分越接近满分（差一点）越高；越新越高；失败次数略加分。"""
    p = 15.0 if solved else 40.0
    score = _score(rdoc)
    if not solved and score > 0:
        p += 30 * min(score, MAX_SCORE) / MAX_SCORE
        if score >= 60:
            p += 10
    if rdoc["status"] == CE:
        p -= 10
    age_days = max(0.0, (now - _time(rdoc)) / 86400)
    p += 15 * 0.5 ** (age_days / 7)   # 一周半衰
    p += min(n_fail, 5)
    return round(p, 2)


def select_submissions(rdocs: list[dict], *, uid: int | None = None, max_n: int = 5,
                       analyzed=frozenset(), hashes: dict[str, str] | None = None,
                       now: float | None = None, tags_by_pid: dict | None = None) -> list[Selection]:
    """从学生的全部记录中选出至多 ``max_n`` 条值得分析的提交，按优先级降序。

    给出 ``tags_by_pid`` 时优先覆盖不同知识点：第一轮跳过标签已被覆盖的题，第二轮补足名额。
    """
    now = now or time.time()
    by_pid: dict[tuple[int, str], list[dict]] = {}
    for r in rdocs:
        if uid is not None and int(r.get("uid", 0)) != int(uid):
            continue
        by_pid.setdefault((int(r.get("uid", 0)), str(r.get("pid"))), []).append(r)
    cands = []
    for (_, pid), recs in by_pid.items():
        picked = pick_for_problem(recs, analyzed=analyzed, hashes=hashes)
        if not picked:
            continue
        r, why = picked
        solved = any(x.get("status") == AC for x in recs)
        n_fail = sum(1 for x in recs if x.get("status") not in ({AC} | PENDING | IGNORED))
        cands.append(Selection(_key(r), int(r.get("uid", 0)), pid, why,
                               _priority(r, solved, n_fail, now),
                               int(r["status"]), _score(r)))
    cands.sort(key=lambda s: s.priority, reverse=True)
    if not tags_by_pid:
        return cands[:max_n]
    chosen, covered = [], set()
    for need_new in (True, False):
        for s in cands:
            if len(chosen) >= max_n:
                break
            if s in chosen:
                continue
            tags = set(tags_by_pid.get(s.pid) or [])
            if need_new and tags and tags <= covered:
                continue
            chosen.append(s)
            covered |= tags
    return sorted(chosen, key=lambda s: s.priority, reverse=True)
