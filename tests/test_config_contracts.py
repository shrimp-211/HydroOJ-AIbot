"""验证配置持久化、有效模型与 Web 编辑之间的契约。"""
import json
import os

import pytest

from hydroai.config.cli import add_model
from hydroai.config.io import read_config, validate_document
from hydroai.config.manager import ConfigManager


def test_repair_does_not_persist_cli_and_environment_overrides(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"oj":{"root":"https://stored.test"}}', encoding='utf-8')
    monkeypatch.setenv('AI_MODEL', 'environment-model')
    cm = ConfigManager(path, cli_overrides={'oj_base': 'https://cli.test/d/other'})

    cm.repair()

    stored = read_config(path)
    assert stored['oj_root'] == 'https://stored.test'
    assert stored['oj_base'] == 'https://stored.test/d/system'
    assert stored['ai_model'] != 'environment-model'
    assert cm.oj_base == 'https://cli.test/d/other'


def test_explicit_null_model_key_does_not_use_another_connection_key(tmp_path):
    cm = ConfigManager(tmp_path / 'config.json', cli_overrides={
        'ai_api_key': 'sk-global', 'models': {
            'inherited': {}, 'cleared': {'api_key': None},
            'unresolved': {'api_key': 'env:TEST_ABSENT_PROVIDER_KEY'},
        },
    })
    assert cm.get_model_config('inherited')['api_key'] == 'sk-global'
    assert cm.get_model_config('cleared')['api_key'] == ''
    assert cm.get_model_config('unresolved')['api_key'] == ''


@pytest.mark.parametrize('value', [float('inf'), float('nan')])
def test_invalid_model_number_returns_validation_error(value):
    assert validate_document({'models': {'a': {'max_tokens': value}}})


def test_cli_single_model_switch_replaces_all_tiers_without_losing_router_options(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'model_router': {'thinking_levels': {'pro': 'high'}}}), encoding='utf-8')
    monkeypatch.setenv('AI_MODEL_MAX', 'old-model')

    add_model(path, name='fast', model_id='wire', base_url='https://ai.test/v1', set_default=True)

    cm = ConfigManager(path)
    assert [cm.get_tier_model(tier) for tier in ('flash', 'pro', 'max')] == ['fast'] * 3
    assert cm.get_tier_thinking('pro') == ['high']


def test_invalid_model_add_keeps_dotenv_and_json_unchanged(tmp_path, monkeypatch, auth, web_app):
    path = tmp_path / 'config.json'
    path.write_text('{"ai_model":"old"}', encoding='utf-8')
    env = tmp_path / '.env'
    env.write_text('# 原有配置\n', encoding='utf-8')
    monkeypatch.delenv('TEST_NEW_MODEL_KEY', raising=False)
    client = auth(web_app.test_client())

    response = client.post('/models/add', json={
        'name': 'new', 'model_id': 'wire', 'base_url': 'not a URL',
        'api_key_env': 'TEST_NEW_MODEL_KEY', 'api_key_value': 'sk-new',
    })

    assert response.status_code == 400
    assert env.read_text(encoding='utf-8') == '# 原有配置\n'
    assert read_config(path) == {'ai_model': 'old'}
    assert 'TEST_NEW_MODEL_KEY' not in os.environ


def test_settings_snapshot_refuses_overwriting_a_newer_provider_switch(tmp_path, monkeypatch, auth, web_app):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ai_model': 'a', 'models': {'a': {}, 'b': {}}}), encoding='utf-8')
    monkeypatch.setenv('OJ_CONTEST_START_PAGE', '5')
    client = auth(web_app.test_client())

    stored = client.get('/config?view=stored')
    assert stored.json == {'ai_model': 'a', 'models': {'a': {}, 'b': {}}}
    assert client.get('/config').json['contest_start_page'] == 5
    assert client.post('/api/models/switch', json={'name': 'b'}).status_code == 200
    response = client.post('/config', json={'ai_model': 'a'}, headers={'If-Match': stored.headers['ETag']})
    assert response.status_code == 409
    assert read_config(path)['ai_model'] == 'b'


def test_provider_usage_includes_effective_tiers_from_environment(tmp_path, monkeypatch, auth, web_app):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ai_model': 'a', 'models': {'a': {}, 'b': {}}}), encoding='utf-8')
    monkeypatch.setenv('AI_MODEL_FLASH', 'b')
    client = auth(web_app.test_client())

    data = client.get('/api/providers').json
    assert data['active']['tiers']['flash'] == 'b'
    assert client.post('/api/models/delete', json={'name': 'b'}).status_code == 400


def test_switch_does_not_claim_to_override_environment(tmp_path, monkeypatch, auth, web_app):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ai_model': 'a', 'models': {'a': {}, 'b': {}}}), encoding='utf-8')
    monkeypatch.setenv('AI_MODEL', 'a')
    client = auth(web_app.test_client())

    response = client.post('/api/models/switch', json={'name': 'b'})
    assert response.status_code == 409
    assert 'AI_MODEL' in response.json['error']
    assert read_config(path)['ai_model'] == 'a'


def test_single_oj_login_honors_cli_override_over_environment(tmp_path, monkeypatch):
    from hydroai.oj import client as oj_client

    monkeypatch.setenv('OJ_USERNAME', 'environment-user')
    monkeypatch.setenv('OJ_PASSWORD', 'environment-password')
    cfg = ConfigManager(tmp_path / 'config.json', cli_overrides={
        'username': 'cli-user', 'password': 'cli-password',
    })
    calls = []
    monkeypatch.setattr(oj_client, 'smart_login', lambda *args: calls.append(args[2:4]) or True)
    client = oj_client.OJClient(cfg)
    try:
        assert client.login()
        assert calls == [('cli-user', 'cli-password')]
    finally:
        client.session.close()
