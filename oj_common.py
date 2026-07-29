"""
OJ Solver — 共享模块
提供所有脚本共用的：配置加载、登录、Cookie 管理、日志配置
"""

import os
import re
import json
import time
import logging
import requests
from pathlib import Path

# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════
def setup_logging(quiet: bool = False, verbose: bool = False,
                  log_file: str = None, name: str = None):
    """统一日志配置。控制台简洁格式 + 文件详细格式（自动轮转）。
    - quiet: 仅 WARNING+
    - verbose: DEBUG+
    - log_file: 文件路径（支持 {date} 占位符，默认 logs/daemon_{date}.log）
    """
    from datetime import datetime
    from logging.handlers import RotatingFileHandler
    console_level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 根级别放开，由各 handler 控制
    root.handlers.clear()

    # 控制台 — 简洁格式，仅 INFO+
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s",
                                           datefmt="%m-%d %H:%M:%S"))
    root.addHandler(console)

    # 文件 — 详细格式 + 轮转 (10MB×5)
    if log_file:
        if "{date}" in log_file:
            log_file = log_file.replace("{date}", datetime.now().strftime("%Y%m%d"))
    else:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / f"oj_{datetime.now().strftime('%Y%m%d')}.log")
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5,
                                 encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(levelname).1s] %(name)s | %(message)s",
            datefmt="%m-%d %H:%M:%S"))
        root.addHandler(fh)
        root.info("[日志] %s (轮转: 10MBx5)", log_file)
    except OSError as e:
        root.warning("[日志] 文件写入失败: %s", e)

    return logging.getLogger(name or __name__)


# ═══════════════════════════════════════════════════════════════
# 共享推送
# ═══════════════════════════════════════════════════════════════
def push_oj_message(session, root: str, text: str, push_uids: list[int] = None,
                    requester: int = 0):
    """向 OJ 用户发送私信。线程安全（调用方负责 session 管理）。"""
    import requests as _r
    targets = set()
    if requester > 0:
        targets.add(requester)
    if push_uids:
        targets.update(push_uids)
    if not targets:
        return
    for uid in targets:
        try:
            session.post(f"{root}/home/messages",
                         json={"operation": "send", "uid": uid, "content": text},
                         headers={"Accept": "application/json"}, timeout=10)
        except _r.RequestException:
            pass  # 推送失败不影响主流程


# ═══════════════════════════════════════════════════════════════
# Dashboard 共享写操作
# ═══════════════════════════════════════════════════════════════
def append_dashboard_record(record: dict, path: str = "dashboard.json",
                            max_history: int = 500):
    """原子追加一条记录到 dashboard.json。兼容 Dashboard 类的嵌套格式。"""
    try:
        p = Path(path)
        data = {}
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, ValueError):
                pass
        # 保留 Dashboard 类的 problems/contests 结构
        for key in ("problems", "contests"):
            data.setdefault(key, {})
        history = data.get("history", [])
        history.append(record)
        if len(history) > max_history:
            history = history[-max_history:]
        data["history"] = history
        tmp = str(p) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        Path(tmp).replace(p)
    except Exception:
        pass  # dashboard 写入失败不应中断主流程


