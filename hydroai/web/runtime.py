"""Per-application process state and non-consuming workspace log stream."""
from __future__ import annotations

import codecs
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from hydroai.config.paths import data_dir
from hydroai.monitor import daemon_control

TASK_TIMEOUT = 1800
LOG_LIMIT = 2000


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._daemon_lock = threading.Lock()
        self._logs_changed = threading.Condition()
        self._logs = deque(maxlen=LOG_LIMIT)
        self._log_id = 0
        self._task_running = False
        self._cancel_requested = False
        self._active_proc = None
        self._daemon_proc = None
        self._history = deque(maxlen=20)

    def _broadcast(self, message: str):
        now = datetime.now()
        line = f"{now:%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} {message}"
        print(line, flush=True)
        with self._logs_changed:
            self._log_id += 1
            self._logs.append((self._log_id, line))
            self._logs_changed.notify_all()

    def emit(self, message: str):
        self._broadcast(f"[{datetime.now():%H:%M:%S}] {message}")

    def log_stream(self, last_event_id=None):
        try:
            cursor = int(last_event_id) if last_event_id is not None else 0
            if cursor < 0:
                cursor = 0
        except (TypeError, ValueError):
            cursor = 0
        with self._logs_changed:
            # An ID from a previous process must not suppress the new log forever.
            if cursor > self._log_id:
                cursor = 0
        yield ':connected\n\n'
        while True:
            with self._logs_changed:
                batch = [(i, line) for i, line in self._logs if i > cursor]
                if not batch:
                    self._logs_changed.wait(timeout=30)
                    batch = [(i, line) for i, line in self._logs if i > cursor]
            if batch:
                cursor = batch[-1][0]
                yield f"id: {cursor}\ndata: {json.dumps([line for _, line in batch], ensure_ascii=False)}\n\n"
            else:
                yield ':keepalive\n\n'

    def clear_logs(self):
        with self._logs_changed:
            self._logs.clear()

    @property
    def task_running(self):
        with self._lock:
            return self._task_running

    def history(self):
        with self._lock:
            return [item.copy() for item in self._history]

    def start_task(self, args: list[str]) -> bool:
        if not args:
            return False
        with self._lock:
            if self._task_running:
                return False
            self._task_running = True
            self._cancel_requested = False
        try:
            threading.Thread(target=self._run_task, args=(list(args),), daemon=True).start()
        except Exception as exc:
            with self._lock:
                self._task_running = False
            self.emit(f"启动任务失败: {type(exc).__name__}")
            return False
        return True

    def stop_task(self) -> bool:
        with self._lock:
            if not self._task_running:
                return False
            proc = self._active_proc
            if proc is not None and proc.poll() is not None:
                return False
            self._cancel_requested = True
        if proc is not None:
            self._terminate(proc)
        self.emit("任务已收到终止信号")
        return True

    @staticmethod
    def _terminate(proc, timeout=5.0):
        if proc.poll() is not None:
            return True
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               capture_output=True, timeout=timeout + 5)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        return proc.poll() is not None

    def _popen(self, cmd):
        return subprocess.Popen(cmd, cwd=str(data_dir(self.root)), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                errors='replace', start_new_session=True)

    def _read_output(self, proc):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        buf = ''
        while True:
            chunk = proc.stdout.buffer.read1(256)
            if not chunk:
                buf += decoder.decode(b'', final=True)
                break
            buf += decoder.decode(chunk)
            while '\n' in buf:
                line, buf = buf.split('\n', 1)
                if line.strip():
                    self._broadcast(line.strip())
            if len(buf) >= 200:
                self._broadcast(buf.strip())
                buf = ''
        if buf.strip():
            self._broadcast(buf.strip())

    def _run_task(self, args):
        proc = None
        finished = threading.Event()
        cmd = [sys.executable, '-u', str(self.root / args[0]), *args[1:]]
        try:
            self.emit(f"执行: {' '.join(shlex.quote(str(a)) for a in cmd)}")
            proc = self._popen(cmd)
            with self._lock:
                self._active_proc = proc
                cancel = self._cancel_requested
            if cancel:
                self._terminate(proc)
            def watchdog():
                if not finished.wait(TASK_TIMEOUT):
                    self.emit('任务运行超时，正在终止')
                    self._terminate(proc)
            threading.Thread(target=watchdog, daemon=True).start()
            self._read_output(proc)
            proc.wait()
            self.emit(f"进程结束 (code {proc.returncode})")
        except Exception as exc:
            self.emit(f"执行异常: {exc}")
            if proc is not None:
                self._terminate(proc)
        finally:
            finished.set()
            if proc is not None:
                if proc.stdout:
                    proc.stdout.close()
                with self._lock:
                    self._history.append({'cmd': ' '.join(shlex.quote(str(a)) for a in args),
                                          'time': datetime.now().strftime('%H:%M:%S'),
                                          'code': proc.returncode})
            with self._lock:
                self._active_proc = None
                self._task_running = False

    def daemon_status(self):
        status = daemon_control.status()
        with self._lock:
            proc = self._daemon_proc
        if not status['running'] and proc and proc.poll() is None:
            return {'running': True, 'pid': proc.pid}
        return status

    def _daemon_cmd(self):
        return [sys.executable, '-u', str(self.root / 'contest_daemon.py'),
                '--no-interactive', *shlex.split(os.environ.get('WEBUI_DAEMON_ARGS', ''))]

    def _watch_daemon(self, proc):
        try:
            self._read_output(proc)
            proc.wait()
            self.emit(f"守护进程结束 (code {proc.returncode})")
        except Exception as exc:
            self.emit(f"守护进程异常: {exc}")
            self._terminate(proc)
        finally:
            if proc.stdout:
                proc.stdout.close()
            daemon_control.remove_pid(proc.pid)
            with self._lock:
                if self._daemon_proc is proc:
                    self._daemon_proc = None

    def start_daemon(self) -> tuple[bool, str]:
        with self._daemon_lock:
            if self.daemon_status()['running']:
                return True, ''
            try:
                proc = self._popen(self._daemon_cmd())
            except (OSError, ValueError) as exc:
                return False, f"启动失败: {type(exc).__name__}"
            with self._lock:
                self._daemon_proc = proc
            try:
                threading.Thread(target=self._watch_daemon, args=(proc,), daemon=True).start()
            except Exception as exc:
                self._terminate(proc)
                if proc.stdout:
                    proc.stdout.close()
                with self._lock:
                    self._daemon_proc = None
                return False, f"启动失败: {type(exc).__name__}"
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if daemon_control.status()['running'] or proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is not None:
                return False, f"守护进程启动失败 (exit {proc.returncode})"
            return True, ''

    def stop_daemon(self) -> tuple[bool, str]:
        with self._daemon_lock:
            with self._lock:
                proc = self._daemon_proc
            daemon_control.stop()
            if proc and proc.poll() is None:
                self._terminate(proc)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if not self.daemon_status()['running']:
                    return True, ''
                time.sleep(0.1)
            return False, '停止守护进程超时，请查看实时控制台'
