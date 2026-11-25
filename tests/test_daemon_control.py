"""守护进程 PID 控制与 Web 状态。"""
import os
import subprocess
import sys

import hydroai.web.app as webui
from hydroai.monitor import daemon_control


def test_pid_file_roundtrip(tmp_path):
    daemon_control.write_pid(4242)
    assert daemon_control.read_pid() == 4242
    assert daemon_control.pid_path() == tmp_path / "daemon.pid"
    daemon_control.remove_pid(4242)
    assert daemon_control.read_pid() == 0


def test_pid_alive_and_stale_cleanup():
    assert daemon_control.pid_alive(os.getpid()) is True
    assert daemon_control.pid_alive(999999) is False
    daemon_control.write_pid(999999)
    assert daemon_control.is_running() is False
    assert daemon_control.read_pid() == 0


def test_web_status_reads_pid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "ROOT", tmp_path)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    daemon_control.write_pid(os.getpid())
    try:
        client = webui.app.test_client()
        response = client.get("/status")
        assert response.status_code == 200
        assert response.get_json()["daemon_running"] is True
        assert response.get_json()["daemon_pid"] == os.getpid()
    finally:
        daemon_control.remove_pid(os.getpid())


def test_web_daemon_start_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "ROOT", tmp_path)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    daemon_control.write_pid(os.getpid())
    try:
        response = webui.app.test_client().post(
            "/daemon/start", headers={"X-Auth-Token": "access"})
        assert response.status_code == 200
        assert response.get_json()["daemon_running"] is True
    finally:
        daemon_control.remove_pid(os.getpid())


def test_web_daemon_stop_without_process(tmp_path, monkeypatch):
    monkeypatch.setattr(webui, "ROOT", tmp_path)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    daemon_control.remove_pid()
    response = webui.app.test_client().post(
        "/daemon/stop", headers={"X-Auth-Token": "access"})
    assert response.status_code == 200
    assert response.get_json()["daemon_running"] is False


def test_web_start_and_stop_real_process(tmp_path, monkeypatch):
    fake = tmp_path / "contest_daemon.py"
    fake.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "Path('daemon.pid').write_text(str(os.getpid()))\n"
        "try:\n"
        "    time.sleep(60)\n"
        "finally:\n"
        "    Path('daemon.pid').unlink(missing_ok=True)\n",
        encoding="utf-8")
    monkeypatch.setattr(webui, "ROOT", tmp_path)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    daemon_control.remove_pid()
    client = webui.app.test_client()
    try:
        start = client.post("/daemon/start", headers={"X-Auth-Token": "access"})
        assert start.status_code == 200
        assert start.get_json()["daemon_running"] is True
        assert daemon_control.is_running() is True

        stop = client.post("/daemon/stop", headers={"X-Auth-Token": "access"})
        assert stop.status_code == 200
        assert stop.get_json()["daemon_running"] is False
        assert daemon_control.is_running() is False
    finally:
        daemon_control.stop()
        daemon_control.remove_pid()


def test_stop_terminates_process_tree():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        daemon_control.write_pid(proc.pid)
        assert daemon_control.stop(timeout=8) is True
        proc.wait(timeout=8)
        assert daemon_control.pid_alive(proc.pid) is False
        assert daemon_control.read_pid() == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
