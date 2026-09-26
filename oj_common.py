"""
OJ Solver — 共享模块
提供所有脚本共用的：配置加载、登录、Cookie 管理、日志配置
"""

import os
import re
import json
import time
import threading
import logging
import requests
from pathlib import Path
from urllib.parse import urlparse

# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════
def setup_logging(quiet: bool = False, verbose: bool = False,
                  log_file: str = None, name: str = None):
    """统一日志配置。控制台简洁格式 + 文件详细格式（自动轮转）。
    - quiet: 仅 WARNING+
    - verbose: DEBUG+
    - log_file: 文件路径（支持 {date} 占位符，默认 logs/daemon_{date}.log）

    可重复调用：旧 handler 会被关闭并移除，避免同一进程内多次初始化
    （如 WebUI 调用子模块）导致日志重复输出与文件句柄泄漏。
    """
    from datetime import datetime
    from logging.handlers import RotatingFileHandler
    console_level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # 根级别放开，由各 handler 控制
    for old in list(root.handlers):
        root.removeHandler(old)
        try:
            old.close()  # 关闭文件句柄，否则 Windows 上无法删除/轮转日志
        except Exception:
            pass

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
# 通用限速器 + 共享推送
# ═══════════════════════════════════════════════════════════════
class RateLimiter:
    """进程级限速器 — 两次 wait() 至少间隔 min_interval 秒。线程安全。"""
    def __init__(self, min_interval: float = 2.0):
        self._min = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            gap = self._min - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()

MSG_LIMITER = RateLimiter(0.6)  # 私信推送限速，避免突发 POST 触发 OJ WAF 403

_last_relogin = 0.0
_relogin_lock = threading.Lock()


def _recover_login(session, root: str):
    """WAF 临时拦截（403）后重新登录。两次重登至少间隔 30s。"""
    global _last_relogin
    with _relogin_lock:
        now = time.monotonic()
        if now - _last_relogin < 30:
            return
        _last_relogin = now
    try:
        username = os.environ.get("OJ_USERNAME", "")
        password = os.environ.get("OJ_PASSWORD", "")
        jar = os.environ.get("OJ_COOKIE_JAR", ".oj_cookies.json")
        if smart_login(session, root, username, password, jar):
            logging.getLogger(__name__).info("[*] 403 后重新登录成功")
    except Exception:
        pass


def push_oj_message(session, root: str, text: str, push_uids: list[int] = None,
                    requester: int = 0):
    """向 OJ 用户发送私信。限速 + 403 自动重新登录重试。"""
    targets = set()
    if requester > 0:
        targets.add(requester)
    if push_uids:
        targets.update(push_uids)
    if not targets:
        return
    got_403 = _send_round(session, root, text, targets)
    # 403 后重新登录并重试一轮
    if got_403:
        logging.getLogger(__name__).warning("[!] 推送 403，重新登录后重试")
        _recover_login(session, root)
        _send_round(session, root, text, targets)


def _send_round(session, root: str, text: str, targets: set) -> bool:
    """发送一轮私信（带限速），返回本轮是否出现 403。

    以返回值而非模块级全局变量传递状态：守护进程/并发求解下多个线程会同时
    推送，全局标志会互相覆盖，导致漏掉或误触发重新登录。
    """
    MSG_LIMITER.wait()
    got_403 = False
    for uid in targets:
        try:
            r = session.post(f"{root}/home/messages",
                             json={"operation": "send", "uid": uid, "content": text},
                             headers={"Accept": "application/json"}, timeout=10)
            if r.status_code == 403:
                got_403 = True
        except requests.RequestException:
            pass  # 推送失败不影响主流程
    return got_403


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
# 单题累计费用（跨调用/跨进程持久化，用于 max_cost_per_problem 长期限额）
# ═══════════════════════════════════════════════════════════════
_accum_path = "cost_accum.json"
_accum_cache: dict | None = None
_accum_lock = threading.Lock()


def load_cost_accum() -> dict:
    """读取单题累计费用 {pid: {"total": float, "updated": ts}}"""
    global _accum_cache
    with _accum_lock:
        if _accum_cache is None:
            try:
                with open(_accum_path, "r", encoding="utf-8") as f:
                    _accum_cache = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError):
                _accum_cache = {}
        return _accum_cache


def accum_get(pid) -> float:
    """读取某题累计费用"""
    d = load_cost_accum()
    try:
        return float(d.get(str(pid), {}).get("total", 0.0))
    except (TypeError, ValueError):
        return 0.0


