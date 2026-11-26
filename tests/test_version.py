"""版本号在包、CLI、Web 与 Dockerfile 中保持一致。"""
from pathlib import Path

import pytest

from hydroai import __version__
from hydroai.config.cli import main
from hydroai.web import app as webui


def test_package_version_and_docker_label_agree():
    assert __version__ == "2.2.0"
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8")
    assert f"ARG APP_VERSION={__version__}" in dockerfile


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_web_requires_token_login(monkeypatch):
    monkeypatch.setattr(webui, "AUTH_TOKEN", "secret")
    client = webui.app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={"token": "wrong"}).status_code == 401
    assert client.post("/login", data={"token": "secret"}).status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_web_exposes_version(monkeypatch):
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    client = webui.app.test_client()
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.get_json()["version"] == __version__
    assert client.get("/status").get_json()["version"] == __version__
    page = client.get("/", headers={"X-Auth-Token": "access"}).get_data(as_text=True)
    assert f"v{__version__}" in page
