"""供应商预置目录与 Web 模型切换 API。"""
import json
import os

import pytest

from hydroai.ai.providers import CATEGORIES, PRESETS, preset_list


@pytest.fixture
def web(tmp_path, web_app):
    return web_app.test_client(), tmp_path / "config.json"


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


def test_web_providers_list_and_switch(web, auth):
    client, path = web
    auth(client)
    path.write_text(json.dumps({
        "ai_model": "a",
        "models": {
            "a": {"base_url": "https://a.test/v1", "api_key": "a-secret"},
            "b": {"base_url": "https://b.test/v1", "api_key": "b-secret"},
        },
    }), encoding="utf-8")

    data = client.get("/api/providers").get_json()
    assert data["presets"] and data["categories"]
    assert [item["name"] for item in data["configured"]] == ["a", "b"]
    assert data["configured"][0]["current"] is True
    assert data["configured"][0]["api_key_configured"] is True

    response = client.post("/api/models/switch", json={"name": "b", "single": True})
    assert response.status_code == 200
    assert response.get_json()["active"] == "b"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ai_model"] == "b"
    assert saved["model_router"]["tiers"] == {
        "flash": "b", "pro": "b", "max": "b"}

    data = client.get("/api/providers").get_json()
    assert data["active"]["model"] == "b"
    assert data["configured"][0]["name"] == "b"
    assert data["configured"][0]["current"] is True


def test_web_model_roles_and_delete(web, auth):
    client, path = web
    auth(client)
    path.write_text(json.dumps({
        "ai_model": "a",
        "models": {
            "a": {"base_url": "https://a.test/v1", "api_key": "a"},
            "b": {"base_url": "https://b.test/v1", "api_key": "b"},
            "c": {"base_url": "https://c.test/v1", "api_key": "c"},
        },
    }), encoding="utf-8")

    data = client.get("/api/providers").get_json()
    assert {role["id"] for role in data["roles"]} >= {
        "default", "flash", "pro", "max", "editorial", "vision",
        "difficulty", "benchmark", "testdata"}

    response = client.post("/api/models/role",
                           json={"role": "editorial", "name": "b"})
    assert response.status_code == 200
    response = client.post("/api/models/role",
                           json={"role": "flash", "name": "b"})
    assert response.status_code == 200
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["editorial_model"] == "b"
    assert saved["model_router"]["tiers"]["flash"] == "b"

    blocked = client.post("/api/models/delete",
                          json={"name": "b"})
    assert blocked.status_code == 400
    assert "用途" in blocked.get_json()["error"]

    deleted = client.post("/api/models/delete",
                          json={"name": "c"})
    assert deleted.status_code == 200
    assert "c" not in json.loads(path.read_text(encoding="utf-8"))["models"]

    current = client.post("/api/models/delete",
                          json={"name": "a"})
    assert current.status_code == 400

    # 清空可选用途后，b 仍被 flash 使用，继续阻止删除。
    clear = client.post("/api/models/role",
                        json={"role": "editorial", "name": ""})
    assert clear.status_code == 200


def test_web_discover_with_key_and_add(web, monkeypatch, auth):
    client, path = web
    auth(client)
    path.write_text(json.dumps({"ai_model": "", "models": {}}), encoding="utf-8")
    seen = {}

    def fake_discover(settings):
        seen.update(settings)
        return {"base_url": settings["base_url"],
                "models": [{"id": "model-one"}, {"id": "model-two"}]}

    import hydroai.ai.services as services
    monkeypatch.setattr(services, "discover_models", fake_discover)
    data = client.post("/models/discover", json={
        "base_url": "https://provider.test/v1",
        "api_key_env": "TEST_PROVIDER_KEY",
        "api_key_value": "test-secret",
        "provider": "compatible",
    }).get_json()
    assert [row["id"] for row in data["models"]] == ["model-one", "model-two"]
    assert seen["api_key"] == "test-secret"

    monkeypatch.delenv("TEST_PROVIDER_KEY", raising=False)
    response = client.post("/models/add", json={
        "name": "p", "model_id": "model-one",
        "base_url": "https://provider.test/v1",
        "api_key_env": "TEST_PROVIDER_KEY", "provider": "compatible",
        "api_key_value": "test-secret", "set_default": True,
    })
    assert response.status_code == 200
    saved = path.read_text(encoding="utf-8")
    assert "test-secret" not in saved
    assert "env:TEST_PROVIDER_KEY" in saved
    assert "TEST_PROVIDER_KEY=test-secret" in (path.parent / ".env").read_text(
        encoding="utf-8")
    assert os.environ.get("TEST_PROVIDER_KEY") == "test-secret"
    os.environ.pop("TEST_PROVIDER_KEY", None)


def test_add_model_proxy_field_and_validation(tmp_path):
    from hydroai.config.cli import add_model
    from hydroai.config.io import read_config, validate_document
    path = tmp_path / "config.json"
    add_model(path, name="p", model_id="m", base_url="https://p.test/v1",
              api_key_env="P_KEY", proxy="http://127.0.0.1:7890")
    model = read_config(path)["models"]["p"]
    assert model["proxy"] == "http://127.0.0.1:7890"
    assert validate_document({"models": {"p": {"proxy": "ftp://bad"}}})
    assert not validate_document({"models": {"p": {"proxy": "socks5://127.0.0.1:1080"}}})


def test_web_update_model_proxy(web, auth):
    client, path = web
    auth(client)
    path.write_text(json.dumps({
        "ai_model": "a",
        "models": {"a": {"base_url": "https://a.test/v1", "api_key": "a"}},
    }), encoding="utf-8")
    response = client.post("/api/models/proxy",
                           json={"name": "a", "proxy": "http://127.0.0.1:7890"})
    assert response.status_code == 200
    assert json.loads(path.read_text(encoding="utf-8"))["models"]["a"]["proxy"] == \
        "http://127.0.0.1:7890"
    response = client.post("/api/models/proxy",
                           json={"name": "a", "proxy": ""})
    assert response.status_code == 200
    assert "proxy" not in json.loads(path.read_text(encoding="utf-8"))["models"]["a"]
    invalid = client.post("/api/models/proxy",
                          json={"name": "a", "proxy": "ftp://bad"})
    assert invalid.status_code == 400


def test_web_add_preset_and_switch(web, auth):
    client, path = web
    auth(client)
    path.write_text(json.dumps({"ai_model": "", "models": {}}), encoding="utf-8")
    response = client.post("/models/add", json={
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

    data = client.get("/api/providers").get_json()
    assert data["configured"][0]["name"] == "deepseek"
    assert data["configured"][0]["current"] is True


def test_web_providers_require_auth_and_unknown_switch(web, auth):
    client, path = web
    assert client.get("/api/providers").status_code == 403
    auth(client)
    path.write_text(json.dumps({"ai_model": "a", "models": {}}), encoding="utf-8")
    response = client.post("/api/models/switch",
                           json={"name": "missing", "single": True})
    assert response.status_code == 400
