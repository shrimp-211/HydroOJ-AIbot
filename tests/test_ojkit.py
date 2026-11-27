"""ojkit：系统消息解析、资源层请求形状、收件箱去重、通知分段。离线运行。"""
import json
import time

import pytest

from hydroai.oj.api import OJApi, OJError
from hydroai.oj.inbox import InboxPoller, SeenStore
from hydroai.oj.mentions import (find_links, msg_time, parse_link,
                                 parse_system_message)
from hydroai.oj.notify import Notifier, split_text
from hydroai.oj.resources import mention, problem_text, status_name

MENTION = {"_id": "6b08610c6d779860b94e0f4c", "from": 1, "flag": 5, "to": [118],
           "content": json.dumps({"message": "User {0} mentioned you in {1:link}",
                                  "params": ["hanyixuan",
                                             "/d/root_test/discuss/6b0861096d779860b94e0f4a"]})}


def test_parse_link_variants():
    assert parse_link("/d/root_test/discuss/6b0861096d779860b94e0f4a").kind == "discuss"
    assert parse_link("https://oj.example/d/x/p/B2175/solution").ident == "B2175"
    assert parse_link("/discuss/6b0861096d779860b94e0f4a").domain == "system"
    assert parse_link("/d/x/record/6b0861096d779860b94e0f4a").kind == "record"
    assert parse_link("/nothing") is None


def test_find_links_in_free_text():
    text = "看这个 https://oj.example/d/a/discuss/6b0861096d779860b94e0f4a 和 /d/a/p/1/solution"
    assert [link.kind for link in find_links(text)] == ["discuss", "solution"]


def test_parse_system_mention():
    notice = parse_system_message(MENTION)
    assert notice.is_mention and notice.author == "hanyixuan"
    assert (notice.link.domain, notice.link.ident) == ("root_test", "6b0861096d779860b94e0f4a")


def test_non_system_or_plain_messages_are_ignored():
    assert parse_system_message({**MENTION, "from": 35}) is None
    assert parse_system_message({**MENTION, "content": "hello"}) is None


def test_msg_time_reads_objectid():
    assert msg_time("6b08610c6d779860b94e0f4c") > 1_700_000_000
    assert msg_time("zz") == 0.0


def test_status_and_problem_text():
    assert status_name(1) == "AC" and status_name(7) == "CE" and status_name("x") == "x"
    assert problem_text({"content": json.dumps({"zh": "你好", "en": "hi"})}) == "你好"
    assert problem_text({"content": "纯文本"}) == "纯文本"
    assert mention(118, "bot") == "@[bot](/user/118)"


def test_split_text_keeps_short_and_bounds_long():
    assert split_text("") == [] and split_text("abc") == ["abc"]
    parts = split_text("\n\n".join(["x" * 900] * 5), limit=1000)
    assert len(parts) == 5 and all(len(p) <= 1000 for p in parts)
    assert all(len(p) <= 100 for p in split_text("y" * 450, limit=100))


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def test_http_success_with_error_body_is_not_success():
    api = make_api(FakeResp(body={"error": {"name": "PermissionError", "code": 403}}))
    with pytest.raises(OJError) as err:
        api.messages.send(35, "测试")
    assert err.value.status == 403


def test_domain_and_paths_reject_traversal():
    api = make_api()
    with pytest.raises(ValueError):
        api.with_domain("../system")
    with pytest.raises(ValueError):
        api.path("/p/../record")
    with pytest.raises(ValueError):
        api.path("/p/%2e%2e/record")


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def make_api(*responses, domain="root_test"):
    api = OJApi("https://oj.example", domain, session=FakeSession(*responses), min_interval=0)
    api._shared["me"] = {"uid": 118, "uname": "bot"}
    return api


def test_request_builds_domain_paths_and_errors():
    api = make_api(FakeResp(body={"ok": 1}),
                   FakeResp(404, {"error": {"name": "NotFound", "message": "无"}}))
    assert api.get("/p/1") == {"ok": 1}
    assert api.session.calls[0][1] == "https://oj.example/d/root_test/p/1"
    with pytest.raises(OJError) as err:
        api.get("/p/2")
    assert err.value.status == 404 and err.value.name == "NotFound"


def test_with_domain_shares_session_and_login_state():
    api = make_api()
    other = api.with_domain("system")
    assert other.session is api.session and other.uid == 118
    assert other.path("/p/1") == "https://oj.example/d/system/p/1"
    assert other.path("/home/messages", domain=False) == "https://oj.example/home/messages"


def test_discussion_reply_payloads():
    api = make_api(FakeResp(body={"drid": "d1"}), FakeResp(body={"url": "/"}))
    assert api.discussions.reply("did", "hi") == "d1"
    assert api.discussions.tail_reply("did", "d1", "x") is True
    first, second = api.session.calls
    assert first[2]["json"] == {"operation": "reply", "content": "hi"}
    assert second[2]["json"]["operation"] == "tail_reply" and second[2]["json"]["drid"] == "d1"


def test_inbox_flattens_threads():
    payload = {"messages": {"1": {"udoc": {"_id": 1, "uname": "Hydro"}, "messages": [MENTION]}}}
    msgs = make_api(FakeResp(body=payload)).messages.inbox()
    assert msgs[0]["peer"] == 1 and msgs[0]["peer_name"] == "Hydro"


def test_records_list_adds_status_name():
    api = make_api(FakeResp(body={"rdocs": [{"_id": "r", "status": 7}]}))
    assert api.records.list(uid=35, pid=26)["rdocs"][0]["status_name"] == "CE"
    assert api.session.calls[0][2]["params"] == {"page": 1, "uidOrName": 35, "pid": 26}


class FakeInboxApi:
    uid = 118

    def __init__(self, msgs):
        inbox = lambda: msgs  # noqa: E731
        self.messages = type("M", (), {"inbox": staticmethod(inbox)})()


def test_inbox_poller_dedupes_and_survives_handler_errors(tmp_path):
    stamp = f"{int(time.time()):08x}"
    old = {**MENTION, "_id": "00000001" + "0" * 16}
    fresh = {**MENTION, "_id": stamp + "0" * 16}
    own = {**fresh, "_id": stamp + "1" * 16, "from": 118}
    got = []

    def handler(msg):
        got.append(msg["_id"])
        raise RuntimeError("boom")

    seen = SeenStore(tmp_path / "s.db", "t")
    poller = InboxPoller(FakeInboxApi([old, fresh, own]), handler, seen, max_age=3600)
    assert poller.poll_once() == 1 and got == [fresh["_id"]]
    assert poller.poll_once() == 0           # 已处理 / 过期 / 自己发的都不再触发
    assert seen.has(old["_id"])              # 过期消息被标记，重启后也不会回放


def test_notifier_respects_event_switch():
    sent = []
    send = lambda uid, text: sent.append((uid, text))  # noqa: E731
    api = type("A", (), {"messages": type("M", (), {"send": staticmethod(send)})()})()
    notifier = Notifier(api, push_list=[2, 35], events={"quiet": False})
    assert notifier.broadcast("quiet", "x") == 0 and notifier.broadcast("loud", "y") == 2
    assert sent == [(2, "y"), (35, "y")]
