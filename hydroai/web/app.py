#!/usr/bin/env python3
"""
OJ Solver Web GUI — 网页操作界面
用法: python webui.py                     # 默认 http://127.0.0.1:5000
      python webui.py --port 8080         # 指定端口
"""

import os
import sys
import json
import time
import secrets
from hydroai.config.manager import ConfigManager, FIELD_TO_JSON
from hydroai.config.paths import data_dir, data_file, config_file, project_root
from hydroai.monitor import daemon_control
from hydroai.config.io import (read_config, atomic_write_json, normalize_config,
                       merge_config, redact_config, validate_document, strip_redacted)
import queue
import shlex
import signal
import pathlib
import argparse
import threading
import subprocess
from datetime import datetime

from flask import Flask, render_template_string, request, jsonify, Response
from werkzeug.serving import run_simple
from hydroai.oj.common import load_config, load_dotenv, create_session, save_cookies, load_cookies, parse_root
# 在处理并发请求前完成用量模块的初始化，避免首次打开用量页时抢占导入。
from hydroai.usage import QuotaManager, get_store

# 不在导入时加载 .env：Web 模块常被测试/WSGI 导入，模块级加载会把本地凭据
# 注入进程并干扰其他配置来源。实际启动时在 main() 中加载。

if sys.platform == "win32" and sys.stdout.isatty():
    import io
    try: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception: pass

app = Flask(__name__)
# 入口脚本与 data 目录的相对基准；测试会把它替换为临时目录。
ROOT = project_root()

# 线程安全的状态锁
_state_lock = threading.Lock()
log_queue: queue.Queue = queue.Queue(maxsize=2000)
active_proc: subprocess.Popen | None = None
daemon_proc: subprocess.Popen | None = None
task_running = False
history: list[dict] = []
AUTH_TOKEN = ""  # 如果设置，需要在请求头携带 X-Auth-Token
ALLOWED_SCRIPTS = {"oj_solver", "contest_solver", "benchmark_solver", "testdata_supplement"}
# EventSource 不能自定义请求头，因此用一次性的短时票据访问 /stream。
STREAM_TICKET_TTL = 60
_stream_tickets: dict[str, tuple[float, str]] = {}
_stream_lock = threading.Lock()


def save_config(cfg: dict):
    atomic_write_json(config_file("config.json", ROOT), cfg)


def broadcast(msg: str):
    """同时输出到控制台和 Web 前端"""
    now = datetime.now()
    line = f"{now:%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} {msg}"
    print(line, flush=True)
    try: log_queue.put_nowait(line)
    except queue.Full: pass

