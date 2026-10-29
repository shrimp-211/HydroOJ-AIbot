"""Connection normalization and read-only model discovery for chat-compatible APIs."""
from urllib.parse import urlsplit, urlunsplit
import requests


class ModelServiceError(ValueError):
    """A sanitized remote-service failure; never includes keys or response bodies."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalize_base_url(value):
    """Accept a base URL or a pasted chat/model-list endpoint; preserve custom prefixes."""
    try:
        parsed = urlsplit(str(value).strip())
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ValueError('API 地址必须是 http(s) URL，不能包含凭据、查询参数或片段') from None
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if path.endswith(('/responses', '/messages', '/generateContent')):
        raise ValueError('此入口需要 Chat Completions 兼容 API，不支持该原生协议端点')
    if not path and parsed.hostname == 'api.openai.com':
        path = '/v1'
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def fetch_json(url, *, headers=None, max_bytes=32 * 1024 * 1024):
    """Bound downloads and do not forward credentials through redirects."""
    try:
        with requests.get(url, headers=headers or {'Accept': 'application/json'},
                          timeout=(5, 15), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                status = response.status_code
                detail = {401: '认证失败，请检查 API Key', 403: '没有模型列表访问权限',
                          404: '接口不存在', 405: '接口不支持 GET', 429: '请求限流，请稍后重试'}.get(status, '远端请求失败')
                raise ModelServiceError(f'{detail} (HTTP {status})', status)
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise ModelServiceError('响应过大，已停止读取')
                chunks.append(chunk)
            import json
            return json.loads(b''.join(chunks))
    except requests.Timeout:
        raise ModelServiceError('连接超时，请检查地址或代理设置') from None
    except requests.RequestException:
        raise ModelServiceError('连接失败，请检查网络、证书和代理设置') from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ModelServiceError):
            raise
        raise ModelServiceError('远端没有返回有效 JSON') from None


def models_urls(base_url, explicit_url=''):
    base = normalize_base_url(base_url)
    parsed = urlsplit(base)
    if explicit_url:
        target = urlsplit(explicit_url)
        if (target.scheme, target.netloc) != (parsed.scheme, parsed.netloc) or target.query or target.fragment or target.username:
            raise ValueError('models_url 必须与 API 地址同源，且不能包含查询参数或凭据')
        return [(explicit_url, base)]
    urls = [(base + '/models', base)]
    # Only a missing root endpoint triggers /v1 fallback; custom version prefixes stay intact.
    if not parsed.path:
        urls.append((base + '/v1/models', base + '/v1'))
    return urls


def discover_models(settings, *, fetcher=None):
    key = settings.get('api_key', '')
    if not key:
        raise ValueError('API Key 未配置或引用的环境变量不存在')
    fetcher = fetcher or fetch_json
    candidates = models_urls(settings['base_url'], settings.get('models_url', ''))
    for index, (url, base) in enumerate(candidates):
        try:
            payload = fetcher(url, headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
        except ModelServiceError as exc:
            if exc.status in (404, 405) and index + 1 < len(candidates):
                continue
            raise
        rows = payload if isinstance(payload, list) else payload.get('data', payload.get('models')) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ModelServiceError('模型列表格式不受支持：需要 data/models 数组')
        found = {}
        for row in rows:
            name = row if isinstance(row, str) else row.get('id') or row.get('name') if isinstance(row, dict) else None
            if not isinstance(name, str) or not name.strip() or len(name) > 256 or any(ord(c) < 32 for c in name):
                continue
            owner = row.get('owned_by', '') if isinstance(row, dict) else ''
            found[name] = {'id': name, 'owned_by': str(owner)[:128]}
        return {'base_url': base, 'models': sorted(found.values(), key=lambda row: row['id'])}
    raise ModelServiceError('没有可用的模型列表接口')
