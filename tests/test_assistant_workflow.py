"""复现 Hydro @ 私信协议、公开/私信边界与实际发送结果。"""
import json
import time
from types import SimpleNamespace

from hydroai.assistant.agent import parse_action
from hydroai.assistant.outbox import has_full_code
from hydroai.assistant.service import AssistantService

DID = "a" * 24
RID = "b" * 24
PSID = "c" * 24


def notice(author="student", domain="root_test", host=""):
    return {"_id": f"{int(time.time()):08x}" + "f" * 16, "from": 1, "flag": 5, "to": [118],
            "content": json.dumps({"message": "User {0} mentioned you in {1:link}",
                                   "params": [author, f"{host}/d/{domain}/discuss/{DID}"]})}


def fake_api(msg=None, *, hidden=False, pm_ok=True):
    writes = []
    def log(*args):
        writes.append(args)
        return True

    thread = {"ddoc": {"owner": 35, "domainId": "root_test", "title": "求思路", "content": "如何推导？",
                       "parentType": 10, "parentId": 26, "hidden": hidden},
              "drdocs": [{"_id": "d" * 24, "owner": 35, "content": "@[](/user/118)"},
                         {"_id": "e" * 24, "owner": 36, "content": "@[](/user/118)"}],
              "udict": {"35": {"uname": "student"}, "36": {"uname": "other"}}}
    api = SimpleNamespace(root="https://oj.example", domain="root_test", uid=118, me={"uname": "bot"},
                          login=lambda: True)
    api.with_domain = lambda domain: api
    api.discussions = SimpleNamespace(get_full=lambda did: thread,
                                      reply=lambda did, text: log("reply", did, text),
                                      tail_reply=lambda *args: log("tail", *args))
    api.problems = SimpleNamespace(detail=lambda pid: {"title": "测试", "content": "题面", "tags": []})
    api.records = SimpleNamespace(list=lambda **kw: {"rdocs": []})
    api.solutions = SimpleNamespace(list=lambda pid: {"psdocs": [{"_id": PSID, "content": "说明"}]},
                                    url=lambda pid: f"https://oj.example/d/root_test/p/{pid}/solution",
                                    reply=lambda *args: log("solution", *args))
    api.messages = SimpleNamespace(inbox=lambda: [msg] if msg else [],
                                   send=lambda uid, text: log("private", uid, text) if pm_ok else False)
    return api, writes


def model(*actions):
    actions = iter(actions)
    messages_seen = []
    def call(messages):
        messages_seen.append(json.dumps(messages, ensure_ascii=False))
        action, args = next(actions)
        return {"content": json.dumps({"action": action, "args": args}, ensure_ascii=False)}
    return call, messages_seen


def test_mention_matches_notification_author_and_routes_private_code():
    api, writes = fake_api()
    called = []
    def student(uid, pid, *, analyze):
        called.append((uid, pid, analyze))
        return {"attempts": 2, "best_submission": {"rid": RID, "code": "private_source"}}
    call, seen = model(("reply_discussion", {"content": "private_source"}),
                       ("send_private", {"uid": 36, "content": "private_source"}),
                       ("send_private", {"uid": 35, "content": "请检查循环边界。"}), ("finish", {}))
    service = AssistantService(api, {"assistant_domains": ["root_test"]}, call_fn=call,
                               insight=SimpleNamespace(student_context=student))
    assert service.handle_message(notice()).status == "replied"
    assert called == [(35, "26", False)]
    assert "private_source" in seen[0]
    assert [(kind, target) for kind, target, _ in writes] == [("private", 35)]


def test_unauthorized_author_or_external_notice_does_not_fetch_student_data():
    api, writes = fake_api()
    def forbidden(*args, **kwargs):
        raise AssertionError("不应读取学生代码或调用模型")
    svc = AssistantService(api, {"assistant_domains": ["root_test"], "assistant_users": [36]},
                           call_fn=forbidden, insight=SimpleNamespace(student_context=forbidden))
    assert svc.handle_message(notice()).status == "ignored"
    assert svc.handle_message(notice(host="https://other.example")).status == "ignored"
    assert svc.handle_message(notice(domain="system")).status == "ignored"
    assert not writes


def test_public_channel_cannot_read_arbitrary_resources_and_can_link_editorial():
    api, writes = fake_api()
    call, seen = model(("read_problem", {"pid": "999"}),
                       ("read_submission", {"rid": RID}),
                       ("post_solution_reply", {"psid": PSID, "content": "可从状态定义入手。"}),
                       ("reply_discussion", {"content": "我在题解下补充了状态定义。"}),
                       ("finish", {}))
    svc = AssistantService(api, {"assistant_domains": ["root_test"]}, call_fn=call, insight=None)
    assert svc.handle_message(notice()).status == "replied"
    assert [w[0] for w in writes] == ["solution", "reply"]
    assert "@[](/user/35)" in writes[-1][-1]
    assert "https://oj.example/d/root_test/p/26/solution" in writes[-1][-1]
    assert "private_source" not in seen[-1]


def test_inbox_dry_run_does_not_consume_live_event():
    api, writes = fake_api(notice())
    config = {"assistant_domains": ["root_test"]}
    def setup(dry):
        call, _ = model(("reply_discussion", {"content": "请从状态定义入手。"}), ("finish", {}))
        return AssistantService(api, config, call_fn=call, insight=None, dry_run=dry)
    assert setup(True).run(once=True) == 1
    assert not writes
    assert setup(False).run(once=True) == 1
    assert setup(False).run(once=True) == 0
    assert len(writes) == 1


def test_failed_private_delivery_is_not_recorded_as_sent():
    api, _ = fake_api(hidden=True, pm_ok=False)
    call, _ = model(("send_private", {"uid": 35, "content": "提示"}), ("finish", {}))
    svc = AssistantService(api, {"assistant_domains": ["root_test"]}, call_fn=call, insight=None)
    result = svc.handle_message(notice())
    assert result.status == "no_reply"
    assert result.replies == 0


def test_rejects_multiple_or_nested_model_actions():
    valid = '{"action":"finish","args":{}}'
    assert parse_action(f"```json\n{valid}\n```")["action"] == "finish"
    assert parse_action(valid + valid) is None
    assert parse_action('{"data":' + valid + '}') is None


def test_guide_blocks_complete_program_but_allows_short_fragments():
    assert has_full_code('```cpp\nint main() {return 0;}\n```')
    assert not has_full_code('```cpp\nvalue += delta;\n```')
