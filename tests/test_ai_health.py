"""AI 可用性预检：额度优先、缓存、通知去重与失败信息。"""
import json
import os
import time

import pytest

from hydroai.ai import health as health_mod
from hydroai.config.manager import ConfigManager


def _config(tmp_path, **overrides):
    path = tmp_path / "config.json"
    data = {
        "ai_model": "m1",
        "ai_base_url": "https://api.test",
        "ai_api_key": "env:TEST_KEY",
        "ai_vision_model": "",
        "editorial_model": "",
        "difficulty_skip_model": "",
        "model_router": {"tiers": {}},
        "models": {"m1": {}, "m2": {}},
        "ai_health_check_enable": True,
        "ai_health_check_ttl": 300,
        "ai_health_check_timeout": 5,
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return ConfigManager(str(path))


@pytest.fixture(autouse=True)
def _clear_health_cache():
    health_mod.clear_health_cache()
    yield
    health_mod.clear_health_cache()


def _patch_probe(monkeypatch, outcomes):
    calls = []

    def fake_probe(ai, model, timeout):
        calls.append(model)
        ok = outcomes.get(model, True)
        return {"model": model, "ok": ok, "error": "" if ok else "HTTP 503"}

    monkeypatch.setattr(health_mod, "_probe_model", fake_probe)
    return calls


def test_health_available_if_any_model_works(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {"m1": False, "m2": True})
    result = health_mod.check_ai_health(_config(tmp_path))
    assert result["available"] is True
    assert result["checked"] == 2
    assert set(calls) == {"m1", "m2"}
    assert result["cached"] is False


def test_health_unavailable_and_reason_is_sanitized(tmp_path, monkeypatch):
    _patch_probe(monkeypatch, {"m1": False, "m2": False})
    result = health_mod.check_ai_health(_config(tmp_path))
    assert result["available"] is False
    assert "m1" in result["reason"] and "HTTP 503" in result["reason"]
    message = health_mod.failure_message(result)
    assert "所有 AI 接口均不可用" in message
    assert "API Key" not in result["reason"]


def test_health_disabled_skips_probe(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {})
    result = health_mod.check_ai_health(
        _config(tmp_path, ai_health_check_enable=False))
    assert result["available"] is True and result["skipped"] is True
    assert calls == []


def test_health_result_is_cached_and_force_bypasses(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {"m1": True, "m2": True})
    cfg = _config(tmp_path)
    first = health_mod.check_ai_health(cfg)
    assert first["cached"] is False and len(calls) == 2
    second = health_mod.check_ai_health(cfg)
    assert second["cached"] is True and len(calls) == 2
    health_mod.check_ai_health(cfg, force=True)
    assert len(calls) == 4


def test_health_ttl_zero_reprobes(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {"m1": True, "m2": True})
    cfg = _config(tmp_path, ai_health_check_ttl=0)
    health_mod.check_ai_health(cfg)
    health_mod.check_ai_health(cfg)
    assert len(calls) == 4


def test_health_without_models_is_unavailable(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {})
    cfg = _config(tmp_path, ai_model="", models={})
    result = health_mod.check_ai_health(cfg)
    assert result["available"] is False
    assert "未配置任何 AI 模型" in result["reason"]
    assert calls == []


def test_health_uses_quota_before_probe(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {})
    cfg = _config(tmp_path, quotas={
        "wallet": {"kind": "balance", "source": "manual", "balance": 0}})
    result = health_mod.check_ai_health(cfg)
    assert result["source"] == "quota"
    assert result["available"] is False
    assert result["error_type"] == "ai_unavailable:quota"
    assert "额度用尽" in result["reason"]
    assert calls == []


def test_health_quota_positive_is_available_without_probe(tmp_path, monkeypatch):
    calls = _patch_probe(monkeypatch, {})
    cfg = _config(tmp_path, quotas={
        "wallet": {"kind": "balance", "source": "manual", "balance": 10}})
    result = health_mod.check_ai_health(cfg)
    assert result["source"] == "quota"
    assert result["available"] is True
    assert calls == []


def test_should_notify_once_per_error_and_version(tmp_path):
    cfg = _config(tmp_path)
    auth = {"error_type": "ai_unavailable:auth"}
    network = {"error_type": "ai_unavailable:network"}
    assert health_mod.should_notify(cfg, auth, channel="push") is True
    assert health_mod.should_notify(cfg, auth, channel="push") is False
    assert health_mod.should_notify(cfg, network, channel="push") is True

    future = time.time() + 10
    os.utime(cfg._path, (future, future))
    cfg.reload()
    assert health_mod.should_notify(cfg, auth, channel="push") is True
    assert health_mod.should_notify(cfg, network, channel="push") is True
