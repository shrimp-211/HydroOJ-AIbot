"""供应商预置目录与 Web 模型切换 API。"""
import json

import pytest

from hydroai.ai.providers import CATEGORIES, PRESETS, preset_list


@pytest.fixture
def web(tmp_path, monkeypatch):
    import hydroai.web.app as webui
    monkeypatch.setattr(webui, "ROOT", tmp_path)
    monkeypatch.setattr(webui, "AUTH_TOKEN", "access")
    return webui.app.test_client(), tmp_path / "config.json"


def test_preset_catalog_is_complete_and_unique():
    ids = [item["id"] for item in PRESETS]
    assert len(ids) == len(set(ids))
    assert len(PRESETS) >= 40
    category_ids = {item["id"] for item in CATEGORIES}
    assert all(item["category"] in category_ids for item in PRESETS)
    assert any(item["category"] == "account" and item["addable"] is False
               for item in PRESETS)
    for item in preset_list():
        assert item["id"] and item["name"]
        assert "api_key" not in item  # 不向前端暴露任何密钥字段


def test_web_providers_list_and_switch(web):
    client, path = web
    headers = {"X-Auth-Token": "access"}
    path.write_text(json.dumps({
        "ai_model": "a",
        "models": {
            "a": {"base_url": "https://a.test/v1", "api_key": "a-secret"},
            "b": {"base_url": "https://b.test/v1", "api_key": "b-secret"},
        },
    }), encoding="utf-8")

    data = client.get("/api/providers", headers=headers).get_json()
    assert data["presets"] and data["categories"]
    assert [item["name"] for item in data["configured"]] == ["a", "b"]
    assert data["configured"][0]["current"] is True
    assert data["configured"][0]["api_key_configured"] is True

    response = client.post("/api/models/switch", headers=headers,
                           json={"name": "b", "single": True})
    assert response.status_code == 200
    assert response.get_json()["active"] == "b"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ai_model"] == "b"
    assert saved["model_router"]["tiers"] == {
        "flash": "b", "pro": "b", "max": "b"}

    data = client.get("/api/providers", headers=headers).get_json()
    assert data["active"]["model"] == "b"
    assert data["configured"][0]["name"] == "b"
    assert data["configured"][0]["current"] is True


def test_web_add_preset_and_switch(web):
    client, path = web
    headers = {"X-Auth-Token": "access"}
    path.write_text(json.dumps({"ai_model": "", "models": {}}), encoding="utf-8")
    response = client.post("/models/add", headers=headers, json={
        "name": "deepseek",
        "model_id": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY",
        "provider": "deepseek",
        "pricing_provider": "deepseek",
        "set_default": True,
    })
    assert response.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ai_model"] == "deepseek"
    assert saved["models"]["deepseek"]["base_url"] == "https://api.deepseek.com"
    assert saved["models"]["deepseek"]["api_key"] == "env:DEEPSEEK_API_KEY"

    data = client.get("/api/providers", headers=headers).get_json()
    assert data["configured"][0]["name"] == "deepseek"
    assert data["configured"][0]["current"] is True


def test_web_providers_require_auth_and_unknown_switch(web):
    client, path = web
    assert client.get("/api/providers").status_code == 403
    path.write_text(json.dumps({"ai_model": "a", "models": {}}), encoding="utf-8")
    response = client.post("/api/models/switch",
                           headers={"X-Auth-Token": "access"},
                           json={"name": "missing", "single": True})
    assert response.status_code == 400
