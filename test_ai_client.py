"""Offline protocol tests: real SDK serialization, streaming, billing and failure handling."""
from types import SimpleNamespace as NS
import json

try:
    import httpx2 as httpx  # OpenAI SDK 3.x
except ImportError:
    import httpx  # OpenAI SDK 1.x / 2.x
import pytest
from openai import OpenAI
from ai_client import AIClient
from ai_protocol import build_request, extract_usage
from config_manager import ConfigManager


def config(tmp_path, **model):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'ai_model': 'm', 'models': {'m': {
        'api_key': 'sk-test', 'base_url': 'https://example.test/v1',
        'max_tokens': 8000, **model}}}), encoding='utf-8')
    return ConfigManager(str(path))


def test_provider_options_and_token_limit():
    cfg = dict(base_url='https://api.openai.com/v1', max_tokens=1000, reasoning_effort='low')
    args = build_request('m', [], cfg, max_tokens=2000)
    assert args['max_completion_tokens'] == 1000
    assert args['reasoning_effort'] == 'low'
    assert 'max_tokens' not in args and 'extra_body' not in args
    cfg['base_url'] = 'https://deepseek.example.test/v1'
    args = build_request('m', [], cfg)
    assert 'reasoning_effort' not in args and 'extra_body' not in args
    assert args['max_tokens'] == 1000


def test_explicit_proxy_capabilities():
    cfg = dict(base_url='https://proxy.test', max_tokens=3000, provider='openai',
               supports_reasoning_effort=False, reasoning_effort='high', extra_body={'foo': 1})
    args = build_request('m', [], cfg, max_tokens=500)
    assert args['max_completion_tokens'] == 500
    assert 'reasoning_effort' not in args
    assert args['extra_body'] == {'foo': 1}
    cfg['extra_body']['max_tokens'] = 99999
    with pytest.raises(ValueError):
        build_request('m', [], cfg)


@pytest.mark.parametrize('usage', [
    NS(prompt_tokens=100, completion_tokens=20, total_tokens=120,
       prompt_tokens_details=NS(cached_tokens=60)),
    {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_cache_hit_tokens': 60},
])
def test_usage_formats(usage):
    assert extract_usage(usage) == {'input': 100, 'output': 20, 'total': 120, 'cache_hit': 60}


def test_cache_creation_not_cache_hit():
    assert extract_usage({'input_tokens': 100, 'cache_creation_input_tokens': 90})['cache_hit'] == 0


def test_cached_input_billed_once(tmp_path):
    ai = AIClient(config(tmp_path, pricing={'input': 2, 'output': 4, 'cache_hit': 0.5}))
    assert ai._calc_cost({'input': 1000000, 'cache_hit': 600000, 'output': 100000}, 'm') == 1.5
    ai.config.set_override(models={'m': {'pricing': {'input': 2}}})
    assert ai._calc_cost({'input': 1000000, 'cache_hit': 600000}, 'm') == 2


def sdk_client(ai, handler):
    client = OpenAI(api_key='sk-test', base_url='https://example.test/v1', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    ai._get_client = lambda model='': client
    return client


def test_stream_usage_only_chunk_and_close(tmp_path, capsys):
    ai = AIClient(config(tmp_path))
    def handler(request):
        body = json.loads(request.content)
        assert body['stream_options'] == {'include_usage': True}
        chunks = [
            {'id': 'x', 'choices': [{'index': 0, 'delta': {'content': 'hello'}, 'finish_reason': None}]},
            {'id': 'x', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]},
            {'id': 'x', 'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}},
        ]
        data = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=data)
    with sdk_client(ai, handler):
        result = ai.chat([{'role': 'user', 'content': 'hi'}], use_stream=True)
    assert result['content'] == 'hello'
    assert result['usage']['total'] == 12


@pytest.mark.parametrize('stream', [False, True])
def test_rate_limit_marker_shared_by_generation_and_chat(tmp_path, stream):
    ai = AIClient(config(tmp_path))
    with sdk_client(ai, lambda request: httpx.Response(429, json={'error': {'message': 'limited'}})):
        r = ai.chat([{'role': 'user', 'content': 'hi'}], use_stream=stream)
        assert r['_rate_limited'] and not r['content']
        assert ai._call_ai('hi', 'system', use_stream=stream)['_rate_limited']


def test_truncated_completion_accounted_but_not_submitted(tmp_path):
    ai = AIClient(config(tmp_path))
    body = {'id': 'x', 'choices': [{'index': 0, 'finish_reason': 'length',
            'message': {'role': 'assistant', 'content': '```cpp\nint main(){'}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
    with sdk_client(ai, lambda request: httpx.Response(200, json=body)):
        r = ai._call_ai('problem', 'system')
    assert r['code'] == '' and r['usage']['total'] == 30


def test_image_messages_not_mutated_or_duplicated(tmp_path, monkeypatch):
    ai = AIClient(config(tmp_path, vision=True))
    messages = [{'role': 'user', 'content': [{'type': 'text', 'text': 'x'},
                {'type': 'image_url', 'image_url': {'url': 'https://x/image.png'}}]}]
    original = json.dumps(messages)
    sent = []
    monkeypatch.setattr(ai, 'chat', lambda msgs, **kw: sent.extend(msgs))
    ai._call_ai_with_messages(messages, images=['https://x/image.png', 'https://x/2.png'])
    assert json.dumps(messages) == original
    assert len(sent[0]['content']) == 3


def test_model_options_and_cached_config_are_isolated(tmp_path):
    cm = config(tmp_path, provider='compatible', stream_usage=False)
    resolved = cm.get_model_config('m')
    assert resolved['stream_usage'] is False
    resolved['pricing']['input'] = 99
    assert cm.get_model_config('m')['pricing'] == {}
