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
