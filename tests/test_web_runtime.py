import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hydroai.web.runtime import Workspace


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail('condition timed out')


def events(frame):
    return json.loads(next(line[6:] for line in frame.splitlines() if line.startswith('data: ')))


def test_multireader_replay_clear_and_isolation(tmp_path):
    a, b = Workspace(tmp_path), Workspace(tmp_path)
    a.emit('before')
    one, two = a.log_stream(), a.log_stream()
    assert next(one) == ':connected\n\n'
    assert next(two) == ':connected\n\n'
    frame = next(one)
    assert any('before' in line for line in events(frame))
    assert events(next(two)) == events(frame)
    event_id = next(line[4:].strip() for line in frame.splitlines() if line.startswith('id:'))
    a.emit('after')
    resumed = a.log_stream(event_id)
    assert next(resumed) == ':connected\n\n'
    assert all('before' not in line for line in events(next(resumed)))
    a.clear_logs()
    assert not b.history()
    assert not b.task_running
    a.emit('fresh')
    fresh = a.log_stream('999999')
    next(fresh)
    assert all('before' not in line and 'after' not in line for line in events(next(fresh)))
    one.close(); two.close(); resumed.close(); fresh.close()


def test_task_process_history_and_cancel(tmp_path):
    (tmp_path / 'short.py').write_text("print('hello runtime', flush=True)\n", encoding='utf-8')
    (tmp_path / 'long.py').write_text("import time\nprint('ready', flush=True)\ntime.sleep(60)\n", encoding='utf-8')
    workspace = Workspace(tmp_path)
    assert workspace.start_task(['short.py', 'argument'])
    wait_for(lambda: not workspace.task_running)
    assert workspace.history()[-1]['code'] == 0
    assert 'short.py argument' in workspace.history()[-1]['cmd']
    stream = workspace.log_stream()
    next(stream)
    assert any('hello runtime' in line for line in events(next(stream)))
    stream.close()
    assert workspace.start_task(['long.py'])
    assert not workspace.start_task(['short.py'])
    wait_for(lambda: workspace._active_proc is not None)
    live = workspace.log_stream()
    next(live)
    lines = events(next(live))
    while not any('ready' in line for line in lines):
        lines = events(next(live))
    live.close()
    assert workspace.stop_task()
    wait_for(lambda: not workspace.task_running)
    assert len(workspace.history()) == 2
    assert workspace.history()[-1]['code'] != 0
    assert not workspace.stop_task()


def test_task_start_race_and_cancel_during_spawn(tmp_path, monkeypatch):
    import hydroai.web.runtime as runtime
    entered, release = threading.Event(), threading.Event()
    real_popen = runtime.subprocess.Popen
    (tmp_path / 'long.py').write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(runtime.subprocess, 'Popen', delayed)
    workspace = Workspace(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: workspace.start_task(['long.py']), range(8)))
    assert results.count(True) == 1
    assert entered.wait(5)
    assert workspace.stop_task()
    release.set()
    wait_for(lambda: not workspace.task_running)
    assert len(workspace.history()) == 1


def test_daemon_delegates_and_uses_configured_args(tmp_path, monkeypatch):
    import hydroai.web.runtime as runtime
    calls = []
    monkeypatch.setenv('WEBUI_DAEMON_ARGS', '--foo "two words"')
    monkeypatch.setattr(runtime.daemon_control, 'status', lambda: {'running': True, 'pid': 42})
    monkeypatch.setattr(runtime.daemon_control, 'stop', lambda: calls.append('stop') or True)
    workspace = Workspace(tmp_path)
    assert workspace.daemon_status() == {'running': True, 'pid': 42}
    assert workspace.start_daemon() == (True, '')
    monkeypatch.setattr(runtime.daemon_control, 'status', lambda: {'running': False, 'pid': 0})
    assert workspace.stop_daemon() == (True, '')
    assert calls == ['stop']
    assert workspace._daemon_cmd() == [sys.executable, '-u', str(tmp_path / 'contest_daemon.py'), '--no-interactive', '--foo', 'two words']


def test_bounded_log_and_replay_after_clear(tmp_path, monkeypatch):
    import hydroai.web.runtime as runtime
    monkeypatch.setattr(runtime, 'LOG_LIMIT', 2)
    workspace = Workspace(tmp_path)
    workspace.emit('first')
    workspace.emit('second')
    workspace.emit('third')
    stream = workspace.log_stream('0')
    next(stream)
    batch = events(next(stream))
    assert len(batch) == 2
    assert 'second' in batch[0] and 'third' in batch[1]
    workspace.clear_logs()
    workspace.emit('fourth')
    assert 'fourth' in events(next(stream))[0]
    stream.close()


def test_fake_process_command_and_reaping(tmp_path, monkeypatch):
    import io
    import hydroai.web.runtime as runtime
    calls = []
    class FakeProcess:
        pid = 123
        returncode = None
        stdout = io.TextIOWrapper(io.BytesIO(b'fake output\n'), encoding='utf-8')
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
        def poll(self):
            return self.returncode
    proc = FakeProcess()
    def spawn(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return proc
    monkeypatch.setattr(runtime.subprocess, 'Popen', spawn)
    workspace = Workspace(tmp_path)
    assert workspace.start_task(['oj_solver.py', '--dry-run'])
    wait_for(lambda: not workspace.task_running)
    assert calls[0][0] == [sys.executable, '-u', str(tmp_path / 'oj_solver.py'), '--dry-run']
    assert calls[0][1]['cwd'] == str(tmp_path)
    assert proc.stdout.closed
    assert workspace.history()[-1]['code'] == 0


def test_task_spawn_failure_releases_slot(tmp_path, monkeypatch):
    workspace = Workspace(tmp_path)
    def fail(_cmd):
        raise OSError('simulated failure')
    monkeypatch.setattr(workspace, '_popen', fail)
    assert workspace.start_task(['missing.py'])
    wait_for(lambda: not workspace.task_running)
    assert workspace.history() == []
    assert not workspace.stop_task()
    stream = workspace.log_stream()
    next(stream)
    assert any('执行异常' in line for line in events(next(stream)))
    stream.close()


def test_daemon_concurrent_start_and_owned_stop(tmp_path, monkeypatch):
    import hydroai.web.runtime as runtime
    monkeypatch.setattr(runtime.daemon_control, 'status', lambda: {'running': False, 'pid': 0})
    monkeypatch.setattr(runtime.daemon_control, 'stop', lambda: False)
    monkeypatch.setattr(runtime.daemon_control, 'remove_pid', lambda pid: None)
    (tmp_path / 'contest_daemon.py').write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
    workspace = Workspace(tmp_path)
    original = workspace._popen
    calls = []
    def spawn(cmd):
        calls.append(cmd)
        return original(cmd)
    monkeypatch.setattr(workspace, '_popen', spawn)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: workspace.start_daemon(), range(4)))
    assert results == [(True, '')] * 4
    assert len(calls) == 1
    assert workspace.daemon_status()['running']
    assert workspace.stop_daemon() == (True, '')
    wait_for(lambda: not workspace.daemon_status()['running'])


def test_task_timeout_reaps_process(tmp_path, monkeypatch):
    import hydroai.web.runtime as runtime
    monkeypatch.setattr(runtime, 'TASK_TIMEOUT', 0.15)
    (tmp_path / 'long.py').write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
    workspace = Workspace(tmp_path)
    assert workspace.start_task(['long.py'])
    wait_for(lambda: not workspace.task_running)
    assert workspace.history()[-1]['code'] != 0