def emit(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    broadcast(f"[{ts}] {msg}")


def run_script(args: list[str]):
    """子进程运行脚本，实时转发输出到 log_queue（支持流式输出）"""
    global active_proc, task_running, history
    cmd = [sys.executable, "-u", str(ROOT / args[0]), *args[1:]]
    emit(f"执行: {' '.join(shlex.quote(str(a)) for a in cmd)}")
    with _state_lock: task_running = True
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(data_dir(ROOT)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True,
        )
        with _state_lock: active_proc = proc

        # 防 hang 看门狗：30 分钟后强制杀掉
        def watchdog():
            try: proc.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception: proc.kill()
        threading.Thread(target=watchdog, daemon=True).start()

        buf = ""
        while True:
            chunk = proc.stdout.read(256)
            if not chunk: break
            buf += chunk
            # 遇到换行就 flush 整行
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    broadcast(line.strip())
            # 残留过长（>200字符）也 flush，保证实时性
            if len(buf) >= 200:
                broadcast(buf.strip())
                buf = ""
        if buf.strip():
            broadcast(buf.strip())
        proc.wait()
        emit(f"进程结束 (code {proc.returncode})")
        with _state_lock:
            history.append({
                "cmd": " ".join(shlex.quote(str(a)) for a in args),
                "time": datetime.now().strftime("%H:%M:%S"),
                "code": proc.returncode,
            })
            if len(history) > 20: history.pop(0)
    except Exception as e:
        emit(f"执行异常: {e}")
    finally:
        with _state_lock:
            active_proc = None
            task_running = False


# ============================================================
# HTML 模板
# ============================================================
TEMPLATE_PATH = pathlib.Path(__file__).parent / "templates" / "index.html"
PAGE = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else "<h1>模板文件缺失</h1>"




# ============================================================
# 路由
# ============================================================
@app.route("/")
def index():
    return render_template_string(PAGE)


_config_lock = threading.Lock()


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    path = config_file("config.json", ROOT)
    try:
        with _config_lock:
            if request.method == "POST":
                patch = strip_redacted(request.get_json(silent=True))
                if not isinstance(patch, dict):
                    return jsonify({"error": "配置必须是 JSON 对象"}), 400
                merged = merge_config(read_config(path), normalize_config(patch))
                errors = validate_document(merged)
                if errors:
                    return jsonify({"error": "配置校验失败", "errors": errors}), 400
                save_config(merged)
                try:
                    from hydroai.ai.health import clear_health_cache
                    clear_health_cache()
                except Exception:
                    pass
                emit("配置已更新")
                return jsonify({"ok": True})
            manager = ConfigManager(str(path))
            cfg = read_config(path)
            cfg.update({key: getattr(manager.cfg, attr) for attr, key in FIELD_TO_JSON.items()})
            safe = redact_config(cfg)
            safe["ai_api_key_configured"] = bool(manager.api_key)
            return jsonify(safe)
    except (OSError, ValueError, TypeError):
        return jsonify({"error": "无法读取或保存配置，请检查 JSON 格式与文件权限"}), 400


@app.route("/config/doctor")
def config_doctor():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    from hydroai.config.cli import diagnose
    return jsonify(diagnose(config_file("config.json", ROOT)))


@app.route('/models/discover', methods=['POST'])
def models_discover():
    if not _check_auth():
        return jsonify({'error': '未授权'}), 403
    from hydroai.ai.services import discover_models
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise ValueError('需要 JSON 对象')
        manager = ConfigManager(str(config_file('config.json', ROOT)))
        name = body.get('model') or manager.ai_model
        if not isinstance(name, str) or (name != manager.ai_model and name not in manager.models):
            raise ValueError('请选择已经保存的模型连接')
        return jsonify(discover_models(manager.get_model_config(name)))
    except (ValueError, OSError) as exc:
        return jsonify({'error': str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '配置读取失败'}), 400


@app.route('/models/add', methods=['POST'])
def models_add():
    if not _check_auth():
        return jsonify({'error': '未授权'}), 403
    from hydroai.config.cli import add_model
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': '需要 JSON 对象'}), 400
    try:
        allowed = {'name', 'model_id', 'base_url', 'api_key_env', 'provider', 'pricing_provider', 'set_default'}
        if set(body) - allowed or not {'name', 'model_id', 'base_url'} <= set(body):
            raise ValueError('缺少必要字段或包含未知字段')
        if 'set_default' in body and type(body['set_default']) is not bool:
            raise ValueError('set_default 必须是 bool')
        with _config_lock:
            result = add_model(config_file('config.json', ROOT), **body)
        try:
            from hydroai.ai.health import clear_health_cache
            clear_health_cache()
        except Exception:
            pass
        return jsonify(result)
    except (ValueError, TypeError, OSError):
        return jsonify({'error': '新增失败：请检查名称是否重复、URL、供应商和环境变量名'}), 400


@app.route('/pricing', methods=['GET'])
@app.route('/pricing/refresh', methods=['POST'])
def pricing_status():
    if not _check_auth():
        return jsonify({'error': '未授权'}), 403
    from hydroai.config.cli import price_report
    try:
        manager = ConfigManager(str(config_file('config.json', ROOT)))
        return jsonify(price_report(manager, refresh=request.method == 'POST'))
    except (ValueError, TypeError, OSError):
        return jsonify({'error': '无法检查定价，请先修正配置'}), 400


@app.route("/status")
def status():
    with _state_lock:
        task = task_running
        proc = daemon_proc
    daemon = daemon_control.status()
    running = daemon["running"] or bool(proc and proc.poll() is None)
    return jsonify({
        "task_running": task,
        "daemon_running": running,
        "daemon_pid": daemon["pid"],
    })


# ── 后台任务 / 守护进程 ──
def _start_task(args: list[str]) -> bool:
    with _state_lock:
        if task_running:
            return False
        task_running = True   # 先占用，避免两个请求同时通过检查
    try:
        threading.Thread(target=run_script, args=(args,), daemon=True).start()
    except Exception as exc:
        with _state_lock:
            task_running = False
        emit(f"启动任务失败: {type(exc).__name__}")
        return False
    return True


@app.route("/run/<name>", methods=["POST"])
def run_named(name):
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    if name not in ALLOWED_SCRIPTS:
        return jsonify({"error": "不支持的任务"}), 400
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "需要 JSON 对象"}), 400
    args = body.get("args", [])
    if (not isinstance(args, list) or len(args) > 30
            or not all(isinstance(arg, str) and len(arg) <= 2000 for arg in args)):
        return jsonify({"error": "参数必须是字符串数组"}), 400

    if not _start_task([name + ".py", *args]):
        return jsonify({"error": "已有任务在运行"}), 409
    return jsonify({"ok": True})


