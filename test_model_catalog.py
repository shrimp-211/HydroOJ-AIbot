"""Price/catalog and model connection regressions, with no live API credentials."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import requests

from model_pricing import PriceCatalog, parse_catalog, resolve_pricing
from model_services import discover_models, normalize_base_url, fetch_json, ModelServiceError
from config_manager import ConfigManager
from config_cli import add_model, price_report, diagnose
from config_io import validate_document
from ai_client import AIClient
from ai_protocol import build_request


def catalog_data():
    return {
        'openai': {'models': {'m': {'cost': {'input': 2, 'output': 4, 'cache_read': 0.5}},
                              'free': {'cost': {'input': 0, 'output': 0}},
                              'tiered': {'cost': {'input': 2, 'output': 4, 'tiers': [{'input': 3}]}}}},
        'reseller': {'api': 'https://reseller.test/v1', 'models': {'m': {'cost': {'input': 8, 'output': 12}}}},
    }


def settings(**updates):
    return {'base_url': 'https://api.openai.com/v1', 'api_key': 'sk-test', 'max_tokens': 4096, **updates}


def test_catalog_units_conversion_and_cached_input_billed_once(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ai_model': 'm', 'ai_base_url': 'https://api.openai.com', 'pricing_usd_to_cny': 7.0}))
    cm = ConfigManager(str(path))
    cm._price_catalog = PriceCatalog(tmp_path/'prices.json', fetcher=lambda url: catalog_data())
    assert cm.get_model_pricing('m') == {'input': 14, 'output': 28, 'cache_hit': 3.5}
    assert AIClient(cm)._calc_cost({'input': 1000000, 'cache_hit': 600000, 'output': 100000}, 'm') == 10.5


def test_manual_free_price_wins_without_network_or_exchange_rate(tmp_path):
    def forbidden(*a): raise AssertionError('must remain offline')
    cache = PriceCatalog(tmp_path/'p.json', fetcher=forbidden)
    result = resolve_pricing(cache, settings(pricing={'input': 0, 'output': 0}), 'free', refresh=True)
    assert result['known'] and result['source'] == 'manual' and result['pricing']['input'] == 0


def test_currency_requires_explicit_rate_and_tiers_not_undercounted(tmp_path):
    cache = PriceCatalog(tmp_path/'p.json', fetcher=lambda url: catalog_data())
    result = resolve_pricing(cache, settings(), 'm', refresh=True)
    assert not result['known'] and result['usd_prices']['input'] == 2
    result = resolve_pricing(cache, settings(), 'tiered', usd_to_cny=7)
    assert not result['known'] and '分档' in result['reason']


def test_proxy_needs_billing_provider_and_exact_model_alias(tmp_path):
    cache = PriceCatalog(tmp_path/'p.json', fetcher=lambda url: catalog_data())
    cfg = settings(base_url='https://proxy.test/v1', provider='openai')
    assert not resolve_pricing(cache, cfg, 'm', usd_to_cny=7, refresh=True)['known']
    cfg.update(pricing_provider='openai', pricing_model='m')
    result = resolve_pricing(cache, cfg, 'friendly-alias', usd_to_cny=7)
    assert result['known'] and result['catalog_model'] == 'openai/m'
    cfg['pricing_model'] = 'm-unknown-suffix'
    assert not resolve_pricing(cache, cfg, 'alias', usd_to_cny=7)['known']


def test_refresh_ttl_stale_fallback_and_failure_backoff(tmp_path):
    now, calls = [10000], []
    def fetch(url):
        calls.append(url)
        if len(calls) > 1: raise ModelServiceError('failure')
        return catalog_data()
    cache = PriceCatalog(tmp_path/'p.json', fetcher=fetch, clock=lambda:now[0])
    first = cache.snapshot(refresh=True, ttl=100)
    cache.snapshot(refresh=True, ttl=100)
    assert len(calls) == 1
    before = (tmp_path/'p.json').read_bytes()
    now[0] += 101
    failed = cache.snapshot(refresh=True, ttl=100)
    assert failed['stale'] and failed['error'] and failed['entries'] == first['entries']
    cache.snapshot(refresh=True, ttl=100)
    assert len(calls) == 2 and (tmp_path/'p.json').read_bytes() == before
    other = PriceCatalog(tmp_path/'p.json', clock=lambda:now[0])
    assert other.snapshot()['entries'] == first['entries']


def test_invalid_refresh_and_corrupt_cache_preserve_valid_memory(tmp_path):
    payload = [catalog_data()]
    cache = PriceCatalog(tmp_path/'p.json', fetcher=lambda url:payload[0])
    first = cache.snapshot(force=True)
    payload[0] = {'invalid': {}}
    assert cache.snapshot(force=True)['entries'] == first['entries']
    (tmp_path/'p.json').write_text('{broken')
    assert cache.snapshot()['entries'] == first['entries']


@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), True, '2'])
def test_invalid_catalog_prices_not_imported(value):
    with pytest.raises(ValueError):
        parse_catalog({'p': {'models': {'m': {'cost': {'input': value, 'output': 2}}}}})


@pytest.mark.parametrize('url,expected', [
    (' https://api.openai.com/ ', 'https://api.openai.com/v1'),
    ('https://gateway.test/custom/v1/chat/completions/', 'https://gateway.test/custom/v1'),
    ('https://gateway.test/custom/models', 'https://gateway.test/custom'),
    ('https://open.bigmodel.cn/api/paas/v4', 'https://open.bigmodel.cn/api/paas/v4'),
])
def test_normalized_connection_urls(url, expected):
    assert normalize_base_url(url) == expected


@pytest.mark.parametrize('url', ['https://key@host/v1', 'https://host?key=secret', 'file:///x', 'https://host/v1/messages'])
def test_unsupported_or_credential_urls_rejected(url):
    with pytest.raises(ValueError): normalize_base_url(url)


def test_discovery_404_root_fallback_uses_same_credentials():
    calls = []
    def fetch(url, **kw):
        calls.append((url, kw['headers']))
        if len(calls) == 1: raise ModelServiceError('missing', 404)
        return {'data': [{'id':'b'}, {'id':'a'}, {'id':'a'}, {'bad':'x'}]}
    result = discover_models(settings(base_url='https://proxy.test'), fetcher=fetch)
    assert [call[0] for call in calls] == ['https://proxy.test/models', 'https://proxy.test/v1/models']
    assert all(call[1]['Authorization'] == 'Bearer sk-test' for call in calls)
    assert result['base_url'] == 'https://proxy.test/v1'
    assert [m['id'] for m in result['models']] == ['a','b']


def test_auth_failure_does_not_probe_more_endpoints():
    calls = []
    def fetch(url, **kw): calls.append(url); raise ModelServiceError('denied', 401)
    with pytest.raises(ModelServiceError): discover_models(settings(base_url='https://proxy.test'), fetcher=fetch)
    assert len(calls) == 1


def test_discovery_explicit_url_and_alternative_response():
    calls = []
    def fetch(url, **kw): calls.append(url); return {'models':['m', {'name':'n'}, None]}
    result = discover_models(settings(models_url='https://api.openai.com/custom/models'), fetcher=fetch)
    assert calls == ['https://api.openai.com/custom/models']
    assert [m['id'] for m in result['models']] == ['m','n']
    with pytest.raises(ValueError):
        discover_models(settings(models_url='https://other.test/models'), fetcher=fetch)
    assert len(calls) == 1


def test_fetch_disallows_redirects_and_hides_response_secrets(monkeypatch):
    calls = []
    class Response:
        status_code = 302
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def get(url, **kwargs): calls.append(kwargs); return Response()
    monkeypatch.setattr(requests, 'get', get)
    with pytest.raises(ModelServiceError, match='HTTP 302'):
        fetch_json('https://api.test/models', headers={'Authorization':'Bearer private'})
    assert calls[0]['allow_redirects'] is False
    def fail(*a, **kw): raise requests.ConnectionError('private-key-in-error')
    monkeypatch.setattr(requests, 'get', fail)
    with pytest.raises(ModelServiceError) as exc: fetch_json('https://api.test')
    assert 'private-key' not in str(exc.value)


def test_profile_add_and_alias_used_on_wire(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_PROVIDER', 'test-secret')
    path = tmp_path/'config.json'
    add_model(path, name='fast', model_id='real-id', base_url='https://proxy.test/v1/chat/completions',
              api_key_env='MY_PROVIDER', set_default=True)
    cm = ConfigManager(str(path))
    assert cm.ai_model == 'fast' and cm.get_tier_model('flash') == 'fast'
    cfg = cm.get_model_config('fast')
    assert cfg['api_key'] == 'test-secret' and cfg['base_url'] == 'https://proxy.test/v1'
    assert build_request('fast', [], cfg)['model'] == 'real-id'
    assert 'test-secret' not in path.read_text()
    with pytest.raises(ValueError):
        add_model(path, name='fast', model_id='other', base_url='https://x.test')


def test_doctor_and_prices_view_are_offline(tmp_path, monkeypatch):
    path = tmp_path/'config.json'
    path.write_text(json.dumps({'ai_model':'m','ai_api_key':'sk-test','pricing_usd_to_cny':7.0}))
    def forbidden(*a, **kw): raise AssertionError('unexpected network')
    monkeypatch.setattr(requests, 'get', forbidden)
    report = diagnose(path)
    assert report['ok'] and not report['models'][0]['pricing']['known']
    assert price_report(ConfigManager(str(path)))['count'] == 0


@pytest.mark.parametrize('patch', [
    {'pricing_usd_to_cny': -1}, {'pricing_usd_to_cny': float('nan')},
    {'pricing_refresh_hours': 0}, {'models':{'m':{'pricing':{'input':-1}}}},
    {'models':{'m':{'pricing':{'currency':'USD'}}}},
    {'models':{'m':{'model_id':3}}},
    {'models':{'m':{'models_url':'https://another.test/models'}}},
])
def test_invalid_pricing_and_connection_config_rejected(patch):
    assert validate_document(patch)


def test_web_catalog_auth_and_profile_round_trip(tmp_path, monkeypatch):
    import webui, model_services
    monkeypatch.setattr(webui, 'ROOT', tmp_path)
    monkeypatch.setattr(webui, 'AUTH_TOKEN', 'access')
    monkeypatch.delenv('OJ_DATA_DIR', raising=False)
    monkeypatch.setenv('AI_API_KEY', 'secret')
    client = webui.app.test_client()
    for url in ('/models/discover','/models/add','/pricing/refresh'):
        assert client.post(url, json={}).status_code == 403
    headers = {'X-Auth-Token':'access'}
    response = client.post('/models/add', headers=headers, json={'name':'alias', 'model_id':'wire',
        'base_url':'https://test.invalid/v1', 'set_default':True})
    assert response.status_code == 200
    monkeypatch.setattr(model_services, 'fetch_json', lambda *a, **kw:{'data':[{'id':'wire'}]})
    response = client.post('/models/discover', headers=headers, json={'model':'alias'})
    assert response.json['models'] == [{'id':'wire','owned_by':''}]
    assert 'secret' not in response.get_data(as_text=True)
    assert client.get('/pricing', headers=headers).status_code == 200

@pytest.mark.parametrize('stream', [False, True])
def test_alias_keeps_its_connection_and_stream_settings(tmp_path, monkeypatch, stream):
    import ai_client
    from openai import OpenAI
    from test_ai_client import httpx
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), request.headers['authorization']))
        assert body['model'] == 'shared-wire-id'
        assert 'stream_options' not in body
        usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}
        if stream:
            chunks = [{'id':'x', 'choices':[{'index':0,'delta':{'content':'answer'},'finish_reason':'stop'}]},
                      {'id':'x','choices':[],'usage':usage}]
            data = ''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n'
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, text=data)
        return httpx.Response(200, json={'id':'x','choices':[{'index':0,'finish_reason':'stop',
                                   'message':{'role':'assistant','content':'answer'}}], 'usage':usage})
    def factory(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(ai_client, 'OpenAI', factory)
    cm = ConfigManager(str(tmp_path/'none.json'), cli_overrides={
        'ai_model':'first', 'ai_api_key':'sk-wrong-global', 'models':{
            name: {'model_id':'shared-wire-id', 'base_url':f'https://{name}.test/v1',
                   'api_key':f'sk-{name}', 'stream_usage':False, 'pricing':{'input':1,'output':2}}
            for name in ('first','second')}})
    ai = AIClient(cm)
    try:
        for name in ('first','second'):
            result = ai.chat([{'role':'user','content':'question'}], model=name, use_stream=stream)
            assert result['content'] == 'answer' and result['model'] == name and result['cost_known']
    finally:
        ai.close()
    assert seen == [(f'https://{name}.test/v1/chat/completions', f'Bearer sk-{name}') for name in ('first','second')]


def test_old_context_tier_not_mistaken_for_flat_pricing(tmp_path):
    payload = {'openai':{'models':{'m':{'cost':{'input':1, 'output':2,
                                            'context_over_200k':{'input':3,'output':4}}}}}}
    cache = PriceCatalog(tmp_path/'p.json', fetcher=lambda url:payload)
    result = resolve_pricing(cache, settings(), 'm', usd_to_cny=7, refresh=True)
    assert not result['known'] and '分档' in result['reason']


def test_missing_usage_is_not_reported_as_known_zero(tmp_path, monkeypatch):
    cm = ConfigManager(str(tmp_path/'none.json'), cli_overrides={
        'ai_model':'m', 'models':{'m':{'pricing':{'input':1,'output':2}}}})
    ai = AIClient(cm)
    monkeypatch.setattr(ai, '_get_client', lambda *args: object())
    monkeypatch.setattr(ai, '_block_call', lambda *a, **kw:('```cpp\nint main(){}\n```', '', None))
    result = ai._call_ai('question', 'system')
    assert result['cost_known'] is False and result['code']


def test_malformed_optional_catalog_api_does_not_break_lookup(tmp_path):
    payload = catalog_data()
    payload['openai']['api'] = {'invalid': 'not-a-url'}
    cache = PriceCatalog(tmp_path/'p.json', fetcher=lambda url:payload)
    result = resolve_pricing(cache, settings(), 'm', usd_to_cny=7, refresh=True)
    assert result['known'] and result['pricing']['input'] == 14


def test_add_model_rejects_invalid_table_without_rewriting_file(tmp_path):
    path = tmp_path/'config.json'
    path.write_text('{"models": []}')
    with pytest.raises(ValueError, match='models'):
        add_model(path, name='m', model_id='wire', base_url='https://test.invalid/v1')
    assert path.read_text() == '{"models": []}'
