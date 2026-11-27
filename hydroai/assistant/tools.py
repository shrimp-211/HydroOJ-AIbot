"""Agent 工具只访问当前会话已核实的资源，不运行学生代码。"""
from __future__ import annotations

from hydroai.assistant.context import Context, number_lines, render_discussion
from hydroai.assistant.outbox import Outbox
from hydroai.assistant.prompts import fence
from hydroai.oj.api import OJError
from hydroai.oj.resources import status_name


class ToolError(ValueError):
    pass


class Finish(Exception):
    pass


class Toolbox:
    def __init__(self, ctx: Context, outbox: Outbox):
        self.ctx, self.api, self.outbox = ctx, ctx.api, outbox
        self.tools = {
            "read_discussion": (self.read_discussion, "{}：读当前讨论"),
            "read_problem": (self.read_problem, "{}：读关联题面"),
            "list_solutions": (self.list_solutions, "{}：列出关联题解链接"),
            "read_solution": (self.read_solution, '{"psid":"题解 ID"}：读取关联题解'),
            "send_private": (self.send_private, '{"uid":提问者 UID,"content":"答疑"}：私信本人'),
            "finish": (self.finish, "{}：结束"),
        }
        if ctx.private:
            self.tools["analyze_submission"] = (self.analyze_submission, "{}：查看所选提交的代码与评测概况")
        else:
            self.tools.update({
                "reply_discussion": (self.reply_discussion, '{"content":"答疑"}：在当前讨论 @ 回复'),
                "reply_in_thread": (self.reply_in_thread, '{"drid":"回复 ID","content":"答疑"}：在当前讨论楼中楼回复'),
                "post_solution_reply": (self.post_solution_reply, '{"psid":"题解 ID","content":"讲解"}：在关联题解下补充讲解'),
            })

    def docs(self) -> str:
        return "\n".join(f"- {name} {doc}" for name, (_, doc) in self.tools.items())

    def call(self, name: str, args: dict) -> str:
        if name not in self.tools:
            raise ToolError("当前会话不允许这个动作")
        try:
            return self.tools[name][0](**args)
        except TypeError as exc:
            raise ToolError("参数不符合工具说明") from exc
        except OJError as exc:
            raise ToolError(f"OJ 未确认请求成功（HTTP {exc.status or '未知'}）") from exc

    def read_discussion(self) -> str:
        return render_discussion(self.ctx) if self.ctx.discussion else "没有关联讨论"

    def read_problem(self) -> str:
        p = self.ctx.problem
        return fence("题面", f"{p['title']}\n{p['content']}", 6500) if p else "没有可用题面"

    def list_solutions(self) -> str:
        return "\n".join(f"{item['psid']} {item['url']}" for item in self.ctx.solutions) or "没有可用题解"

    def _solution(self, psid: str) -> dict:
        for item in self.ctx.solutions:
            if item["psid"] == psid:
                return item
        raise ToolError("题解不属于当前题目")

    def read_solution(self, psid: str) -> str:
        return fence("题解", self._solution(psid)["content"], 4800)

    def analyze_submission(self) -> str:
        best = (self.ctx.student or {}).get("best_submission")
        if not best:
            return "没有可用的提交"
        record = self.api.records.get(best["rid"])["rdoc"]
        if (str(record.get("uid")) != str(self.ctx.author_uid)
                or str(record.get("pid")) != self.ctx.internal_pid
                or record.get("domainId") != self.api.domain):
            raise ToolError("提交不属于提问者或当前题目")
        cases = record.get("testCases") or []
        lines = [f"状态 {status_name(record.get('status'))}；得分 {record.get('score')}；评测点 {len(cases)} 个",
                 fence("编译信息", record.get("compilerTexts"), 1000),
                 fence("学生代码", number_lines(record.get("code", "")), 6000)]
        return "\n".join(lines)

    @staticmethod
    def _content(content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content 必须是文字")
        return content

    def reply_discussion(self, content: str) -> str:
        return self.outbox.reply_discussion(self._content(content))

    def reply_in_thread(self, drid: str, content: str) -> str:
        if not any(reply.get("_id") == drid for reply in self.ctx.discussion.get("drdocs", [])):
            raise ToolError("回复不在当前讨论中")
        return self.outbox.reply_in_thread(drid, self._content(content))

    def send_private(self, uid: int, content: str) -> str:
        if type(uid) is not int:
            raise ToolError("uid 必须是整数")
        return self.outbox.send_private(uid, self._content(content))

    def post_solution_reply(self, psid: str, content: str) -> str:
        solution = self._solution(psid)
        result = self.outbox.post_solution_reply(self.ctx.pid, psid, self._content(content))
        if result.startswith("已发送"):
            self.outbox.solution_url = solution["url"]
            return f"{result}；题解地址：{solution['url']}"
        return result

    def finish(self, summary: str = "") -> str:
        # 审计只记录工具名，避免把模型生成的总结写入日志。
        raise Finish()
