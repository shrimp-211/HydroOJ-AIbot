#!/usr/bin/env python3
"""
OJ Solver Web GUI — 网页操作界面
用法: python webui.py                     # 默认 http://127.0.0.1:5000
      python webui.py --port 8080         # 指定端口
"""

import os
import sys
import json
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
from oj_common import load_config, load_dotenv, create_session, save_cookies, load_cookies, parse_root

load_dotenv()

if sys.platform == "win32" and sys.stdout.isatty():
    import io
    try: sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception: pass

app = Flask(__name__)
ROOT = pathlib.Path(__file__).parent.resolve()

# 线程安全的状态锁
_state_lock = threading.Lock()
log_queue: queue.Queue = queue.Queue(maxsize=2000)
active_proc: subprocess.Popen | None = None
task_running = False
history: list[dict] = []
AUTH_TOKEN = ""  # 如果设置，需要在请求头携带 X-Auth-Token


def save_config(cfg: dict):
    with open(ROOT / "config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


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
    cmd = [sys.executable, "-u", *args]
    emit(f"执行: {' '.join(shlex.quote(str(a)) for a in cmd)}")
    with _state_lock: task_running = True
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
TEMPLATE_PATH = ROOT / "templates" / "index.html"
PAGE = TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists() else "<h1>模板文件缺失</h1>"




# ============================================================
# 路由
# ============================================================
@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "POST":
        if not _check_auth(): return jsonify({"error": "未授权"}), 403
        new_cfg = request.json
        if new_cfg:
            old = load_config()
            for section in new_cfg:
                if section in old and isinstance(old[section], dict):
                    payload = new_cfg[section]
                    # ai_api_key 仅在前端传了新值时才更新
                    if section == "ai" and "ai_api_key" not in payload:
                        keys = {k: v for k, v in payload.items() if k != "ai_api_key"}
                        old[section].update(keys)
                    else:
                        old[section].update(payload)
                else:
                    old[section] = new_cfg[section]
            save_config(old)
            emit("配置已更新")
        return jsonify({"ok": True})

    # GET — 返回配置（API Key 仅返回是否已配置标记）
    cfg = load_config()
    ai_cfg = cfg.get("ai", {})
    if ai_cfg:
        has_key = bool(ai_cfg.get("ai_api_key", ""))
        ai_cfg = dict(ai_cfg)
        ai_cfg["api_key_configured"] = has_key
        ai_cfg.pop("ai_api_key", None)
        cfg = dict(cfg)
        cfg["ai"] = ai_cfg
    return jsonify(cfg)



@app.route("/status")
def status():
    with _state_lock:
        return jsonify({"task_running": task_running})


def _read_dashboard():
    try:
        with open("dashboard.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"problems": {}, "contests": {}, "history": []}


@app.route("/api/stats")
def api_stats():
    d = _read_dashboard()
    history = d.get("history", [])
    total = len(history)
    ac = sum(1 for h in history if h.get("status") == "ac")
    tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in history)
    cost = sum(h.get("cost", 0) for h in history)
    return jsonify({
        "total": total, "ac": ac, "fail": total - ac,
        "ac_rate": f"{ac}/{total}" if total else "0/0",
        "total_tokens": tokens, "total_cost": round(cost, 4),
    })


@app.route("/api/today")
def api_today():
    today = datetime.now().strftime("%m-%d")
    d = _read_dashboard()
    today_records = [h for h in d.get("history", [])
                     if h.get("finished_at", "").startswith(today)]
    ac = sum(1 for h in today_records if h.get("status") == "ac")
    tokens = sum(h.get("tokens_in", 0) + h.get("tokens_out", 0) for h in today_records)
    cost = sum(h.get("cost", 0) for h in today_records)
    return jsonify({
        "date": today, "total": len(today_records),
        "ac": ac, "tokens": tokens, "cost": round(cost, 4),
        "records": today_records[-20:],
    })


@app.route("/api/history")
def api_history():
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
        from oj_solver import AIClient
        AIClient.reload_class_prompts()
        return jsonify({"ok": True, "msg": "提示词已重载"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/kill", methods=["POST"])
def kill_task():
    if not _check_auth(): return jsonify({"error": "未授权"}), 403
    global active_proc, task_running
    with _state_lock:
        proc = active_proc
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            proc.kill()
        with _state_lock: active_proc = None; task_running = False
        emit("任务已被用户终止")
        return jsonify({"ok": True})
    return jsonify({"error": "没有正在运行的任务"}), 400


@app.route("/history")
def get_history():
    with _state_lock:
        return jsonify(history[-20:])


def _check_auth() -> bool:
    """检查 X-Auth-Token；未设置时仅允许本地访问"""
    if not AUTH_TOKEN:
        return request.remote_addr in ("127.0.0.1", "::1", "localhost")
    return request.headers.get("X-Auth-Token", "") == AUTH_TOKEN


@app.route("/stream")
def stream():
    def gen():
        try:
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
    parser = argparse.ArgumentParser(description="OJ Solver Web GUI")
    parser.add_argument("--port", type=int, default=5000, help="端口 (默认 5000)")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址")
    parser.add_argument("--auth-token", default="", help="API 鉴权 Token（可选）")
    parser.add_argument("--debug", action="store_true", help="调试模式")

    try:
        _args, _ = parser.parse_known_args()
    except:
        _args = argparse.Namespace(port=5000, host="127.0.0.1", auth_token="", debug=False)

    global AUTH_TOKEN
    AUTH_TOKEN = _args.auth_token

    print(f"[*] OJ Solver WebUI 启动: http://{_args.host}:{_args.port}")
    if AUTH_TOKEN: print("[*] 已启用鉴权 Token")
    run_simple(_args.host, _args.port, app, use_reloader=False,
               use_debugger=_args.debug, threaded=True)


if __name__ == "__main__":
    main()
