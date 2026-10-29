"""models.dev price cache. Source rates are USD per million tokens, never per token."""
from copy import deepcopy
import math
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from config_io import atomic_write_json
from model_services import fetch_json, ModelServiceError, normalize_base_url

SOURCE_URL = 'https://models.dev/api.json'
CACHE_VERSION = 1


def valid_rate(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def parse_catalog(payload):
    if not isinstance(payload, dict):
        raise ValueError('定价目录格式不受支持')
    entries = {}
    for provider, info in payload.items():
        if not isinstance(info, dict) or not isinstance(info.get('models'), dict):
            continue
        for model_id, model in info['models'].items():
            if not isinstance(model, dict):
                continue
            cost = model.get('cost', {})
            if not isinstance(cost, dict) or not all(valid_rate(cost.get(k)) for k in ('input', 'output')):
                continue
            prices = {'input': cost['input'], 'output': cost['output']}
            if valid_rate(cost.get('cache_read')):
                prices['cache_hit'] = cost['cache_read']
            entries[f'{provider}/{model_id}'] = {
                'provider': provider, 'model': model_id,
                'api': info.get('api') if isinstance(info.get('api'), str) else '',
                'prices': prices,
                'tiered': bool(cost.get('tiers')) or any('above' in key or key.startswith('context_over_') for key in cost),
            }
    if not entries:
        raise ValueError('定价目录没有有效的输入/输出价格')
    return entries


class PriceCatalog:
    def __init__(self, path, *, fetcher=None, clock=None):
        self.path = Path(path)
        self.fetcher = fetcher or fetch_json
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self._last_attempt = None
        self._entries = {}
        self._fetched_at = 0
        self._error = ''
        self._mtime = None

    def _read_cache(self):
        try:
            mtime = self.path.stat().st_mtime_ns
            if mtime == self._mtime:
                return
            import json
            obj = json.loads(self.path.read_text(encoding='utf-8'))
            entries = obj.get('entries')
            stamp = obj.get('fetched_at')
            if (obj.get('version') != CACHE_VERSION or not isinstance(entries, dict)
                    or not entries or not valid_rate(stamp) or stamp > self.clock() + 300):
                return
            for entry in entries.values():
                if (not isinstance(entry, dict) or not isinstance(entry.get('prices'), dict)
                        or not all(valid_rate(entry['prices'].get(k)) for k in ('input', 'output'))
                        or ('cache_hit' in entry['prices'] and not valid_rate(entry['prices']['cache_hit']))
                        or not isinstance(entry.get('provider'), str) or not isinstance(entry.get('model'), str)
                        or not isinstance(entry.get('api', ''), str)):
                    return
            self._entries, self._fetched_at, self._mtime = entries, stamp, mtime
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # Keep the last valid in-memory copy if the cache is damaged.

    def snapshot(self, *, refresh=False, force=False, ttl=21600):
        with self._lock:
            self._read_cache()
            now = self.clock()
            stale = not self._entries or now - self._fetched_at >= ttl
            retry_ready = self._last_attempt is None or now - self._last_attempt >= 60
            if force or (refresh and stale and retry_ready):
                self._last_attempt = now
                try:
                    entries = parse_catalog(self.fetcher(SOURCE_URL))
                    self._entries, self._fetched_at, self._error = entries, now, ''
                    try:
                        atomic_write_json(self.path, {'version': CACHE_VERSION, 'fetched_at': now, 'entries': entries})
                        self._mtime = self.path.stat().st_mtime_ns
                    except OSError:
                        self._error = '价格已获取，但缓存无法写入；本进程仍可使用'
                except (ModelServiceError, OSError, ValueError, TypeError):
                    self._error = '价格目录刷新失败，继续使用最后有效缓存（如有）'
            return {'source': SOURCE_URL, 'fetched_at': self._fetched_at,
                    'stale': not self._entries or self.clock() - self._fetched_at >= ttl,
                    'error': self._error, 'entries': deepcopy(self._entries)}

    def lookup(self, model_id, *, provider='', base_url='', refresh=False, ttl=21600):
        data = self.snapshot(refresh=refresh, ttl=ttl)
        if not provider:
            try:
                host = urlsplit(normalize_base_url(base_url)).hostname
                official = {'api.openai.com': 'openai', 'api.deepseek.com': 'deepseek',
                            'open.bigmodel.cn': 'zhipuai', 'api.z.ai': 'zai'}
                matches = {entry['provider'] for entry in data['entries'].values()
                           if entry.get('api') and urlsplit(entry['api']).hostname == host}
                # A protocol setting such as provider=openai is NOT a billing provider.
                provider = official.get(host) or (next(iter(matches)) if len(matches) == 1 else '')
            except (ValueError, TypeError):
                provider = ''
        entry = data['entries'].get(f'{provider}/{model_id}') if provider else None
        return entry, {key: value for key, value in data.items() if key != 'entries'}


def resolve_pricing(catalog, settings, model, *, usd_to_cny=0, refresh=False, ttl=21600):
    manual = settings.get('pricing')
    if manual:
        return {'known': True, 'source': 'manual', 'currency': 'CNY', 'pricing': deepcopy(manual), 'stale': False}
    entry, meta = catalog.lookup(settings.get('pricing_model') or settings.get('model_id') or model,
                                 provider=settings.get('pricing_provider', ''), base_url=settings['base_url'],
                                 refresh=refresh, ttl=ttl)
    result = {'known': False, 'currency': 'CNY', 'pricing': {}, **meta}
    if not entry:
        result['reason'] = '未找到供应商与模型的精确价格；可设置 pricing_provider / pricing_model 或手工 pricing'
        return result
    result.update({'catalog_model': f"{entry['provider']}/{entry['model']}", 'usd_prices': entry['prices']})
    if not valid_rate(usd_to_cny) or usd_to_cny <= 0:
        result['reason'] = '美元价格未换算：请设置 pricing_usd_to_cny（1 USD 对应的人民币金额）'
        return result
    if entry.get('tiered'):
        result['reason'] = '目录包含分档价格，请手工配置适用价格，避免低估费用'
        return result
    result.update({'known': True, 'usd_to_cny': usd_to_cny,
                   'pricing': {key: value * usd_to_cny for key, value in entry['prices'].items()}})
    return result