# ═══════════════════════════════════════════════════════════════
# .env 加载
# ═══════════════════════════════════════════════════════════════
def load_dotenv(env_path: str = ".env"):
    """加载 .env 文件到 os.environ（已存在的变量不覆盖）"""
    p = Path(env_path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = val


# ═══════════════════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════════════════
def load_config(config_path: str = "config.json") -> dict:
    """从 JSON 文件加载配置（文件不存在则返回空）"""
    p = Path(config_path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ═══════════════════════════════════════════════════════════════
# Session 创建（含 HTTP 重试）
# ═══════════════════════════════════════════════════════════════
def create_session(verify_ssl: bool = True) -> requests.Session:
    s = requests.Session()
    if not verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s.verify = verify_ssl
    s.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    from requests.adapters import HTTPAdapter, Retry

    # 区分 429（限流）和其他错误的重试策略
    class RateLimitRetry(Retry):
        RETRY_AFTER_STATUS_CODES = frozenset([413, 429, 503])
        def get_retry_after(self, response):
            if not response:
                return super().get_retry_after(response)
            # urllib3.HTTPResponse 使用 .status，requests.Response 使用 .status_code
            sc = getattr(response, 'status', 0) or getattr(response, 'status_code', 0)
            if sc == 429:
                # 429 默认等 10s（若服务端未返回 Retry-After 头）
                retry_after = super().get_retry_after(response)
                return retry_after if retry_after else 3
            return super().get_retry_after(response)

    retry = RateLimitRetry(total=3, backoff_factor=2,
                           status_forcelist=[429, 502, 503, 504],
                           allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


# ═══════════════════════════════════════════════════════════════
# Cookie 持久化
# ═══════════════════════════════════════════════════════════════
def load_cookies(session: requests.Session, path: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        logging.getLogger(__name__).debug("[*] 已加载 %d 条 cookie", len(cookies))
        return True
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger(__name__).warning("[!] Cookie 加载失败: %s", e)
        return False


def save_cookies(session: requests.Session, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"name": c.name, "value": c.value, "domain": c.domain}
                   for c in session.cookies], f)
    try: os.chmod(path, 0o600)
    except OSError: pass


# ═══════════════════════════════════════════════════════════════
# OJ 登录
# ═══════════════════════════════════════════════════════════════
def oj_login(session: requests.Session, root: str, username: str, password: str,
             max_retries: int = 3) -> bool:
    """登录 OJ，返回是否成功。自动重试网络错误。"""
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            time.sleep(3)
        try:
            session.get(f"{root}/login", timeout=15)
            resp = session.post(
                f"{root}/login",
                data={"uname": username, "password": password,
                      "rememberme": "on", "tfa": "", "authnChallenge": "",
                      "login_submit": "登录"},
                allow_redirects=False, timeout=15)
            if resp.status_code in (302, 303):
                loc = resp.headers.get("Location", "/")
                session.get(f"{root}{loc}" if loc.startswith("/") else loc, timeout=10)
                return True
            if "密码错误" in resp.text:
                return False
        except requests.RequestException:
            pass
    return False


# ═══════════════════════════════════════════════════════════════
# 用户 ID
# ═══════════════════════════════════════════════════════════════
def fetch_user_id(session: requests.Session, root: str) -> int | None:
    """从首页提取当前用户 ID"""
    try:
        r = session.get(f"{root}/", timeout=15)
        m = re.search(r"window\.UserContext\s*=\s*'(.+?)';\s*$", r.text, re.MULTILINE)
        if m:
            uctx = json.loads(m.group(1))
            uid = uctx.get("_id")
            if uid:
                return int(uid)
    except (requests.RequestException, json.JSONDecodeError, ValueError):
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# URL 解析
# ═══════════════════════════════════════════════════════════════
def parse_contest_or_problem(url: str) -> dict:
    """解析比赛/训练/题目 URL → {type, base_url, domain_id, pids?, contest_id?}"""
    # 比赛 URL
    m = re.match(r"(https?://[^/]+)/d/([^/]+)/contest/([a-f0-9]+)", url)
    if m:
        return {"type": "contest", "base_url": m.group(1),
                "domain_id": m.group(2), "contest_id": m.group(3)}
    # 训练 URL（支持 /training/{id} 和 /d/{domain}/training/{id}）
    m = re.match(r"(https?://[^/]+)(?:/d/([^/]+))?/training/([a-f0-9]+)", url)
    if m:
        return {"type": "training", "base_url": m.group(1),
                "domain_id": m.group(2) or "system", "training_id": m.group(3)}
    # 带 domain 的题目 URL
    m = re.match(r"(https?://[^/]+)/d/([^/]+)/p(?:roblem)?/([a-zA-Z0-9]+)", url)
    if m:
        return {"type": "problem", "base_url": m.group(1),
                "domain_id": m.group(2), "pids": [m.group(3)],
                "title_prefix": f"单题 P{m.group(3)}"}
    # 根路径题目 URL
    m = re.match(r"(https?://[^/]+)/p(?:roblem)?/([a-zA-Z0-9]+)", url)
    if m:
        return {"type": "problem", "base_url": m.group(1),
                "domain_id": "system", "pids": [m.group(2)],
                "title_prefix": f"单题 P{m.group(2)}"}
    raise ValueError(f"无法解析链接: {url}")


def parse_problem_url(raw: str) -> tuple:
    """解析题目链接 → (root, api_base, pid)"""
    m = re.match(r"(https?://[^/]+)(?:/d/([^/]+))?/p(?:roblem)?/([a-zA-Z0-9]+)", raw)
    if m:
        root, domain, pid = m.group(1), m.group(2), m.group(3)
        return root, f"{root}/d/{domain}" if domain else f"{root}/d/system", pid
    if raw.strip().isdigit():
        return None, None, raw.strip()
    raise ValueError(f"无法从 '{raw}' 中解析出题目 ID")


def parse_root(url: str) -> str:
    """从 URL 提取根地址"""
    m = re.match(r"https?://[^/]+", url)
    if m:
        return m.group(0)
    raise ValueError(f"无法从 '{url}' 提取根地址")
