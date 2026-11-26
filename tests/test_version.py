"""版本号在包、CLI、Web 与 Dockerfile 中保持一致。"""
from pathlib import Path

import pytest

from hydroai import __version__
from hydroai.config.cli import main


def test_package_version_and_docker_label_agree():
    assert __version__ == "2.3.0"
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(
        encoding="utf-8")
    assert f"ARG APP_VERSION={__version__}" in dockerfile


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_web_requires_login(web_app):
    client = web_app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/login").status_code == 200
    assert client.post("/login", data={
        "username": "admin", "password": "wrong"}).status_code == 401
    assert client.post("/login", data={
        "username": "nobody", "password": "secret"}).status_code == 401
    assert client.post("/login", data={
        "username": "admin", "password": "secret"}).status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_web_refuses_login_without_password(web_app):
    web_app.config["WEBUI_PASSWORD"] = ""
    client = web_app.test_client()
    assert client.get("/login").status_code == 503


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://preview.example.test', 'Sec-Fetch-Site': 'same-site'},
    {'Origin': 'https://panel.example.test:8443'},
    {'Origin': 'http://panel.example.test'},
    {'Origin': 'null'},
    {'Referer': 'https://preview.example.test/settings'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_web_write_rejects_foreign_browser_source(web_app, monkeypatch, headers):
    origin = 'https://panel.example.test'
    client = web_app.test_client()
    with client.session_transaction(base_url=origin) as sess:
        sess.update(authenticated=True, username='admin')
    cleared = []
    monkeypatch.setattr(web_app.extensions['workspace'], 'clear_logs', lambda: cleared.append(True))
    response = client.post('/clear', base_url=origin, headers=headers)
    assert response.status_code == 403
    assert response.get_json()['code'] == 'origin_mismatch'
    assert not cleared
    assert client.get('/status', base_url=origin, headers=headers).status_code == 200


@pytest.mark.parametrize(('base_url', 'headers'), [
    ('http://panel.example.test', {}),
    ('https://panel.example.test', {'Origin': 'https://panel.example.test:443'}),
    ('https://panel.example.test', {'Referer': 'https://panel.example.test/settings'}),
    ('http://panel.example.test:8443', {
        'Origin': 'https://panel.example.test:8443', 'Sec-Fetch-Site': 'same-origin',
    }),
])
def test_browser_login_and_logout_accept_same_origin_and_script_clients(web_app, base_url, headers):
    client = web_app.test_client()
    assert client.post('/login', base_url=base_url, headers=headers, data={
        'username': 'admin', 'password': 'secret',
    }).status_code == 302
    assert client.get('/', base_url=base_url).status_code == 200
    assert client.post('/logout', base_url=base_url, headers=headers).status_code == 302
    assert client.get('/', base_url=base_url).status_code == 302


def test_login_post_rejects_foreign_origin(web_app):
    client = web_app.test_client()
    response = client.post('/login', data={'username': 'admin', 'password': 'secret'},
                           headers={'Origin': 'https://other.example.test'})
    assert response.status_code == 403
    assert client.get('/').status_code == 302


def test_web_exposes_version(auth, web_app):
    client = auth(web_app.test_client())
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.get_json()["version"] == __version__
    assert client.get("/status").get_json()["version"] == __version__
    page = client.get("/").get_data(as_text=True)
    assert f"v{__version__}" in page