def _watch_daemon(proc: subprocess.Popen):
    global daemon_proc
    try:
        buf = ""
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if line.strip():
                    broadcast(line.strip())
        if buf.strip():
            broadcast(buf.strip())
        proc.wait()
        emit(f"守护进程结束 (code {proc.returncode})")
    except Exception as exc:
        emit(f"守护进程异常: {exc}")
    finally:
        daemon_control.remove_pid(proc.pid)
        with _state_lock:
            if daemon_proc is proc:
                daemon_proc = None


def _terminate_proc(proc: subprocess.Popen, timeout: float = 5.0) -> bool:
    if proc is None or proc.poll() is not None:
        return True
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=timeout + 5)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
        return True
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return proc.poll() is not None


def _daemon_cmd() -> list[str]:
    extra = shlex.split(os.environ.get("WEBUI_DAEMON_ARGS", ""))
    return [sys.executable, "-u", str(ROOT / "contest_daemon.py"),
            "--no-interactive", *extra]


def _spawn_daemon() -> tuple[bool, str]:
    """启动守护进程并等待 PID；Web 与 Docker 自动启动共用。"""
    global daemon_proc
    if daemon_control.is_running():
        return True, ""
    with _state_lock:
        current = daemon_proc
    if current and current.poll() is None:
        return True, ""
    try:
        proc = subprocess.Popen(
            _daemon_cmd(), cwd=str(data_dir(ROOT)), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", start_new_session=True)
    except OSError as exc:
        return False, f"启动失败: {type(exc).__name__}"
    with _state_lock:
        daemon_proc = proc
    threading.Thread(target=_watch_daemon, args=(proc,), daemon=True).start()

    # 等待 PID 文件出现；能提前发现“启动后立即退出”并反馈给 Web。
    deadline = time.time() + 2.5
    while time.time() < deadline:
        if daemon_control.is_running() or proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is not None:
        return False, f"守护进程启动失败 (exit {proc.returncode})"
    return True, ""


@app.route("/daemon/<action>", methods=["POST"])
def daemon_action(action):
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    if action == "start":
        ok, error = _spawn_daemon()
        if not ok:
            emit(error)
            return jsonify({"error": error + "，请查看实时控制台"}), 500
        emit("守护进程已启动")
        return jsonify({"ok": True, "daemon_running": True,
                        "daemon_pid": daemon_control.read_pid()})

    if action == "stop":
        with _state_lock:
            proc = daemon_proc
        stopped = daemon_control.stop()
        if not stopped and proc and proc.poll() is None:
            # 兼容旧版本没有 PID 文件的守护进程
            stopped = _terminate_proc(proc)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not daemon_control.is_running() and (not proc or proc.poll() is not None):
                break
            time.sleep(0.1)
        running = daemon_control.is_running() or bool(proc and proc.poll() is None)
        if running:
            return jsonify({"error": "停止守护进程超时，请查看实时控制台",
                            "daemon_running": True}), 500
        emit("守护进程已停止")
        return jsonify({"ok": True, "daemon_running": False})

    if action == "once":
        if not _start_task(["contest_daemon.py", "--once", "--no-interactive"]):
            return jsonify({"error": "已有任务在运行"}), 409
        return jsonify({"ok": True})
    return jsonify({"error": "未知操作"}), 400


# ── 用量与额度 API ──
def _usage_store():
    return get_store(data_file("usage.db", ROOT))


def _today_start() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@app.route("/api/usage/summary")
def api_usage_summary():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    days = max(1, min(request.args.get("days", 30, type=int), 3660))
    store = _usage_store()
    now = time.time()
    return jsonify({
        "days": days,
        "summary": store.summary(since=now - days * 86400),
        "today": store.summary(since=_today_start()),
    })


@app.route("/api/usage/series")
def api_usage_series():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    days = max(1, min(request.args.get("days", 30, type=int), 366))
    return jsonify({"days": days, "series": _usage_store().daily(days=days)})


@app.route("/api/usage/breakdown")
def api_usage_breakdown():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    by = request.args.get("by", "model")
    if by not in ("model", "provider", "task", "problem_id", "day", "hour"):
        return jsonify({"error": "不支持的统计维度"}), 400
    days = max(1, min(request.args.get("days", 30, type=int), 3660))
    rows = _usage_store().breakdown(by, since=time.time() - days * 86400,
                                    limit=request.args.get("limit", 20, type=int))
    return jsonify({"by": by, "days": days, "rows": rows})


@app.route("/api/usage/recent")
def api_usage_recent():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    days = max(1, min(request.args.get("days", 7, type=int), 3660))
    limit = max(1, min(request.args.get("limit", 50, type=int), 500))
    rows = _usage_store().recent(limit, since=time.time() - days * 86400)
    return jsonify({"days": days, "requests": rows})


@app.route("/api/usage/heatmap")
def api_usage_heatmap():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    days = max(1, min(request.args.get("days", 365, type=int), 3660))
    return jsonify(_usage_store().heatmap(days=days))


_quota_manager = None
_quota_lock = threading.Lock()


def _quota_payload(refresh=False):
    """复用同一个额度管理器，远端余额才不会在每个请求里重复查询。"""
    global _quota_manager
    path = str(config_file("config.json", ROOT))
    try:
        with _quota_lock:
            current = getattr(getattr(_quota_manager, "config", None), "_path", None)
            if _quota_manager is None or str(current or "") != path:
                _quota_manager = QuotaManager(ConfigManager(path), store=_usage_store())
            manager = _quota_manager
        return manager.summary(refresh=refresh)
    except (OSError, ValueError, TypeError):
        return {"items": [], "total": 0, "low": 0, "errors": 0,
                "error": "无法读取额度配置"}


@app.route("/api/quota")
def api_quota():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    return jsonify(_quota_payload(refresh=False))


@app.route("/api/quota/refresh", methods=["POST"])
def api_quota_refresh():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    return jsonify(_quota_payload(refresh=True))


def _read_dashboard():
    try:
        with open(data_file("dashboard.json", ROOT), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"problems": {}, "contests": {}, "history": []}


@app.route("/api/stats")
def api_stats():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    d = _read_dashboard()
    # history 里混有比赛记录（无 pid/status），不能计入题目数与 AC 率。
    probs = [h for h in d.get("history", [])
             if isinstance(h, dict) and "pid" in h]
    total = len(probs)
    ac = sum(1 for h in probs if h.get("status") == "ac")
    tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in probs)
    cost = sum(h.get("cost", 0) for h in probs)
    store = _usage_store()
    return jsonify({
        "total": total, "ac": ac, "fail": total - ac,
        "ac_rate": f"{ac}/{total}" if total else "0/0",
        "total_tokens": tokens, "total_cost": round(cost, 4),
        "usage": store.summary(),
        "by_model": store.breakdown("model", limit=10),
    })


