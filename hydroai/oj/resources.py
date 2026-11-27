"""ojkit 资源层：把 HydroOJ 的页面 JSON 接口整理成按资源划分的方法。

所有方法只做「请求 + 规整」，不含业务判断；返回值保持 Hydro 原字段，
仅在需要时补充少量派生字段（如 ``status_name``）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterator

from hydroai.oj.api import OJError

# Hydro 的评测状态码（record.status）
STATUS_NAMES = {0: "WAITING", 1: "AC", 2: "WA", 3: "TLE", 4: "MLE", 5: "OLE", 6: "RE",
                7: "CE", 8: "SE", 9: "CANCELED", 10: "ETC", 11: "HACK_OK", 12: "HACK_FAIL",
                13: "IGNORED", 20: "FETCHED", 21: "COMPILING", 22: "JUDGING"}
STATUS_AC = 1
STATUS_PENDING = {0, 20, 21, 22}


def status_name(code) -> str:
    try:
        return STATUS_NAMES.get(int(code), str(code))
    except (TypeError, ValueError):
        return str(code)


def problem_text(pdoc: dict, lang: str = "zh") -> str:
    """题面正文。Hydro 的 content 常为 ``{"zh": "...", "en": "..."}`` 的 JSON 字符串。"""
    raw = (pdoc or {}).get("content", "")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, dict):
        return raw.get(lang) or next((v for v in raw.values() if v), "")
    return str(raw or "")


class _Resource:
    def __init__(self, api):
        self.api = api


class Problems(_Resource):
    def list(self, page: int = 1, q: str = "", sort: str = "") -> dict:
        params = {"page": page}
        if q:
            params["q"] = q
        if sort:
            params["sort"] = sort
        return self.api.get("/p", params=params)

    def iter_all(self, q: str = "", max_pages: int = 200) -> Iterator[dict]:
        for page in range(1, max_pages + 1):
            data = self.list(page, q)
            yield from data.get("pdocs", [])
            if page >= int(data.get("ppcount", 1) or 1):
                return

    def get(self, pid) -> dict:
        """返回原始页面 JSON，常用字段：``pdoc``、``psdoc``（我的状态）。"""
        return self.api.get(f"/p/{pid}")

    def detail(self, pid) -> dict:
        data = self.get(pid)
        pdoc = data.get("pdoc") or {}
        return {"pid": pdoc.get("pid") or pdoc.get("docId"), "docId": pdoc.get("docId"),
                "title": pdoc.get("title", ""), "content": problem_text(pdoc),
                "tags": pdoc.get("tag", []), "stats": pdoc.get("stats", {}),
                "config": pdoc.get("config", {}), "data": pdoc.get("data", []),
                "owner": pdoc.get("owner"), "raw": pdoc}

    def titles(self, pids) -> dict:
        """批量取题名：依赖列表接口 ``pdict``；拿不到就逐题取。"""
        out = {}
        for pid in pids:
            try:
                out[pid] = self.get(pid).get("pdoc", {}).get("title", "")
            except Exception:
                out[pid] = ""
        return out


class Records(_Resource):
    def list(self, *, uid=None, pid=None, status=None, lang: str = "", tid: str = "",
             page: int = 1, all_domain: bool = False) -> dict:
        params = {"page": page}
        if uid is not None:
            params["uidOrName"] = uid
        if pid is not None:
            params["pid"] = pid
        if status is not None:
            params["status"] = status
        if lang:
            params["lang"] = lang
        if tid:
            params["tid"] = tid
        if all_domain:
            params["allDomain"] = "on"
        data = self.api.get("/record", params=params)
        for rdoc in data.get("rdocs", []):
            rdoc["status_name"] = status_name(rdoc.get("status"))
        return data

    def iter_list(self, max_pages: int = 20, **filters) -> Iterator[dict]:
        for page in range(1, max_pages + 1):
            rdocs = self.list(page=page, **filters).get("rdocs", [])
            if not rdocs:
                return
            yield from rdocs

    def get(self, rid: str) -> dict:
        """完整记录：含 ``code``、``testCases``、``compilerTexts``、``judgeTexts``。"""
        data = self.api.get(f"/record/{rid}")
        rdoc = data.get("rdoc") or {}
        rdoc["status_name"] = status_name(rdoc.get("status"))
        data["pdoc"] = data.get("pdoc") or {}
        return data

    def submit(self, pid, code: str, lang: str = "cc.cc14o2", tid: str = "") -> str:
        """提交代码，返回记录 id（比赛内提交传 ``tid``）。"""
        form = {"lang": lang, "code": code}
        if tid:
            form["tid"] = tid
        resp = self.api.post(f"/p/{pid}/submit", data=form, raw=True, allow_redirects=False)
        if resp.status_code in (302, 303):
            match = re.search(r"/record/([a-f0-9]+)(?:[/?#]|$)",
                              resp.headers.get("Location", ""))
            if match:
                return match.group(1)
        elif resp.status_code == 200:
            try:
                payload = resp.json()
                rid = payload.get("rid", "") if isinstance(payload, dict) and not payload.get("error") else ""
                if isinstance(rid, str) and re.fullmatch(r"[a-f0-9]+", rid):
                    return rid
            except ValueError:
                pass
        raise OJError(f"提交失败: {resp.status_code}", resp.status_code)

    def wait(self, rid: str, timeout: float = 60, interval: float = 2) -> dict:
        """轮询到评测结束（或超时）并返回 rdoc；超时返回最后一次看到的 rdoc。"""
        deadline, rdoc = time.monotonic() + timeout, {}
        while True:
            rdoc = self.get(rid)["rdoc"]
            if int(rdoc.get("status", 0)) not in STATUS_PENDING or time.monotonic() >= deadline:
                return rdoc
            time.sleep(interval)

    def rejudge(self, rid: str) -> bool:
        self.api.post(f"/record/{rid}", json={"operation": "rejudge"})
        return True


class Discussions(_Resource):
    def list(self, page: int = 1, *, node_type: str = "", node: str = "") -> dict:
        path = f"/discuss/{node_type}/{node}" if node_type and node else "/discuss"
        return self.api.get(path, params={"page": page})

    def get(self, did: str, page: int = 1) -> dict:
        return self.api.get(f"/discuss/{did}", params={"page": page})

    def get_full(self, did: str, max_pages: int = 10) -> dict:
        """合并所有分页，返回 ``{ddoc, drdocs, udict, vnode}``。"""
        first = self.get(did)
        drdocs = list(first.get("drdocs", []))
        udict = dict(first.get("udict", {}))
        for page in range(2, min(int(first.get("pcount", 1) or 1), max_pages) + 1):
            more = self.get(did, page)
            drdocs += more.get("drdocs", [])
            udict.update(more.get("udict", {}))
        return {"ddoc": first.get("ddoc", {}), "drdocs": drdocs, "udict": udict,
                "vnode": first.get("vnode") or {}, "path": first.get("path", [])}

    def create(self, title: str, content: str, *, node_type: str = "problem",
               node="") -> str:
        """在题目/节点下发帖，返回讨论 id。"""
        data = self.api.post(f"/discuss/{node_type}/{node}/create",
                             data={"title": title, "content": content,
                                   "highlight": "", "pin": ""})
        return data.get("did", "")

    def reply(self, did: str, content: str) -> str:
        data = self.api.post(f"/discuss/{did}", json={"operation": "reply",
                                                      "content": content})
        return data.get("drid", "")

    def tail_reply(self, did: str, drid: str, content: str) -> bool:
        self.api.post(f"/discuss/{did}", json={"operation": "tail_reply",
                                               "drid": drid, "content": content})
        return True

    def delete_reply(self, did: str, drid: str) -> bool:
        self.api.post(f"/discuss/{did}", json={"operation": "delete_reply", "drid": drid})
        return True

    def delete_tail_reply(self, did: str, drid: str, drrid: str) -> bool:
        self.api.post(f"/discuss/{did}", json={"operation": "delete_tail_reply",
                                               "drid": drid, "drrid": drrid})
        return True

    def url(self, did: str) -> str:
        return self.api.path(f"/discuss/{did}")


class Solutions(_Resource):
    def list(self, pid, page: int = 1) -> dict:
        return self.api.get(f"/p/{pid}/solution", params={"page": page})

    def post(self, pid, content: str) -> str:
        data = self.api.post(f"/p/{pid}/solution",
                             json={"operation": "submit", "content": content})
        return data.get("psid", "")

    def reply(self, pid, psid: str, content: str) -> bool:
        self.api.post(f"/p/{pid}/solution", json={"operation": "reply", "psid": psid,
                                                  "content": content})
        return True

    def url(self, pid) -> str:
        return self.api.path(f"/p/{pid}/solution")


class Messages(_Resource):
    def inbox(self) -> list[dict]:
        """所有会话消息，扁平化并带上对方信息：``[{...msg, peer, peer_name}]``。"""
        data = self.api.get("/home/messages", domain=False)
        out = []
        for peer, thread in (data.get("messages") or {}).items():
            if not isinstance(thread, dict):
                continue
            udoc = thread.get("udoc") or {}
            for msg in thread.get("messages", []):
                msg = dict(msg)
                msg["peer"] = int(udoc.get("_id", peer) or 0)
                msg["peer_name"] = udoc.get("uname", "")
                out.append(msg)
        out.sort(key=lambda m: m.get("_id", ""))
        return out

    def send(self, uid: int, text: str) -> bool:
        self.api.post("/home/messages", domain=False,
                      json={"operation": "send", "uid": int(uid), "content": text})
        return True


class Contests(_Resource):
    def list(self, page: int = 1, rule: str = "") -> dict:
        return self.api.get("/contest", params={"page": page, "rule": rule})

    def get(self, tid: str) -> dict:
        return self.api.get(f"/contest/{tid}")

    def scoreboard(self, tid: str) -> dict:
        return self.api.get(f"/contest/{tid}/scoreboard")

    def attend(self, tid: str) -> bool:
        """兼容旧报名接口：发送 JSON，以 HTTP 200 确认，响应可无 JSON。"""
        response = self.api.post(f"/contest/{tid}", json={"operation": "attend"}, raw=True, timeout=15)
        return response.status_code == 200


class Homework(_Resource):
    def list(self, page: int = 1) -> dict:
        return self.api.get("/homework", params={"page": page})

    def get(self, tid: str) -> dict:
        return self.api.get(f"/homework/{tid}")

    def scoreboard(self, tid: str) -> dict:
        return self.api.get(f"/homework/{tid}/scoreboard")


class Trainings(_Resource):
    def list(self, page: int = 1) -> dict:
        return self.api.get("/training", params={"page": page})

    def get(self, tid: str) -> dict:
        return self.api.get(f"/training/{tid}")


class Users(_Resource):
    def get(self, uid: int) -> dict:
        """用户主页 JSON：``udoc``（资料）、``pdocs``（出过的题）。"""
        return self.api.get(f"/user/{int(uid)}", domain=False)

    def resolve_uid(self, username: str) -> int:
        if not username or len(username) > 128:
            return 0
        data = self.api.get("/api/user", domain=False,
                            params={"domainId": self.api.domain or "system", "uname": username})
        user = data.get("user") or data.get("udoc") or data
        if not isinstance(user, dict) or str(user.get("uname", "")).casefold() != username.casefold():
            return 0
        return int(user.get("_id") or 0)

    def name(self, uid) -> str:
        try:
            return (self.get(uid).get("udoc") or {}).get("uname", "")
        except (OJError, ValueError):
            return ""

    def ranking(self, page: int = 1) -> dict:
        return self.api.get("/ranking", params={"page": page})


def mention(uid, uname: str) -> str:
    """Hydro 的 @ 语法。"""
    return f"@[{uname}](/user/{uid})"


_RECORD_RE = re.compile(r"/record/([0-9a-f]{24})")


def record_id_from(text: str) -> str:
    m = _RECORD_RE.search(text or "")
    return m.group(1) if m else ""
