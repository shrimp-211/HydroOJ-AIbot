"""学情汇总：只报告记录事实和待验证的重复现象。"""
from __future__ import annotations

from collections import Counter, defaultdict


def build_profile(events: list[dict], now: float | None = None) -> dict:
    grouped = defaultdict(list)
    for event in events:
        grouped[event.get("tag") or "未分类"].append(event)
    tags = {}
    for tag, items in grouped.items():
        errors = Counter(e.get("error_type") for e in items
                         if e.get("error_type") not in (None, "", "待确定", "基本正确"))
        tags[tag] = {"attempts": len(items), "solved": sum(bool(e.get("solved")) for e in items),
                     "common_errors": [x for x, n in errors.most_common() if n >= 2],
                     "avg_distance": None, "weighted_distance": None, "trend": ""}
    errors = Counter(e.get("error_type") for e in events
                     if e.get("error_type") not in (None, "", "待确定", "基本正确"))
    return {"tags": tags, "weak_tags": [],
            "recurring_errors": [k for k, n in errors.most_common(3) if n >= 2],
            "error_counts": dict(errors), "improving": [], "declining": [], "events": len(events)}


def summary_text(profile: dict, top: int = 3) -> str:
    if not profile["events"]:
        return "暂无学情记录。"
    lines = [f"已有 {profile['events']} 条按题目标签归类的记录；单次提交不足以判断能力。"]
    if profile["recurring_errors"]:
        lines.append("多次记录中的错误类型（可能含模型推测，需核实）：" +
                     "、".join(profile["recurring_errors"][:top]))
    return "\n".join(lines)


def student_report(uname: str, uid: int, profile: dict, analyses: list[dict]) -> str:
    out = [f"# 学情报告：{uname or uid}（uid={uid}）", "", summary_text(profile), "",
           "## 提交诊断（非能力定论）"]
    for a in sorted(analyses, key=lambda x: x.get("_at", 0), reverse=True)[:10]:
        out.append(f"- 题目 {a['pid']}：{a.get('error_type', '待确定')}；"
                   f"定性判断（待验证）：{a.get('distance_level', '无法确定')}；{a.get('root_cause', '')}")
        out.extend(f"  - 本次提交待修项（待验证）：{s}" for s in a.get("repair_items", [])[:5])
        out.extend(f"  - 本次提交相关概念（待验证）：{s}" for s in a.get("missing_concepts", [])[:5])
        out.extend(f"  - 建议：{s}" for s in a.get("suggestions", [])[:2])
    return "\n".join(out)


def class_report(domain: str, profiles: dict[int, tuple[str, dict]]) -> str:
    errors = Counter()
    for _, prof in profiles.values():
        errors.update(prof["error_counts"])
    out = [f"# 班级学情概览：{domain}", "", f"统计学生 {len(profiles)} 人。", "",
           "## 记录中的错误类型（含待验证的模型推测）"]
    out.extend([f"- {k}：{v} 次" for k, v in errors.most_common(8)] or ["- 暂无数据"])
    return "\n".join(out)
