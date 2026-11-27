"""助手服务：校验通知身份和范围，再读取上下文，完成一轮有界答疑。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from hydroai.ai.health import AIUnavailableError
from hydroai.assistant.agent import Agent
from hydroai.assistant.context import enrich_context, load_event, make_insight
from hydroai.assistant.events import MentionEvent, parse_event, same_site
from hydroai.assistant.outbox import Outbox
from hydroai.assistant.prompts import build_system
from hydroai.assistant.store import AssistantStore
from hydroai.assistant.tools import Toolbox
from hydroai.config.paths import data_dir
from hydroai.oj.api import OJApi, OJError
from hydroai.oj.inbox import InboxPoller, SeenStore
from hydroai.oj.mentions import SYSTEM_UID
from hydroai.oj.notify import Notifier

log = logging.getLogger(__name__)
UNAVAILABLE = "助手暂时不可用，请稍后再 @ 我。"
FAILED = "这次没能整理出可靠的回答，请稍后再 @ 我，或联系老师。"


@dataclass
class HandleResult:
    status: str
    reason: str = ""
    replies: int = 0
    steps: int = 0
    cost: float = 0.0
    sent: list = field(default_factory=list)


class AssistantService:
    def __init__(self, api: OJApi, config, *, call_fn=None, store=None,
                 insight="auto", dry_run: bool | None = None, max_age: float = 3600):
        self.api, self.config = api, config
        self.dry_run = bool(config.get("assistant_dry_run", False) if dry_run is None else dry_run)
        self.store = store or AssistantStore(data_dir() / "assistant.db")
        self._insight, self._ai = insight, None
        self.call_fn = call_fn or self._default_call
        self.max_age = max_age

    def _default_call(self, messages: list) -> dict:
        from hydroai.ai.client import AIClient
        if self._ai is None:
            self._ai = AIClient(self.config, show_thinking=False)
        model = self.config.get("assistant_model", "") or self.config["ai_model"]
        response = self._ai.chat(messages, model=model, task="assistant",
                                 max_tokens=min(4096, self.config.task_token_limit("assistant")))
        if not response:
            raise AIUnavailableError("答疑模型未返回可用结果")
        return response

    def handle_message(self, msg: dict) -> HandleResult:
        event = parse_event(msg)
        recipients = msg.get("to") or []
        if event is None or (recipients and self.api.uid not in recipients):
            return HandleResult("ignored", "不是给当前账号的 @ 通知")
        return self.handle_event(event)

    def handle_event(self, event: MentionEvent) -> HandleResult:
        if not self.config.get("assistant_enable", True):
            return HandleResult("ignored", "助手已停用")
        domains = self.config.get("assistant_domains")
        if not isinstance(domains, list) or event.domain not in domains:
            return HandleResult("ignored", "域不在允许列表")
        if not same_site(event.link.url, self.api.root):
            return HandleResult("ignored", "通知链接不在当前 OJ")
        start, outbox, tools = time.monotonic(), None, []
        try:
            ctx = load_event(self.api, event)
            if ctx.author_uid <= SYSTEM_UID or ctx.author_uid == ctx.api.uid:
                return HandleResult("ignored", "不是可确认的其他用户")
            users = self.config.get("assistant_users") or []
            if users and ctx.author_uid not in users:
                return HandleResult("ignored", "用户不在允许列表")
            cap = max(0, int(self.config.get("assistant_max_replies_per_thread", 6)))
            used = self.store.thread_replies(event.domain, event.did) if event.did else 0
            insight = make_insight(ctx.api, self.config) if self._insight == "auto" else self._insight
            enrich_context(ctx, insight)
            if event.did and used >= cap and not ctx.private:
                return HandleResult("limited", "讨论回复已达上限")
            reveal = bool(self.config.get("assistant_reveal_code", False))
            outbox = Outbox(ctx, Notifier(ctx.api), reveal_code=reveal, dry_run=self.dry_run,
                            thread_budget=max(cap - used, 0))
            box = Toolbox(ctx, outbox)
            system = build_system(box.docs(), reveal_code=reveal, private=ctx.private)
            steps = min(20, max(1, int(self.config.get("assistant_max_steps", 8))))
            res = Agent(self.call_fn, box, system, steps).run(ctx.text)
            tools = res.log
            answered = self._answered(outbox)
            result = HandleResult("replied" if answered else "no_reply", steps=res.steps, cost=res.cost)
            if not answered:
                self._fallback(outbox, FAILED)
        except AIUnavailableError:
            result = HandleResult("unavailable", "答疑模型暂不可用")
            if self._ai is not None:
                self._ai.close()
                self._ai = None
            if outbox is not None and not self._answered(outbox):
                self._fallback(outbox, UNAVAILABLE)
        except Exception as exc:
            result = HandleResult("error", type(exc).__name__)
            if outbox is not None and not self._answered(outbox):
                self._fallback(outbox, FAILED)
        result.sent = outbox.sent if outbox is not None else []
        result.replies = len(result.sent)
        self.store.record(msg_id=event.msg_id, domain=event.domain, did=event.did,
                          author=event.author_name, status=result.status, replies=outbox.replies if outbox else 0,
                          dry_run=self.dry_run, tools=tools, elapsed=time.monotonic() - start,
                          cost=result.cost, error=result.reason)
        log.info("[助手] %s → %s %s", event.msg_id, result.status, result.reason)
        return result

    @staticmethod
    def _answered(outbox: Outbox) -> bool:
        if outbox.solution_url:
            return any(s["kind"] in ("reply", "tail_reply") for s in outbox.sent)
        return any(s["kind"] != "solution_reply" for s in outbox.sent)

    @staticmethod
    def _fallback(outbox: Outbox, text: str) -> None:
        try:
            if not outbox.private and outbox.did and outbox.reply_discussion(text).startswith("已发送"):
                return
            outbox.send_private(outbox.author_uid, text)
        except OJError:
            log.warning("[助手] 备用通知未确认送达")

    def run(self, once: bool = False) -> int:
        if not self.config.get("assistant_enable", True):
            log.info("[助手] 已停用")
            return 0
        if not self.api.login():
            raise OJError("助手登录失败")
        mode = "dry" if self.dry_run else "live"
        with SeenStore(data_dir() / "assistant_seen.db", f"{self.api.root}:{self.api.uid}:{mode}") as seen:
            poller = InboxPoller(self.api, self.handle_message, seen,
                                 interval=float(self.config.get("assistant_interval", 10)),
                                 max_age=self.max_age, accept=lambda m: parse_event(m) is not None)
            if once:
                return poller.poll_once()
            try:
                poller.run()
            except KeyboardInterrupt:
                poller.stop()
        return 0
