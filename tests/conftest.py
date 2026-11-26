"""Test isolation: never let a developer's local .env leak into test cases.

Some entry points call ``load_dotenv()`` while running, which can inject the
repository's real ``.env`` into ``os.environ`` and affect later tests. Every
test therefore starts with application-owned variables removed; tests that need
them set them explicitly through ``monkeypatch``.
"""
import os

import pytest

APP_ENV_PREFIXES = ("OJ_", "AI_", "PRICING_", "DEEPSEEK_", "ZHIPU_")


@pytest.fixture(autouse=True)
def _clean_app_environment(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith(APP_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    # Keep dashboard.json / usage.db / cookies inside the per-test temp dir.
    # Tests that need a different location override OJ_DATA_DIR explicitly.
    monkeypatch.setenv("OJ_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def web_app(tmp_path):
    from hydroai.web.app import create_app
    return create_app(tmp_path, config={"TESTING": True, "WEBUI_USERNAME": "admin", "WEBUI_PASSWORD": "secret"})


@pytest.fixture
def auth():
    """为 Flask 测试客户端写入登录会话，等价于已通过登录页。"""
    def _auth(client, username="admin"):
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = username
        return client
    return _auth
