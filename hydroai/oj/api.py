"""ojkit 核心：带登录恢复、限速与统一错误的 HydroOJ HTTP 网关。

所有「与 OJ 交互」的新能力（题目、记录、讨论、题解、私信、比赛、用户…）都挂在
:class:`OJApi` 上，按资源拆分在 :mod:`hydroai.oj.resources`。求解、学情诊断、
@ 助手等模块只依赖这一层，不直接拼 URL。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import unquote

import requests

from hydroai.oj.common import (RateLimiter, create_session, fetch_user_id,
                               load_cookies, oj_login, save_cookies)

log = logging.getLogger(__name__)
JSON_HEADERS = {"Accept": "application/json"}


class OJError(RuntimeError):
    """OJ 返回非 2xx，或响应不是预期结构。"""

    def __init__(self, message: str, status: int = 0, name: str = ""):
        super().__init__(message)
        self.status = status
        self.name = name


class OJApi:
    """一个已登录会话 + 一个当前域。``with_domain`` 复用同一会话切换域。"""

    @staticmethod
    def _new_session():
        session = create_session()
        for adapter in set(session.adapters.values()):
            adapter.max_retries = adapter.max_retries.new(allowed_methods=frozenset({"GET", "HEAD"}))
        return session

    def __init__(self, root: str, domain: str = "system", *, username: str = "",
                 password: str = "", cookie_jar: str = "", session=None,
                 min_interval: float = 0.3, retry_message_403: bool = False,
                 _shared: dict | None = None):
        if domain and not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", domain):
            raise ValueError("无效的 Hydro 域 ID")
        self.root = root.rstrip("/")
        self.domain = domain
        # 登录态属于「会话」，多个域视图共享，避免重复登录
        self._shared = _shared if _shared is not None else {
            "session": session if session is not None else self._new_session(),
            "username": username, "password": password,
            "cookie_jar": cookie_jar, "me": None,
            "lock": threading.RLock(), "limiter": RateLimiter(min_interval),
            "relogin_at": 0.0, "retry_message_403": retry_message_403,
        }
        if _shared is None and cookie_jar:
            load_cookies(self.session, cookie_jar)
        from hydroai.oj.resources import (Contests, Discussions, Homework, Messages,
                                          Problems, Records, Solutions, Trainings, Users)
        self.problems = Problems(self)
        self.records = Records(self)
        self.discussions = Discussions(self)
        self.solutions = Solutions(self)
        self.messages = Messages(self)
        self.contests = Contests(self)
        self.trainings = Trainings(self)
        self.homework = Homework(self)
        self.users = Users(self)

    # ---- 构造 ----
    @classmethod
    def from_config(cls, config, domain: str | None = None) -> "OJApi":
        root = config["oj_root"].rstrip("/")
        if domain is None:
            base = str(config.get("oj_base", "") or "")
            domain = base.rsplit("/d/", 1)[1].strip("/") if "/d/" in base else "system"
        return cls(root, domain, username=config.username, password=config.password,
                   cookie_jar=config.get("cookie_jar", "") or "")

    def with_domain(self, domain: str) -> "OJApi":
        if domain == self.domain:
            return self
        return OJApi(self.root, domain, _shared=self._shared)

    # ---- 会话 ----
    @property
    def session(self) -> requests.Session:
        return self._shared["session"]

    @property
    def me(self) -> dict:
        """当前登录用户 ``{"uid", "uname"}``；未登录返回空字典。"""
        return self._shared["me"] or {}

    @property
    def uid(self) -> int:
        return int(self.me.get("uid", 0) or 0)

    def login(self, force: bool = False) -> bool:
        sh = self._shared
        with sh["lock"]:
            if sh["me"] and not force:
                return True
            if not force:
                me = self._whoami()
                if me and (not sh["username"] or me.get("uname", "").casefold() == sh["username"].casefold()):
                    sh["me"] = me
                    return True
            sh["me"] = None
            if not sh["username"] or not sh["password"]:
                log.error("[-] 未配置 OJ 凭据")
                return False
            self.session.cookies.clear()
            if not oj_login(self.session, self.root, sh["username"], sh["password"]):
                return False
            me = self._whoami()
            if not me or me.get("uname", "").casefold() != sh["username"].casefold():
                return False
            if sh["cookie_jar"]:
                save_cookies(self.session, sh["cookie_jar"])
            sh["me"] = me
            return True

    def _whoami(self) -> dict:
        uid = fetch_user_id(self.session, self.root) or 0
        if not uid:
            return {}
        uname = ""
        try:
            data = self.session.get(f"{self.root}/user/{uid}", headers=JSON_HEADERS,
                                    timeout=15).json()
            uname = (data.get("udoc") or {}).get("uname", "")
        except (requests.RequestException, ValueError):
            pass
        return {"uid": uid, "uname": uname}

    # ---- 请求 ----
    def path(self, path: str, *, domain: bool = True) -> str:
        clean_path = unquote(path)
        if (not path.startswith("/") or clean_path.startswith("//") or "\\" in clean_path
                or any(seg in {".", ".."} for seg in clean_path.split("/"))
                or re.search(r"[\x00-\x20?#]", path)):
            raise ValueError("必须使用站内资源路径，查询参数用 params 传入")
        if not domain or not self.domain:
            return f"{self.root}{path}"
        return f"{self.root}/d/{self.domain}{path}"

    def request(self, method: str, path: str, *, domain: bool = True, params=None,
                json=None, data=None, raw: bool = False, timeout: int = 30,
                allow_redirects: bool = False):
        """只读请求可恢复会话；旧私信通道可显式启用确定 403 后重试。"""
        url = self.path(path, domain=domain)
        for attempt in (1, 2):
            self._shared["limiter"].wait()
            try:
                resp = self.session.request(method, url, params=params, json=json,
                                            data=data, headers=JSON_HEADERS,
                                            timeout=timeout,
                                            allow_redirects=allow_redirects)
            except requests.RequestException as exc:
                raise OJError(f"网络错误: {type(exc).__name__}") from exc
            legacy_message = (self._shared.get("retry_message_403") and method == "POST"
                              and not domain and path == "/home/messages"
                              and isinstance(json, dict) and json.get("operation") == "send")
            can_recover = ((method == "GET" and resp.status_code in (401, 403))
                           or (legacy_message and resp.status_code == 403))
            if can_recover and attempt == 1 and self._relogin():
                continue
            break
        if raw:
            return resp
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        err = payload.get("error") if isinstance(payload, dict) else None
        if err or not 200 <= resp.status_code < 300:
            err = err if isinstance(err, dict) else {}
            status = err.get("code") if resp.status_code == 200 else resp.status_code
            status = status if type(status) is int else resp.status_code
            name = str(err.get("name", ""))[:80]
            raise OJError(f"{method} {path} → HTTP {status} {name}".rstrip(), status, name)
        if not isinstance(payload, dict):
            raise OJError(f"{method} {path} 未返回预期的 JSON 对象", resp.status_code)
        return payload

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw):
        return self.request("POST", path, **kw)

    def _relogin(self) -> bool:
        sh = self._shared
        with sh["lock"]:
            if time.monotonic() - sh["relogin_at"] < 30:
                return False
            sh["relogin_at"] = time.monotonic()
            return self.login(force=True)
