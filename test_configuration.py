"""Configuration lifecycle, offline diagnostics and Web API regressions."""
import json
from pathlib import Path
import pytest

from config_cli import initialize, diagnose, main
from config_io import read_config, validate_document
from config_manager import ConfigManager
from model_router import ModelRouter


def write(path, data):
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_initialize_economy_and_refuse_overwrite(tmp_path):
    path = tmp_path / 'config.json'
    cfg = initialize(path, model='model', base_url='https://api.test/v1', oj_root='https://oj.test', profile='economy')
    assert not cfg['difficulty_detect_enable'] and cfg['solve_candidates'] == 2
    assert cfg['agent_stress_enable'] and cfg['editorial_enable']
    assert (tmp_path / '.env').exists()
    with pytest.raises(ValueError):
        initialize(path, model='other', base_url='https://x', oj_root='https://x')
    assert read_config(path)['ai_model'] == 'model'


def test_initialize_keeps_existing_env(tmp_path):
    env = tmp_path / '.env'
    env.write_text('AI_API_KEY=keep-me\n')
    initialize(tmp_path / 'config.json', model='m', base_url='https://api.test', oj_root='https://oj.test')
    assert env.read_text() == 'AI_API_KEY=keep-me\n'


def test_doctor_safe_and_offline(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    initialize(path, model='m', base_url='https://api.test', oj_root='https://oj.test')
    monkeypatch.setenv('AI_API_KEY', 'never-display-this-secret')
    report = diagnose(path)
    assert report['ok'] and report['models'][0]['output_limit'] == 16384
    assert 'never-display-this-secret' not in json.dumps(report)


def test_missing_model_env_does_not_fall_back_to_other_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_API_KEY', 'global-secret')
    monkeypatch.delenv('MISSING_PROVIDER_KEY', raising=False)
    path = write(tmp_path / 'config.json', {'ai_model': 'm', 'models': {'m': {'api_key': 'env:MISSING_PROVIDER_KEY'}}})
    cm = ConfigManager(str(path))
    assert cm.get_model_api_key('m') == ''
    assert not diagnose(path)['ok']


@pytest.mark.parametrize('reference', ['env:PROVIDER_KEY', '${PROVIDER_KEY}', 'PROVIDER_KEY'])
def test_secret_references_and_rotation(tmp_path, monkeypatch, reference):
    cm = ConfigManager(str(write(tmp_path / 'c.json', {'models': {'m': {'api_key': reference}}})))
    monkeypatch.setenv('PROVIDER_KEY', 'first')
    assert cm.get_model_api_key('m') == 'first'
    monkeypatch.setenv('PROVIDER_KEY', 'second')
    assert cm.get_model_api_key('m') == 'second'


def test_reload_preserves_cli_and_recovers_after_bad_json(tmp_path):
    path = write(tmp_path / 'c.json', {'ai_model': 'file', 'monitor_interval': 10})
    cm = ConfigManager(str(path), cli_overrides={'ai_model': 'cli'})
    write(path, {'ai_model': 'changed', 'monitor_interval': 20})
    cm.reload()
    assert cm.ai_model == 'cli' and cm.monitor_interval == 20
    path.write_text('{broken', encoding='utf-8')
    assert cm.reload() is False and cm.monitor_interval == 20
    write(path, {'monitor_interval': 30})
    assert cm.reload() is True and cm.monitor_interval == 30 and cm.ai_model == 'cli'


def test_reload_callback_can_read_manager(tmp_path):
    cm = ConfigManager(str(tmp_path / 'none.json'))
    seen = []
    cm.on_change(lambda cfg: seen.append(cm.get_model_config(cfg.ai_model)))
    cm.reload()
    assert seen


def test_legacy_config_and_global_only_routing(tmp_path):
    path = write(tmp_path / 'c.json', {'ai': {'model': 'single', 'base_url': 'https://api.test'},
                                      'oj': {'root': 'https://oj.test'}, 'code': {'lang': 'py.py3'}})
    cm = ConfigManager(str(path))
    assert cm.ai_model == 'single' and cm.lang_ext == 'python'
    assert cm.oj_base == 'https://oj.test/d/system'
    assert ModelRouter(cm).default.model == 'single'


def test_save_preserves_extensions_and_does_not_copy_runtime_secrets(tmp_path):
    path = write(tmp_path / 'c.json', {'extension': {'x': 1}, 'ai_api_key': 'env:MY_KEY'})
    cm = ConfigManager(str(path), cli_overrides={'password': 'runtime-only', 'ai_api_key': 'sk-runtime'})
    cm.save()
    saved = read_config(path)
    assert saved['extension'] == {'x': 1} and saved['ai_api_key'] == 'env:MY_KEY'
    assert 'runtime-only' not in path.read_text() and 'sk-runtime' not in path.read_text()


@pytest.mark.parametrize('data', [
    {'ai_task_tokens': {'solve': -1}}, {'models': {'m': {'stream_usage': 'false'}}},
    {'models': {'m': {'provider': 'oops'}}}, {'models': {'m': {'max_tokens': 2.5}}},
    {'model_router': {'tiers': []}}, {'ai_base_url': 'file:///bad'},
])
def test_invalid_config_diagnostics(data):
    assert validate_document(data)


@pytest.fixture
def web(tmp_path, monkeypatch):
    import webui
    monkeypatch.setattr(webui, 'ROOT', tmp_path)
    monkeypatch.setattr(webui, 'AUTH_TOKEN', 'access')
    return webui.app.test_client(), tmp_path / 'config.json'


def test_web_config_requires_auth_and_redacts_nested_keys(web):
    client, path = web
    write(path, {'password': 'secret-pass', 'ai_api_key': 'sk-secret',
                 'models': {'m': {'api_key': 'sk-model', 'extra_body': {'authorization': 'secret-header'}}}})
    assert client.get('/config').status_code == 403
    response = client.get('/config', headers={'X-Auth-Token': 'access'})
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    for secret in ['secret-pass', 'sk-secret', 'sk-model', 'secret-header']:
        assert secret not in text
    assert response.json['password_configured']
    assert response.json['models']['m']['api_key_configured']


def test_web_flat_and_legacy_writes_reach_runtime(web):
    client, path = web
    headers = {'X-Auth-Token': 'access'}
    write(path, {'ai_api_key': 'env:KEEP_KEY', 'models': {'m': {'max_tokens': 1000}}})
    response = client.post('/config', headers=headers, json={
        'ai': {'model': 'm', 'base_url': 'https://proxy.test'},
        'ai_task_tokens': {'plan': 2048}, 'monitor_interval': 60})
    assert response.status_code == 200
    cm = ConfigManager(str(path))
    assert cm.ai_model == 'm' and cm.ai_base_url == 'https://proxy.test'
    assert cm.task_token_limit('plan') == 2048
    assert read_config(path)['ai_api_key'] == 'env:KEEP_KEY'
    assert 'ai' not in read_config(path)
    before = path.read_bytes()
    assert client.post('/config', headers=headers, json={'monitor_interval': -1}).status_code == 400
    assert path.read_bytes() == before
    assert client.post('/config', headers=headers, json=[]).status_code == 400


def test_doctor_malformed_json_exit_code_and_no_secret_echo(tmp_path, capsys):
    path = tmp_path / 'bad.json'
    path.write_text('{secret-broken-value', encoding='utf-8')
    assert main(['--config', str(path), 'doctor', '--json']) == 1
    output = capsys.readouterr().out
    assert 'secret-broken-value' not in output and json.loads(output)['ok'] is False
