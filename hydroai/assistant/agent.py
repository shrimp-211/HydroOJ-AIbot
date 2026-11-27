"""与供应商无关的 JSON 动作循环（ReAct）。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from hydroai.assistant.prompts import fence
from hydroai.assistant.tools import Finish, ToolError, Toolbox

log = logging.getLogger(__name__)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


def parse_action(text: str) -> dict | None:
    """接受单个 JSON 对象，可带代码围栏；不执行歧义或嵌套的动作。"""
    if not isinstance(text, str) or len(text) > 12000:
        return None
    text = text.strip()
    if match := _FENCE.fullmatch(text):
        text = match.group(1)
    try:
        action = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        return None
    if not isinstance(action.get("args"), dict):
        return None
    return action


@dataclass
class AgentResult:
    finished: bool = False
    steps: int = 0
    log: list[dict] = field(default_factory=list)
    cost: float = 0.0


class Agent:
    """``call_fn(messages) -> {"content": str, "cost": float}``，可替换以便离线测试。"""

    def __init__(self, call_fn, toolbox: Toolbox, system: str, max_steps: int = 8):
        self.call_fn, self.toolbox = call_fn, toolbox
        self.system, self.max_steps = system, max_steps

    def run(self, context_text: str) -> AgentResult:
        res = AgentResult()
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": context_text + "\n\n请开始，输出第一个动作 JSON。"}]
        bad_json = 0
        for step in range(1, self.max_steps + 1):
            res.steps = step
            reply = self.call_fn(messages) or {}
            res.cost += float(reply.get("cost") or 0)
            content = reply.get("content", "") or ""
            action = parse_action(content)
            if action is None:
                bad_json += 1
                res.log.append({"step": step, "error": "bad_json"})
                if bad_json >= 2:  # 允许一次修复机会
                    break
                messages += [{"role": "assistant", "content": content[:1500]},
                             {"role": "user", "content": "输出不是合法的动作 JSON。请只输出一个 "
                              '{"action": "...", "args": {}} 对象。'}]
                continue
            name = action["action"]
            entry = {"step": step, "action": name if name in self.toolbox.tools else "unknown"}
            try:
                obs = self.toolbox.call(name, action["args"])
            except Finish:
                res.finished = True
                res.log.append(entry)
                return res
            except ToolError as exc:
                obs = f"错误: {exc}"
            except Exception as exc:  # 工具内部意外：告诉模型，不中断整个流程
                log.warning("[助手] 工具 %s 异常：%s", entry["action"], type(exc).__name__)
                obs = f"工具异常: {type(exc).__name__}"
            entry["error"] = obs.startswith(("错误", "工具异常"))
            res.log.append(entry)
            observation = fence("工具结果", obs, 7000)
            messages += [{"role": "assistant", "content": json.dumps(action, ensure_ascii=False)},
                         {"role": "user", "content": f"观察:\n{observation}\n\n继续，输出下一个动作 JSON。"}]
        return res