@app.route("/api/today")
def api_today():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    today = datetime.now().strftime("%m-%d")
    d = _read_dashboard()
    today_records = [h for h in d.get("history", [])
                     if isinstance(h, dict) and "pid" in h
                     and h.get("finished_at", "").startswith(today)]
    ac = sum(1 for h in today_records if h.get("status") == "ac")
    tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in today_records)
    cost = sum(h.get("cost", 0) for h in today_records)
    return jsonify({
        "date": today, "total": len(today_records),
        "ac": ac, "tokens": tokens, "cost": round(cost, 4),
        "records": today_records[-20:],
        "usage": _usage_store().summary(since=_today_start()),
    })


@app.route("/api/history")
def api_history():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    d = _read_dashboard()
    n = request.args.get("n", 20, type=int)
    history = d.get("history", [])
    return jsonify({"history": history[-n:]})


@app.route("/api/reload-prompts", methods=["POST"])
def api_reload_prompts():
    """热重载提示词（功能 C）"""
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    try:
        from hydroai.ai.client import AIClient
        AIClient.reload_class_prompts()
        return jsonify({"ok": True, "msg": "提示词已重载"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/kill", methods=["POST"])
def kill_task():
    if not _check_auth(): return jsonify({"error": "未授权"}), 403
    with _state_lock:
        proc = active_proc
    if proc and proc.poll() is None:
        try:
            if hasattr(os, 'killpg'):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.kill()
        except OSError:
            proc.kill()
        # 由工作线程回收子进程并释放任务槽，防止新任务被旧线程的清理覆盖。
        emit("任务已收到终止信号")
        return jsonify({"ok": True})
    return jsonify({"error": "没有正在运行的任务"}), 400


@app.route("/history")
def get_history():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    with _state_lock:
        return jsonify(history[-20:])


def _check_auth() -> bool:
    """检查 X-Auth-Token；未设置时仅允许本地访问"""
    if not AUTH_TOKEN:
        return request.remote_addr in ("127.0.0.1", "::1", "localhost")
    return request.headers.get("X-Auth-Token", "") == AUTH_TOKEN


@app.route("/stream/ticket", methods=["POST"])
def stream_ticket():
    """为 EventSource 签发短时票据，避免把长期 Token 放进 URL。"""
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    now = time.time()
    ticket = secrets.token_urlsafe(24)
    remote = request.remote_addr or ""
    with _stream_lock:
        for key in [k for k, (expires, _) in _stream_tickets.items() if expires <= now]:
            _stream_tickets.pop(key, None)
        _stream_tickets[ticket] = (now + STREAM_TICKET_TTL, remote)
    return jsonify({"ticket": ticket, "expires_in": STREAM_TICKET_TTL})


def _stream_auth() -> bool:
    ticket = request.args.get("ticket", "")
    if ticket:
        now = time.time()
        with _stream_lock:
            entry = _stream_tickets.get(ticket)
            if entry and entry[0] > now and entry[1] == (request.remote_addr or ""):
                return True
    return _check_auth()


@app.route("/stream")
def stream():
    if not _stream_auth():
        return jsonify({"error": "未授权"}), 403

    def gen():
        try:
            # 立即发送握手，空闲时也能让浏览器确认连接已建立。
            yield ":connected\n\n"
            while True:
                batch = []
                try:
                    batch.append(log_queue.get(timeout=30))
                    while True:
                        try: batch.append(log_queue.get_nowait())
                        except queue.Empty: break
                except queue.Empty:
                    pass
                if batch:
                    yield f"data: {json.dumps(batch, ensure_ascii=False)}\n\n"
                else:
                    yield ":keepalive\n\n"
        except GeneratorExit:
            pass  # 客户端断开，正常退出
    return Response(gen(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.route("/clear", methods=["POST"])
def clear():
    if not _check_auth(): return jsonify({"error": "未授权"}), 403
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break
    return jsonify({"ok": True})


# ============================================================
# 入口
# ============================================================
def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="OJ Solver Web GUI")
    parser.add_argument("--port", type=int, default=5000, help="端口 (默认 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址")
    parser.add_argument("--auth-token", default=os.environ.get("WEBUI_AUTH_TOKEN", ""), help="API 鉴权 Token（或 WEBUI_AUTH_TOKEN）")
    parser.add_argument("--debug", action="store_true", help="调试模式")

    try:
        _args, _ = parser.parse_known_args()
    except:
        _args = argparse.Namespace(port=5000, host="127.0.0.1", auth_token="", debug=False)

    global AUTH_TOKEN
    AUTH_TOKEN = _args.auth_token

    print(f"[*] OJ Solver WebUI 启动: http://{_args.host}:{_args.port}")
    if AUTH_TOKEN: print("[*] 已启用鉴权 Token")
    if os.environ.get("WEBUI_AUTOSTART_DAEMON", "").strip().lower() in (
            "1", "true", "yes", "on"):
        ok, error = _spawn_daemon()
        print("[*] 守护进程已自动启动" if ok else f"[-] 守护进程自动启动失败: {error}")
    run_simple(_args.host, _args.port, app, use_reloader=False,
               use_debugger=_args.debug, threaded=True)


if __name__ == "__main__":
    main()