def accum_add(pid, cost: float) -> float:
    """将 cost 累加到 pid 并原子写回，返回新累计值。线程安全。"""
    global _accum_cache
    with _accum_lock:  # 锁内直接操作缓存，避免重入（threading.Lock 不可重入）
        if _accum_cache is None:
            try:
                with open(_accum_path, "r", encoding="utf-8") as f:
                    _accum_cache = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError):
                _accum_cache = {}
        d = _accum_cache
        try:
            cur = float(d.get(str(pid), {}).get("total", 0.0))
        except (TypeError, ValueError):
            cur = 0.0
        new = round(cur + cost, 4)
        d[str(pid)] = {"total": new, "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            tmp = _accum_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, _accum_path)
        except OSError:
            pass  # 累计写入失败不影响主流程
        return new


# ═══════════════════════════════════════════════════════════════
# .env 加载
# ═══════════════════════════════════════════════════════════════
def load_dotenv(env_path: str = ".env"):
    """加载 .env 文件到 os.environ（已存在的变量不覆盖）。

    支持 `export KEY=value`、行内注释、单双引号包裹的值，以及 Windows 下
    用记事本保存出的 UTF-8 BOM。
    """
    p = Path(env_path)
    if not p.exists():
        return
    with open(p, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # 引号内的 # 属于值本身；未加引号时 # 视为行内注释
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            else:
                val = val.split("#", 1)[0].strip()
                val = val.strip('"').strip("'")
            if not key:
                continue
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

    # 多线程提交（contest_solver 默认 4 线程）时连接池需大于默认的 10，
    # 否则并发请求会退化为串行等待空闲连接。
    retry = RateLimitRetry(total=3, backoff_factor=2,
                           status_forcelist=[429, 502, 503, 504],
                           allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ═══════════════════════════════════════════════════════════════
# 智能登录 — cookie 复用减少登录频率
# ═══════════════════════════════════════════════════════════════
def smart_login(session, root: str, username: str, password: str,
                cookie_jar: str = ".oj_cookies.json") -> bool:
    """智能登录：优先复用 cookie，仅失效时重新登录。"""
    # 1. 尝试加载 cookie
    if load_cookies(session, cookie_jar):
        if _cookie_session_valid(session, root):
            return True
    # 2. 完整登录
    if oj_login(session, root, username, password):
        save_cookies(session, cookie_jar)
        return True
    return False


def _cookie_session_valid(session, root: str) -> bool:
    """校验已加载的 cookie 是否仍是登录态。

    先看题目接口（最便宜）；若该域无 P1 或返回异常，再回退到首页检查
    `window.UserContext`。避免把「接口 404」误判成「cookie 失效」而每次
    都重新登录。
    """
    try:
        r = session.get(f"{root}/d/system/p/1",
                        headers={"Accept": "application/json"}, timeout=10)
        if r.status_code == 200:
            return True
    except requests.RequestException:
        return False  # 网络异常：交给 oj_login 重试，避免雪崩式重登
    try:
        r = session.get(f"{root}/", timeout=10)
        return r.status_code == 200 and "window.UserContext" in r.text
    except requests.RequestException:
        return False


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
            name, value = c.get("name"), c.get("value")
            if not name:
                continue
            session.cookies.set(name, value or "", domain=c.get("domain") or "")
        logging.getLogger(__name__).debug("[*] 已加载 %d 条 cookie", len(cookies))
        return True
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger(__name__).warning("[!] Cookie 加载失败: %s", e)
        return False


def save_cookies(session: requests.Session, path: str):
    """原子写入 cookie jar，避免进程被强杀时留下半截文件。"""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump([{"name": c.name, "value": c.value, "domain": c.domain}
                   for c in session.cookies], f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上无意义，但 POSIX 下限制权限


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
# 比赛/训练 ID 不一定是十六进制（Hydro 允许自定义 ID），统一用宽松字符集。
_ID = r"[A-Za-z0-9_-]+"


def _strip_url_noise(url: str) -> str:
    """去掉查询串/锚点/末尾斜杠，粘贴带 ?tid= 的链接也能解析。"""
    return url.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def parse_contest_or_problem(url: str) -> dict:
    """解析比赛/训练/题目 URL → {type, base_url, domain_id, pids?, contest_id?}"""
    url = _strip_url_noise(url)
    # 比赛 URL
    m = re.match(rf"(https?://[^/]+)/d/({_ID})/contest/({_ID})", url)
    if m:
        return {"type": "contest", "base_url": m.group(1),
                "domain_id": m.group(2), "contest_id": m.group(3)}
    # 训练 URL（支持 /training/{id} 和 /d/{domain}/training/{id}）
    m = re.match(rf"(https?://[^/]+)(?:/d/({_ID}))?/training/({_ID})", url)
    if m:
        return {"type": "training", "base_url": m.group(1),
                "domain_id": m.group(2) or "system", "training_id": m.group(3)}
    # 带 domain 的题目 URL
    m = re.match(rf"(https?://[^/]+)/d/({_ID})/p(?:roblem)?/({_ID})", url)
    if m:
        return {"type": "problem", "base_url": m.group(1),
                "domain_id": m.group(2), "pids": [m.group(3)],
                "title_prefix": f"单题 P{m.group(3)}"}
    # 根路径题目 URL
    m = re.match(rf"(https?://[^/]+)/p(?:roblem)?/({_ID})", url)
    if m:
        return {"type": "problem", "base_url": m.group(1),
                "domain_id": "system", "pids": [m.group(2)],
                "title_prefix": f"单题 P{m.group(2)}"}
    raise ValueError(f"无法解析链接: {url}")


def parse_problem_url(raw: str) -> tuple:
    """解析题目链接 → (root, api_base, pid)"""
    raw = _strip_url_noise(raw)
    m = re.match(rf"(https?://[^/]+)(?:/d/({_ID}))?/p(?:roblem)?/({_ID})", raw)
    if m:
        root, domain, pid = m.group(1), m.group(2), m.group(3)
        return root, f"{root}/d/{domain}" if domain else f"{root}/d/system", pid
    if raw.strip().isdigit():
        return None, None, raw.strip()
    raise ValueError(f"无法从 '{raw}' 中解析出题目 ID")


def parse_root(url: str) -> str:
    """从 URL 提取根地址"""
    parsed = urlparse(_strip_url_noise(url))
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    raise ValueError(f"无法从 '{url}' 提取根地址")
