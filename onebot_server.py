#!/usr/bin/env python3
"""
OneBot 通信服务端 — HTTP 双向通信。
接收 OneBot 消息事件 → 解析指令 → 调用 OJ Solver → 回复结果。
"""

import os, re, sys, json, time, logging, argparse, subprocess, threading
from pathlib import Path
from flask import Flask, request, jsonify
from oj_common import load_dotenv

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")

def _check_auth():
    if not AUTH_TOKEN: return True  # 未设置 token 则不校验
    return request.headers.get("X-Auth-Token", "") == AUTH_TOKEN

SCRIPT_DIR = Path(__file__).parent
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

WHITELIST: set[str] = set()
PENDING_TASKS: dict[str, dict] = {}
TASK_COUNTER = 0


def send_reply(msg: dict, text: str):
    """通过 OneBot HTTP API 发送回复消息"""
    api_url = app.config.get("ONEBOT_API_URL", "")
    if not api_url:
        log.warning("[!] 未配置 ONEBOT_API_URL，无法发送")
        return
    try:
        import requests as rq
        rq.post(f"{api_url}/send_msg", json={
            "user_id": msg.get("user_id"),
            "group_id": msg.get("group_id", ""),
            "message": text,
        }, timeout=10)
    except Exception as e:
        log.warning("[!] 发送失败: %s", e)


@app.route("/", methods=["POST"])
def onebot_event():
    data = request.json or {}
    post_type = data.get("post_type", "")
    if post_type == "message":
        handle_message(data)
    return jsonify({"status": "ok"})


def handle_message(data: dict):
    user_id = str(data.get("user_id", ""))
    raw = data.get("raw_message", data.get("message", ""))
    if isinstance(raw, list):
        raw = "".join(s.get("data", {}).get("text", "") for s in raw if s.get("type") == "text")

    log.info("[msg] %s: %s", user_id, raw[:100])
    if user_id not in WHITELIST:
        send_reply(data, "无权限")
        return

    parts = raw.strip().split()
    if not parts: return
    cmd = parts[0].lower()
    args = " ".join(parts[1:]) if len(parts) > 1 else ""

    if cmd in ("solve", "求解", "s"):
        cmd_solve(data, args)
    elif cmd in ("contest", "比赛", "c"):
        cmd_contest(data, args)
    elif cmd in ("help", "帮助", "h"):
        send_reply(data, "solve <PID/链接> | contest <链接> | status | help")
    elif cmd in ("status", "状态"):
        send_reply(data, f"待处理: {len(PENDING_TASKS)} | 白名单: {list(WHITELIST)}")
    else:
        send_reply(data, f"未知指令: {cmd}")


def cmd_solve(data: dict, args: str):
    if not args: send_reply(data, "格式: solve <题目ID或链接>"); return
    url = args
    if args.isdigit(): url = f"https://oj.yuanyicode.com/p/{args}"
    elif not args.startswith("http"): send_reply(data, f"无法识别: {args}"); return

    global TASK_COUNTER
    TASK_COUNTER += 1
    tid = f"task_{TASK_COUNTER}"
    PENDING_TASKS[tid] = {"url": url, "user": data.get("user_id", "?")}
    send_reply(data, f"任务 #{TASK_COUNTER} 已提交: {url}")

    def run():
        sp = str(SCRIPT_DIR / "oj_solver.py")
        try:
            r = subprocess.run([sys.executable, sp, url, "--no-show-thinking"],
                             capture_output=True, text=True, timeout=300, cwd=str(SCRIPT_DIR))
            out = r.stdout[-1000:] if len(r.stdout) > 1000 else r.stdout
            summary = "\n".join(line for line in out.split("\n")
                if any(k in line for k in ["AC", "得分", "完成", "题解", "评测", "结果", "异常", "失败"]))[-500:]
            send_reply(data, f"#{TASK_COUNTER} 完成:\n{summary or out[-500:]}")
        except subprocess.TimeoutExpired:
            send_reply(data, f"#{TASK_COUNTER} 超时(5min)")
        except Exception as e:
            send_reply(data, f"#{TASK_COUNTER} 异常: {e}")
        finally:
            PENDING_TASKS.pop(tid, None)

    threading.Thread(target=run, daemon=True).start()


def cmd_contest(data: dict, args: str):
    if not args.startswith("http"): send_reply(data, "格式: contest <链接>"); return
    global TASK_COUNTER; TASK_COUNTER += 1
    send_reply(data, f"比赛任务 #{TASK_COUNTER} 已启动")
    subprocess.Popen([sys.executable, str(SCRIPT_DIR / "contest_solver.py"), args], cwd=str(SCRIPT_DIR))


@app.route("/admin/whitelist", methods=["GET", "POST"])
def admin_whitelist():
    if not _check_auth():
        return jsonify({"error": "未授权"}), 403
    if request.method == "POST":
        d = request.json or {}
        uid = str(d.get("user_id", ""))
        if d.get("action") == "add" and uid: WHITELIST.add(uid)
        elif d.get("action") == "remove" and uid: WHITELIST.discard(uid)
    return jsonify({"whitelist": list(WHITELIST), "pending": len(PENDING_TASKS)})


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5701)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--whitelist", help="白名单 user_id，逗号分隔")
    parser.add_argument("--api-url", required=True, help="OneBot HTTP API 地址 (如 http://127.0.0.1:5700)")
    args = parser.parse_args()

    if args.whitelist:
        for uid in args.whitelist.split(","): WHITELIST.add(uid.strip())
    app.config["ONEBOT_API_URL"] = args.api_url

    log.info("OneBot 服务端: http://%s:%d/", args.host, args.port)
    log.info("白名单: %s", list(WHITELIST) or "空")
    log.info("OneBot 上报地址: http://<本机IP>:%d/", args.port)

    from werkzeug.serving import run_simple
    run_simple(args.host, args.port, app, threaded=True)


if __name__ == "__main__":
    main()
