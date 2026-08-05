#!/usr/bin/env python3
"""消息后端 — OJ 私信收发，与守护进程共享输入队列。"""

import hashlib, json, logging, queue, re, subprocess, sys, threading, time
from pathlib import Path
from datetime import datetime
import requests

SCRIPT_DIR = Path(__file__).parent
log = logging.getLogger(__name__)
MAX_PROCESSED = 300


class MsgBackend:
    """OJ 私信后端 — 轮询消息，白名单指令入队，关键节点推送"""

    def __init__(self, session: requests.Session, root: str, my_uid: int,
                 whitelist: set[str], push_list: set[int],
                 interval: int = 15):
        self.s = session
        self.root = root
        self.my_uid = my_uid
        self.whitelist = whitelist
        self.push_list = push_list
        self.interval = interval
        self.processed: set[str] = set()
        self.cmd_queue: queue.Queue = queue.Queue()
        self._running = False
        self._load_processed()
        # 消息轮询高频且单次失败可快速重试，降低重试次数避免单次 fetch 卡死
        try:
            from requests.adapters import HTTPAdapter, Retry
            low_retry = HTTPAdapter(max_retries=Retry(total=1))
            self.s.mount("https://", low_retry)
            self.s.mount("http://", low_retry)
        except Exception:
            pass

    def send(self, uid: int, text: str):
        try:
            from oj_common import MSG_LIMITER
            MSG_LIMITER.wait()
            self.s.post(f"{self.root}/home/messages",
                json={"operation": "send", "uid": uid, "content": text},
                headers={"Accept": "application/json"}, timeout=15)
        except Exception as e:
            log.warning("[消息] 发送失败: %s", e)

    def push(self, text: str):
        """推送消息给所有推送名单用户"""
        for uid in list(self.push_list):
            self.send(uid, text)

    @staticmethod
    def _msg_key(msg: dict) -> str:
        """生成消息唯一键：内容+时间戳的hash"""
        content = msg.get("content", "")
        mid = msg.get("_id", "")  # MongoDB ObjectId 含时间
        raw = f"{content}|{mid[:8]}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def _load_processed(self):
        try:
            with open(".msg_processed.json", "r") as f:
                self.processed = set(json.load(f))
        except Exception:
            self.processed = set()

    def _save_processed(self):
        try:
            from pathlib import Path
            tmp = ".msg_processed.json.tmp"
            with open(tmp, "w") as f:
                json.dump(list(self.processed)[-500:], f)
            Path(tmp).replace(".msg_processed.json")
        except Exception: pass

    def fetch(self) -> list[dict]:
        try:
            r = self.s.get(f"{self.root}/home/messages",
                           headers={"Accept": "application/json"}, timeout=20)
            if r.status_code != 200: return []
            now = time.time()
            cutoff = now - 120
            msgs = []
            for thread in r.json().get("messages", {}).values():
                if not isinstance(thread, dict): continue
                udoc = thread.get("udoc", {})
                for m in thread.get("messages", []):
                    if m.get("from") == self.my_uid: continue
                    if m.get("flag", 0) != 1: continue
                    mid = m.get("_id", "")
                    if len(mid) >= 8:
                        try:
                            if int(mid[:8], 16) < cutoff: continue
                        except ValueError: pass
                    key = self._msg_key(m)
                    if key in self.processed: continue
                    m["_udoc_uname"] = udoc.get("uname", "?")
                    m["_udoc_id"] = udoc.get("_id", 0)
                    msgs.append(m)
            return msgs
        except Exception as e:
            log.warning("[消息] 获取异常: %s", e)
            raise  # 交给 start 捕获并快速重试，避免长时间错过消息

    def _handle(self, msg: dict):
        uid = msg.get("_udoc_id", 0)
        text = msg.get("content", "").strip()
        key = self._msg_key(msg)
        if key in self.processed: return
        self.processed.add(key)
        self._save_processed()

        if str(uid) not in self.whitelist:
            self.send(uid, "无使用权限，请联系管理员添加权限")
            return
        # 按行分割，逐行处理
        for line in text.split("\n"):
            line = line.strip()
            if not line: continue
            log.info("[消息] #%d: %s", uid, line[:80])
            self.cmd_queue.put((line, uid))

    def stop(self):
        self._running = False

    def start(self):
        self._running = True
        log.info("[消息] 轮询启动 (间隔%ds) | 白名单: %s", self.interval, self.whitelist)
        while self._running:
            try:
                for msg in self.fetch():
                    self._handle(msg)
                time.sleep(self.interval)
            except Exception as e:
                log.warning("[消息] 获取失败，3s 后重试: %s", e)
                time.sleep(3)  # 失败快速重试，不等待整个轮询间隔

    def start_async(self) -> threading.Thread:
        t = threading.Thread(target=self.start, daemon=True)
        t.start()
        return t
