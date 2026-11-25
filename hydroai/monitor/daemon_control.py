"""跨平台守护进程 PID 管理。

WebUI 与守护进程共用 data 目录下的 ``daemon.pid``，因此 Web 重启后仍能
识别并停止此前启动的守护进程；外部启动的守护进程也会被识别。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from hydroai.config.paths import data_file, project_root

PID_FILE = "daemon.pid"
_last_heartbeat = 0.0


def pid_path() -> Path:
    return data_file(PID_FILE, project_root())


def _pid_namespace() -> str:
    """容器/进程命名空间标识；Docker 重启后会变化，用于识别过期 PID。"""
    try:
        return str(os.stat("/proc/self/ns/pid").st_ino)
    except (OSError, AttributeError):
        return ""


def _read_entry() -> dict:
    try:
        text = pid_path().read_text(encoding="utf-8").strip()
    except OSError:
        return {"pid": 0, "ns": "", "heartbeat": 0.0}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            pid = int(data.get("pid", 0) or 0)
            try:
                heartbeat = float(data.get("heartbeat", 0) or 0)
            except (TypeError, ValueError):
                heartbeat = 0.0
            return {"pid": pid, "ns": str(data.get("ns") or ""),
                    "heartbeat": heartbeat}
        except (ValueError, TypeError):
            return {"pid": 0, "ns": "", "heartbeat": 0.0}
    return {"pid": int(text) if text.isdigit() else 0, "ns": "", "heartbeat": 0.0}


def read_pid() -> int:
    return _read_entry()["pid"]


def _write_entry(entry: dict) -> None:
    path = pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps({
        "pid": int(entry["pid"]),
        "ns": entry.get("ns", ""),
        "heartbeat": float(entry.get("heartbeat", time.time())),
    }), encoding="utf-8")
    os.replace(temp, path)


def write_pid(pid: int) -> None:
    _write_entry({"pid": pid, "ns": _pid_namespace(), "heartbeat": time.time()})


def heartbeat(pid: int = 0) -> None:
    """更新心跳（默认当前进程），最多每 20 秒写一次。"""
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat < 20:
        return
    entry = _read_entry()
    target = int(pid or os.getpid())
    if entry["pid"] != target:
        return
    _last_heartbeat = now
    if now - entry["heartbeat"] < 20:
        return
    entry["heartbeat"] = now
    _write_entry(entry)


def remove_pid(pid: int = 0) -> None:
    """删除 PID 文件；指定 pid 时只删除属于自己的记录。"""
    try:
        current = read_pid()
        if pid and current and current != int(pid):
            return
        pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _entry_active(entry: dict) -> bool:
    pid = entry["pid"]
    if not pid or not pid_alive(pid):
        return False
    current_ns = _pid_namespace()
    if entry["ns"] and current_ns:
        # Docker 容器重启后命名空间变化，旧 PID 即使被复用也不会误认。
        return entry["ns"] == current_ns
    if entry["heartbeat"]:
        return time.time() - entry["heartbeat"] <= 180
    return True  # 兼容旧版本只写 int 的 PID 文件


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        # 拒绝访问通常表示进程仍存在，只是不属于当前用户。
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _proc_state(pid: int) -> str:
    """Linux /proc 状态；非 Linux 或不可读时返回空串。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return raw.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    # 僵尸进程仍能被 kill(pid, 0) 命中，但已经不能执行任务。
    if _proc_state(pid) in ("Z", "X"):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _can_signal(entry: dict) -> bool:
    pid = entry["pid"]
    if not pid or not pid_alive(pid):
        return False
    current_ns = _pid_namespace()
    if entry["ns"] and current_ns and entry["ns"] != current_ns:
        return False
    return True


def is_running() -> bool:
    entry = _read_entry()
    if _entry_active(entry):
        return True
    if entry["pid"]:
        remove_pid(entry["pid"])  # 清理过期/跨命名空间 PID 文件
    return False


def stop(timeout: float = 8.0) -> bool:
    """停止守护进程及其子进程。返回是否确实停止了一个运行中的进程。"""
    entry = _read_entry()
    pid = entry["pid"]
    if not pid:
        return False
    if not _can_signal(entry):
        remove_pid(pid)
        return False

    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=timeout + 5)
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        try:
            pgid = os.getpgid(pid)
            # Web 启动时使用 start_new_session=True，pgid==pid 才是安全的进程组。
            if pgid == pid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            remove_pid(pid)
            return False
        except PermissionError:
            return False
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline and pid_alive(pid):
            time.sleep(0.2)
        if pid_alive(pid):
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline and pid_alive(pid):
        time.sleep(0.1)
    alive = pid_alive(pid)
    if not alive:
        remove_pid(pid)
    return not alive


def status() -> dict:
    entry = _read_entry()
    running = _entry_active(entry)
    if not running and entry["pid"]:
        remove_pid(entry["pid"])
    return {"running": running, "pid": entry["pid"] if running else 0}
