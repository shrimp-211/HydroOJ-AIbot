"""模块注册表：声明式列表，入口按需导入，避免未使用模块拖慢启动。"""
from __future__ import annotations

import importlib

from hydroai.modules.base import ModuleSpec

_SPECS = (
    ModuleSpec(
        name="ojkit", title="OJ 交互", package="hydroai.oj",
        summary="登录、题目、提交与评测、记录、讨论、题解、私信通知、比赛与训练、用户",
        entry="hydroai.oj.cli:main",
        capabilities=("problems", "records", "discussions", "solutions", "messages",
                      "contests", "trainings", "homework", "users", "inbox", "notify"),
        config_keys=("oj_root", "oj_base", "cookie_jar", "msg_push_list"),
    ),
    ModuleSpec(
        name="solver", title="AI 解题与题解", package="hydroai.solve",
        summary="获取题面 → 生成/验证代码 → 提交 → 撰写并发布题解",
        entry="hydroai.solve.cli:main",
        capabilities=("solve", "contest", "editorial", "benchmark", "testdata"),
        requires=("ojkit",),
        config_keys=("ai_model", "agent_enabled", "editorial_enable", "max_cost_per_problem"),
    ),
    ModuleSpec(
        name="insight", title="学情诊断", package="hydroai.insight",
        summary="从提交记录中挑选最值得分析的提交，长期追踪学生薄弱点与距正解的差距",
        entry="hydroai.insight.cli:main",
        capabilities=("select", "analyze", "profile", "report"),
        requires=("ojkit",),
        config_keys=("insight_db", "insight_model", "insight_max_per_student"),
    ),
    ModuleSpec(
        name="assistant", title="@ 助手 Agent", package="hydroai.assistant",
        summary="接收 @ 通知，读取讨论/题目/学生代码，在讨论区或私信中答疑、讲题解",
        entry="hydroai.assistant.cli:main",
        capabilities=("mentions", "discussion_reply", "private_message", "editorial_link",
                      "code_review"),
        requires=("ojkit", "insight"),
        config_keys=("assistant_enable", "assistant_domains", "assistant_model"),
    ),
)
_BY_NAME = {s.name: s for s in _SPECS}


def list_specs() -> list[ModuleSpec]:
    return list(_SPECS)


def get_spec(name: str) -> ModuleSpec:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError(f"未知模块 {name!r}，可用: {', '.join(_BY_NAME)}") from None


def describe() -> list[dict]:
    return [{"name": s.name, "title": s.title, "summary": s.summary,
             "capabilities": list(s.capabilities), "requires": list(s.requires),
             "package": s.package} for s in _SPECS]


def run_module(name: str, argv: list[str]) -> int:
    module_path, func = get_spec(name).entry.split(":")
    entry = getattr(importlib.import_module(module_path), func)
    return int(entry(argv) or 0)
