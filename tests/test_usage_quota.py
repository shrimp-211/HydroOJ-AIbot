"""逐请求用量账本与订阅额度（无网络）。"""
import json
import time
from pathlib import Path

import pytest

from hydroai.ai.client import AIClient
from hydroai.config.io import validate_document
from hydroai.config.manager import ConfigManager
from hydroai.usage.quota import QuotaManager
from hydroai.usage.store import UsageStore, get_store


def _store(tmp_path) -> UsageStore:
    return UsageStore(tmp_path / "usage.db")


def _manager(tmp_path, quotas, *, models=None, api_key="sk-test") -> ConfigManager:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "ai_model": "m",
        "ai_base_url": "https://api.deepseek.com",
        "ai_api_key": api_key,
        "models": models or {},
        "quotas": quotas,
    }), encoding="utf-8")
    return ConfigManager(str(path))


def test_record_summary_daily_breakdown_and_recent(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    assert store.record(model="m", provider="p", task="solve", problem_id="1",
                        usage={"input": 100, "output": 50, "cache_hit": 40},
                        cost=0.01, cost_known=True, elapsed_s=2.0, ok=True, ts=now - 60)
    assert store.record(model="m", provider="p", task="plan", problem_id="2",
                        usage={"input": 200, "output": 10},
                        cost=0.02, cost_known=True, elapsed_s=1.0, ok=True, ts=now - 30)
    assert store.record(model="n", provider="p", task="solve", problem_id="3",
                        usage={}, cost=0.0, cost_known=False, elapsed_s=0.5, ok=False, ts=now)

    summary = store.summary(since=now - 120)
    assert summary["requests"] == 3
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 60
    assert summary["total_tokens"] == 360
    assert summary["cache_hit_tokens"] == 40
    assert summary["cache_rate"] == pytest.approx(40 / 300, abs=1e-4)
    assert summary["cost"] == pytest.approx(0.03)
    assert summary["failed_requests"] == 1
    assert summary["avg_output_speed"] == pytest.approx(60 / 3.5, rel=1e-3)

    filtered = store.summary(since=now - 120, models=["m"])
    assert filtered["requests"] == 2

    breakdown = {row["key"]: row for row in store.breakdown("model", since=now - 120)}
    assert breakdown["m"]["requests"] == 2
    assert breakdown["n"]["requests"] == 1

    by_task = {row["key"]: row for row in store.breakdown("task", since=now - 120)}
    assert set(by_task) == {"solve", "plan"}

    recent = store.recent(10, since=now - 120)
    assert len(recent) == 3
    assert recent[0]["problem_id"] == "3"          # 最新的在前
    assert recent[0]["ok"] is False
    assert "time" in recent[0]

    daily = store.daily(days=7)
    assert len(daily) == 7
    assert daily[-1]["requests"] == 3

    heat = store.heatmap(days=7)
    assert heat["total_requests"] == 3


def test_prune_removes_old_rows(tmp_path):
    store = _store(tmp_path)
    old = time.time() - 10 * 86400
    store.record(model="m", usage={"input": 1}, ts=old)
    store.record(model="m", usage={"input": 1}, ts=time.time())
    assert store.prune(retention_days=3) == 1
    assert store.summary()["requests"] == 1


def test_notification_allowed_once_per_error_and_config_version(tmp_path):
    store = _store(tmp_path)
    auth = "ai_unavailable:auth:requester"
    network = "ai_unavailable:network:requester"
    assert store.notification_allowed(auth, "v1") is True
    assert store.notification_allowed(auth, "v1") is False
    assert store.notification_allowed(network, "v1") is True
    # 配置版本变化后，所有错误类型可以再次通知。
    assert store.notification_allowed(auth, "v2") is True
    assert store.notification_allowed(network, "v2") is True


def test_prune_if_due_is_throttled_by_meta(tmp_path):
    store = _store(tmp_path)
    old = time.time() - 10 * 86400
    store.record(model="m", usage={"input": 1}, ts=old)
    store.record(model="m", usage={"input": 1}, ts=time.time())
    assert store.prune_if_due(retention_days=3, interval_hours=24) == 1
    store.record(model="m", usage={"input": 1}, ts=old)
    assert store.prune_if_due(retention_days=3, interval_hours=24) == 0
    assert store.summary()["requests"] == 2


def test_window_quota_uses_local_usage_and_reset(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.record(model="m", provider="p", usage={"input": 10, "output": 10},
                 cost=0.3, cost_known=True, ts=now - 120)
    store.record(model="m", provider="p", usage={"input": 10, "output": 10},
                 cost=0.5, cost_known=True, ts=now - 60)
    cfg = _manager(tmp_path, {
        "plan": {"models": ["m"], "kind": "window", "unit": "cost",
                 "limit": 1.0, "window_hours": 1, "low_ratio": 0.5},
    })
    item = QuotaManager(cfg, store=store).status()[0]
    assert item["kind"] == "window"
    assert item["used"] == pytest.approx(0.8)
    assert item["remaining"] == pytest.approx(0.2)
    assert item["ratio"] == pytest.approx(0.2)
    assert item["status"] == "low"                    # 0.2 <= low_ratio 0.5
    assert item["reset_in_s"] is not None and item["reset_in_s"] > 0
    assert item["source"] == "local"


def test_window_quota_period_5h_and_request_unit(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    for _ in range(3):
        store.record(model="m", usage={}, ts=now - 30)
    cfg = _manager(tmp_path, {
        "five": {"models": ["m"], "kind": "window", "unit": "requests",
                 "limit": 10, "period": "5h"},
    })
    item = QuotaManager(cfg, store=store).status()[0]
    assert item["used"] == 3
    assert item["remaining"] == 7
    assert item["window"] == "5h"


def test_multiple_quotas_keep_order_in_parallel(tmp_path, monkeypatch):
    _mock_get(monkeypatch, {"balance": 1.0})
    cfg = _manager(tmp_path, {
        name: {"kind": "balance", "source": "stepfun", "api_key": "WALLET_KEY"}
        for name in ("a", "b", "c")
    })
    monkeypatch.setenv("WALLET_KEY", "secret")
    items = QuotaManager(cfg, store=_store(tmp_path)).status()
    assert [item["id"] for item in items] == ["a", "b", "c"]
    assert all(item["remaining"] == 1.0 for item in items)


def test_manual_balance_low_threshold(tmp_path):
    cfg = _manager(tmp_path, {
        "wallet": {"kind": "balance", "source": "manual", "balance": 3.0,
                   "currency": "CNY", "low_threshold": 5.0},
    })
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 3.0
    assert item["status"] == "low"
    assert item["source"] == "manual"


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload


def test_deepseek_balance_adapter_caches_and_never_follows_redirect(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(200, {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "12.50"}]})

    monkeypatch.setattr("hydroai.usage.quota.requests.get", fake_get)
    cfg = _manager(tmp_path, {
        "ds": {"models": ["m"], "kind": "balance", "source": "deepseek",
               "low_threshold": 5.0, "cache_seconds": 300},
    }, models={"m": {"api_key": "sk-model"}})
    manager = QuotaManager(cfg, store=_store(tmp_path))
    first = manager.status()[0]
    second = manager.status()[0]
    assert first["remaining"] == 12.5 and first["source"] == "deepseek"
    assert second["remaining"] == 12.5
    assert len(calls) == 1                              # 命中缓存
    assert calls[0][0].endswith("/user/balance")
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["headers"]["Authorization"] == "Bearer sk-model"

    def redirect_get(url, **kwargs):
        return _Response(302)

    monkeypatch.setattr("hydroai.usage.quota.requests.get", redirect_get)
    item = manager.status(refresh=True)[0]
    assert item["status"] in ("error", "stale")
    assert "重定向" in (item.get("error") or "")


def test_generic_balance_json_path(tmp_path, monkeypatch):
    def fake_get(url, **kwargs):
        assert url == "https://gateway.test/api/balance"
        return _Response(200, {"data": {"remaining": "8.25"}})

    monkeypatch.setattr("hydroai.usage.quota.requests.get", fake_get)
    cfg = _manager(tmp_path, {
        "gw": {"kind": "balance", "source": "generic",
               "endpoint": "https://gateway.test/api/balance",
               "json_path": "data.remaining", "api_key": "GATEWAY_KEY"},
    })
    monkeypatch.setenv("GATEWAY_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 8.25
    assert item["source"] == "generic"


def test_quota_config_validation():
    assert validate_document({"quotas": {
        "w": {"kind": "window", "unit": "cost", "limit": 10, "period": "5h"}}}) == []
    errors = validate_document({"quotas": {
        "bad": {"kind": "oops", "source": "nope", "unit": "kg", "period": "hourly",
                "limit": -1, "models": [1]}}})
    assert any("kind" in e for e in errors)
    assert any("source" in e for e in errors)
    assert any("unit" in e for e in errors)
    assert any("period" in e for e in errors)
    assert any("limit" in e for e in errors)
    assert any("models" in e for e in errors)


def _mock_get(monkeypatch, payload, *, status=200):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(status, payload)

    monkeypatch.setattr("hydroai.usage.quota.requests.get", fake_get)
    return calls


def test_kimi_plan_tiers(tmp_path, monkeypatch):
    reset = int((time.time() + 3600) * 1000)
    calls = _mock_get(monkeypatch, {
        "limits": [{"detail": {"limit": 100, "remaining": 25, "resetTime": reset}}],
        "usage": {"limit": 1000, "remaining": 500, "resetTime": reset},
    })
    cfg = _manager(tmp_path, {
        "kimi": {"kind": "plan", "source": "kimi", "api_key": "KIMI_KEY",
                 "low_ratio": 0.3},
    })
    monkeypatch.setenv("KIMI_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["kind"] == "plan" and item["unit"] == "percent"
    assert [tier["name"] for tier in item["tiers"]] == ["five_hour", "weekly"]
    assert item["tiers"][0]["remaining_percent"] == 25.0
    assert item["tiers"][1]["remaining_percent"] == 50.0
    assert item["remaining"] == 25.0
    assert item["status"] == "low"                    # 25% <= low_ratio 30%
    assert item["reset_in_s"] and item["reset_in_s"] > 0
    assert calls[0][0].endswith("/usages")
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_zhipu_plan_tiers_and_no_plan(tmp_path, monkeypatch):
    reset = int((time.time() + 3600) * 1000)
    calls = _mock_get(monkeypatch, {"success": True, "data": {"level": "Pro", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 20, "nextResetTime": reset},
        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 40, "nextResetTime": reset},
    ]}})
    cfg = _manager(tmp_path, {
        "glm": {"kind": "plan", "source": "zhipu_plan", "api_key": "ZHIPU_KEY",
                "base_url": "https://open.bigmodel.cn"},
    })
    monkeypatch.setenv("ZHIPU_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    by_name = {tier["name"]: tier for tier in item["tiers"]}
    assert by_name["five_hour"]["remaining_percent"] == 80.0
    assert by_name["weekly"]["remaining_percent"] == 60.0
    assert item["remaining"] == 60.0
    assert item["plan_name"] == "Pro"
    assert calls[0][1]["headers"]["Authorization"] == "secret"   # 智谱不加 Bearer

    _mock_get(monkeypatch, {"success": False, "msg": "当前用户不存在coding plan"})
    broken = QuotaManager(cfg, store=_store(tmp_path)).status(refresh=True)[0]
    assert broken["status"] in ("error", "stale")
    assert "Coding Plan" in broken["error"]


def test_minimax_plan_tiers(tmp_path, monkeypatch):
    reset = int((time.time() + 3600) * 1000)
    _mock_get(monkeypatch, {
        "base_resp": {"status_code": 0},
        "model_remains": [{
            "model_name": "general",
            "current_interval_remaining_percent": 70,
            "end_time": reset,
            "current_weekly_status": 1,
            "current_weekly_remaining_percent": 40,
            "weekly_end_time": reset,
        }],
    })
    cfg = _manager(tmp_path, {
        "mm": {"kind": "plan", "source": "minimax", "api_key": "MINIMAX_KEY",
               "base_url": "https://api.minimaxi.com", "low_ratio": 0.5},
    })
    monkeypatch.setenv("MINIMAX_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    by_name = {tier["name"]: tier for tier in item["tiers"]}
    assert by_name["five_hour"]["remaining_percent"] == 70.0
    assert by_name["weekly"]["remaining_percent"] == 40.0
    assert item["remaining"] == 40.0
    assert item["status"] == "low"                    # 40% <= 50%


def test_opencode_go_plan_tiers(tmp_path, monkeypatch):
    reset = time.time() + 3600
    _mock_get(monkeypatch, {"usage": {
        "rolling": {"percent": 10, "resetsAt": reset},
        "weekly": {"percent": 80, "resetsAt": reset},
        "monthly": {"percent": 50, "resetsAt": reset},
    }})
    cfg = _manager(tmp_path, {
        "go": {"kind": "plan", "source": "opencode_go", "api_key": "OPENCODE_KEY"},
    })
    monkeypatch.setenv("OPENCODE_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    by_name = {tier["name"]: tier for tier in item["tiers"]}
    assert by_name["five_hour"]["remaining_percent"] == 90.0
    assert by_name["weekly"]["remaining_percent"] == 20.0
    assert by_name["monthly"]["remaining_percent"] == 50.0
    assert item["remaining"] == 20.0


def test_ollama_and_mimo_plan_tiers(tmp_path, monkeypatch):
    _mock_get(monkeypatch, {"included": {
        "balance_usd": 80, "allowance_usd": 100,
        "period": {"until": time.time() + 86400},
    }})
    cfg = _manager(tmp_path, {
        "ollama": {"kind": "plan", "source": "ollama", "api_key": "OLLAMA_KEY"},
    })
    monkeypatch.setenv("OLLAMA_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["tiers"][0]["name"] == "monthly"
    assert item["tiers"][0]["remaining_percent"] == 80.0

    _mock_get(monkeypatch, {
        "quota": {"total_credits": 1000, "used_credits": 250},
        "period": {"end_at": "2099-12-01 00:00:00"},
    })
    cfg = _manager(tmp_path, {
        "mimo": {"kind": "plan", "source": "mimo_plan", "api_key": "MIMO_KEY"},
    })
    monkeypatch.setenv("MIMO_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["tiers"][0]["name"] == "credits"
    assert item["tiers"][0]["remaining_percent"] == 75.0
    assert item["tiers"][0]["reset_in_s"] > 0


@pytest.mark.parametrize("source,payload,expected,url_suffix", [
    ("stepfun", {"balance": 12.5}, 12.5, "/v1/accounts"),
    ("novita", {"availableBalance": "123456"}, 12.3456, "/v3/user/balance"),
])
def test_new_balance_sources(tmp_path, monkeypatch, source, payload, expected, url_suffix):
    calls = _mock_get(monkeypatch, payload)
    cfg = _manager(tmp_path, {
        "wallet": {"kind": "balance", "source": source, "api_key": "WALLET_KEY"},
    })
    monkeypatch.setenv("WALLET_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == pytest.approx(expected, abs=1e-4)
    assert calls[0][0].endswith(url_suffix)


def test_mimo_and_zhipu_balances(tmp_path, monkeypatch):
    _mock_get(monkeypatch, {"balance_infos": [
        {"currency": "CNY", "total_available": "10.00", "frozen_balance": "1.00"}]})
    cfg = _manager(tmp_path, {
        "mimo": {"kind": "balance", "source": "mimo", "api_key": "MIMO_KEY"},
    })
    monkeypatch.setenv("MIMO_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 9.0 and item["currency"] == "CNY"

    calls = _mock_get(monkeypatch, {"data": {"availableBalance": 8.5}})
    cfg = _manager(tmp_path, {
        "glm": {"kind": "balance", "source": "zhipu", "api_key": "ZHIPU_KEY",
                "base_url": "https://open.bigmodel.cn"},
    })
    monkeypatch.setenv("ZHIPU_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 8.5
    assert calls[0][1]["headers"]["Authorization"] == "secret"


def test_newapi_and_sub2api_balances(tmp_path, monkeypatch):
    calls = _mock_get(monkeypatch, {"success": True, "data": {
        "quota": 500000, "used_quota": 1000000, "group": "vip"}})
    cfg = _manager(tmp_path, {
        "newapi": {"kind": "balance", "source": "newapi",
                   "base_url": "https://newapi.test", "api_key": "NEWAPI_KEY",
                   "headers": {"New-Api-User": "42"}},
    })
    monkeypatch.setenv("NEWAPI_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 1.0
    assert item["used"] == 2.0
    assert item["limit"] == 3.0
    assert item["note"] == "vip"
    assert calls[0][0] == "https://newapi.test/api/user/self"
    assert calls[0][1]["headers"]["New-Api-User"] == "42"

    calls = _mock_get(monkeypatch, {
        "isValid": True, "remaining": 5.5,
        "quota": {"used": 1.5, "limit": 7.0, "unit": "USD"}, "planName": "Pro"})
    cfg = _manager(tmp_path, {
        "sub": {"kind": "balance", "source": "sub2api",
                "base_url": "https://sub.test/v1", "api_key": "SUB_KEY"},
    })
    monkeypatch.setenv("SUB_KEY", "secret")
    item = QuotaManager(cfg, store=_store(tmp_path)).status()[0]
    assert item["remaining"] == 5.5
    assert item["note"] == "Pro"
    assert calls[0][0] == "https://sub.test/v1/usage"


def test_ai_client_records_request_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("OJ_DATA_DIR", str(tmp_path))
    cfg = _manager(tmp_path, {}, models={
        "m": {"pricing": {"input": 1.0, "output": 2.0, "cache_hit": 0.1}}})
    ai = AIClient(cfg)
    monkeypatch.setattr(ai, "_get_client", lambda *a, **kw: object())
    usage = {"prompt_tokens": 100, "completion_tokens": 20,
             "prompt_cache_hit_tokens": 10, "total_tokens": 120}
    monkeypatch.setattr(ai, "_block_call",
                        lambda args, *, model: ("```cpp\nint main(){}\n```", "", usage))
    result = ai.chat([{"role": "user", "content": "hi"}], task="solve")
    assert result["usage"]["input"] == 100
    rows = get_store().recent(5)
    assert len(rows) == 1
    row = rows[0]
    assert row["model"] == "m"
    assert row["task"] == "solve"
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 20
    assert row["cache_hit_tokens"] == 10
    assert row["cost_known"] is True
