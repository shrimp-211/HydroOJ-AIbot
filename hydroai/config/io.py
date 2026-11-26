"""CLI、Web 和运行时共用的配置读写、脱敏与校验。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from hydroai.config.paths import config_file

LEGACY_SECTIONS = {
    'oj': {'root': 'oj_root', 'base': 'oj_base', 'username': 'username', 'password': 'password'},
    'ai': {'base_url': 'ai_base_url', 'model': 'ai_model', 'ai_api_key': 'ai_api_key',
           'api_key': 'ai_api_key', 'max_tokens': 'ai_max_tokens', 'reasoning_effort': 'ai_reasoning_effort'},
    'code': {'lang': 'code_lang'},
    'monitor': {'domains': 'monitor_domains', 'interval': 'monitor_interval',
                'state_file': 'monitor_state_file', 'start_page': 'contest_start_page'},
}
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
SECRET_WORDS = ('password', 'api_key', 'secret', 'authorization', 'access_token')
POSITIVE_INTEGERS = (
    'ai_max_tokens', 'verify_timeout', 'monitor_interval', 'contest_start_page',
    'pricing_refresh_hours', 'agent_max_steps', 'agent_max_submissions', 'agent_max_seconds',
    'agent_run_timeout', 'solve_candidates', 'usage_retention_days', 'ai_health_check_timeout',
)


def normalize_config(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError('配置顶层必须是 JSON 对象')
    result = deepcopy(data)
    for section, mapping in LEGACY_SECTIONS.items():
        old = result.pop(section, None)
        if isinstance(old, dict):
            for key, target in mapping.items():
                if key in old and target not in result:
                    result[target] = old[key]
    return result


def read_config(path) -> dict:
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}
    return normalize_config(json.loads(text))


def atomic_write_bytes(path, content: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def atomic_write_json(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    atomic_write_bytes(path, text.encode('utf-8'))


def check_document(data: dict):
    errors = validate_document(data)
    if errors:
        raise ValueError('; '.join(errors))


def env_reference(value) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith('env:'):
        return value[4:]
    if value.startswith('${') and value.endswith('}'):
        return value[2:-1]
    if re.fullmatch(r'[A-Z_][A-Z0-9_]*', value):
        return value
    return None


def dotenv_entry(line: str) -> tuple[str, str] | None:
    line = line.strip().lstrip('﻿')
    if not line or line.startswith('#'):
        return None
    if line.startswith('export '):
        line = line[len('export '):]
    key, sep, value = line.partition('=')
    key, value = key.strip(), value.strip()
    if not sep or not ENV_NAME.fullmatch(key) or '\0' in value:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return key, value[1:-1]
    return key, value.split('#', 1)[0].strip().strip('"').strip("'")


def load_dotenv(env_path=None):
    """加载配置同目录的 .env；兼容旧目录，不覆盖进程已有变量。"""
    if env_path is None:
        sibling = config_file('config.json').with_name('.env')
        env_path = sibling if sibling.exists() else config_file('.env')
    try:
        text = Path(env_path).read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return
    for line in text.splitlines():
        entry = dotenv_entry(line)
        if entry:
            os.environ.setdefault(*entry)


def redact_config(data):
    """隐藏凭据，包括可能含有认证信息的代理地址。"""
    if isinstance(data, list):
        return [redact_config(item) for item in data]
    if not isinstance(data, dict):
        return data
    result = {}
    for key, value in data.items():
        low = key.lower()
        if low == 'proxy' or any(word in low for word in SECRET_WORDS):
            result[key + '_configured'] = bool(value)
        else:
            result[key] = redact_config(value)
    return result


def strip_redacted(data):
    """忽略用于展示的标记；补丁未提供密钥时保留原值。"""
    if isinstance(data, dict):
        return {key: strip_redacted(value) for key, value in data.items()
                if not key.endswith('_configured')}
    if isinstance(data, list):
        return [strip_redacted(item) for item in data]
    return data


def merge_config(current, patch):
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_document(data: dict) -> list[str]:
    """离线校验文档，不在错误中包含配置值。"""
    from hydroai.config.manager import AppConfig, JSON_TO_FIELD

    if not isinstance(data, dict):
        return ['配置顶层必须是 JSON 对象']
    defaults = AppConfig()
    errors = []
    for key, value in data.items():
        attr = JSON_TO_FIELD.get(key)
        if attr:
            default_type = type(getattr(defaults, attr))
            if type(value) is not default_type and not (default_type is float and type(value) is int):
                errors.append(f'{key}: 应为 {default_type.__name__}')
    errors.extend(_validate_options(data))
    errors.extend(_validate_quotas(data.get('quotas', {})))
    errors.extend(_validate_models(data.get('models', {}), data.get('ai_base_url', defaults.ai_base_url)))
    router = data.get('model_router', {})
    if isinstance(router, dict):
        for key in ('tiers', 'thinking_levels'):
            entries = router.get(key, {})
            if not isinstance(entries, dict) or any(not isinstance(v, str) for v in entries.values()):
                errors.append(f'model_router.{key}: 必须是字符串映射')
    return errors


def _validate_options(data):
    from hydroai.ai.pricing import valid_rate
    from hydroai.ai.services import normalize_base_url

    for key in ('oj_root', 'oj_base'):
        if key in data and not _valid_url(data[key]):
            yield f'{key}: 需要 http(s) URL'
    if 'ai_base_url' in data:
        try:
            normalize_base_url(data['ai_base_url'])
        except ValueError as exc:
            yield str(exc)
    for key in POSITIVE_INTEGERS:
        if key in data and (type(data[key]) is not int or data[key] <= 0):
            yield f'{key}: 必须是正整数'
    if 'ai_health_check_ttl' in data and (type(data['ai_health_check_ttl']) is not int
                                          or data['ai_health_check_ttl'] < 0):
        yield 'ai_health_check_ttl: 必须是非负整数'
    for key in ('pricing_usd_to_cny', 'max_cost_per_problem'):
        if key in data and not valid_rate(data[key]):
            yield f'{key}: 必须是非负有限数值'
    budgets = data.get('ai_task_tokens', {})
    if isinstance(budgets, dict):
        for task, value in budgets.items():
            if type(value) is not int or value <= 0:
                yield f'ai_task_tokens.{task}: 必须是正整数'


def _validate_quotas(quotas):
    if not isinstance(quotas, dict):
        return  # 顶层类型已校验。
    for qid, quota in quotas.items():
        if qid.startswith('_') or quota is None:
            continue
        prefix = f'quotas.{qid}'
        if not isinstance(quota, dict):
            yield f'{prefix}: 必须是对象'
        else:
            yield from _validate_quota(prefix, quota)


def _validate_quota(prefix, quota):
    from hydroai.ai.pricing import valid_rate

    kind, source = quota.get('kind', quota.get('type')), quota.get('source')
    sources = ('manual', 'deepseek', 'stepfun', 'siliconflow', 'openrouter', 'novita',
               'mimo', 'zhipu', 'newapi', 'sub2api', 'generic', 'kimi', 'zhipu_plan',
               'minimax', 'opencode_go', 'ollama', 'mimo_plan')
    if kind not in (None, 'balance', 'window', 'plan'):
        yield f'{prefix}.kind: 必须是 balance、window 或 plan'
    if source is not None and source not in sources:
        yield f'{prefix}.source: 不支持的额度来源'
    if kind == 'plan' and not source:
        yield f'{prefix}.source: plan 额度必须指定来源'
    if source in ('newapi', 'sub2api') and not (quota.get('base_url') or quota.get('endpoint')):
        yield f'{prefix}.base_url: {source} 查询需要站点地址'
    if quota.get('unit') not in (None, 'cost', 'tokens', 'requests', 'currency', 'percent'):
        yield f'{prefix}.unit: 必须是 cost、tokens、requests、currency 或 percent'
    if quota.get('period') not in (None, '5h', 'daily', 'weekly', 'monthly', 'rolling'):
        yield f'{prefix}.period: 不支持的统计周期'
    for key in ('limit', 'window_hours', 'low_threshold', 'low_ratio', 'balance', 'cache_seconds', 'timeout'):
        value = quota.get(key)
        if value is not None and not valid_rate(value):
            yield f'{prefix}.{key}: 必须是非负有限数值'
    models = quota.get('models')
    if models is not None and (not isinstance(models, list) or any(not isinstance(m, str) for m in models)):
        yield f'{prefix}.models: 必须是字符串数组'
    for key in ('api_key', 'endpoint', 'json_path', 'used_json_path', 'total_json_path', 'base_url', 'currency'):
        if quota.get(key) is not None and not isinstance(quota[key], str):
            yield f'{prefix}.{key}: 必须是字符串'
    if 'headers' in quota and not isinstance(quota['headers'], dict):
        yield f'{prefix}.headers: 必须是对象'


def _validate_models(models, default_url):
    if not isinstance(models, dict):
        return
    for name, model in models.items():
        if name.startswith('_'):
            continue
        prefix = f'models.{name}'
        if not isinstance(model, dict):
            yield f'{prefix}: 必须是对象'
        else:
            yield from _validate_model(prefix, model, default_url)


def _validate_model(prefix, model, default_url):
    from hydroai.ai.pricing import valid_rate
    from hydroai.ai.services import models_urls, normalize_proxy

    if model.get('provider', 'auto') not in ('auto', 'openai', 'deepseek', 'zhipu', 'compatible'):
        yield f'{prefix}.provider: 不支持的供应商'
    if model.get('token_parameter') not in (None, '', 'max_tokens', 'max_completion_tokens'):
        yield f'{prefix}.token_parameter: 不支持的参数名'
    for key in ('max_tokens', 'timeout', 'max_retries'):
        value = model.get(key)
        if value is None:
            continue
        if type(value) not in (int, float) or not valid_rate(value) or (key != 'max_retries' and value < 1):
            yield f'{prefix}.{key}: 无效数值'
        elif key != 'timeout' and int(value) != value:
            yield f'{prefix}.{key}: 必须是整数'
    for key in ('stream_usage', 'supports_reasoning_effort', 'vision'):
        if key in model and type(model[key]) is not bool:
            yield f'{prefix}.{key}: 必须是 bool'
    if model.get('thinking') not in (None, 'enabled', 'disabled'):
        yield f'{prefix}.thinking: 必须是 enabled 或 disabled'
    if model.get('api_key') is not None and not isinstance(model['api_key'], str):
        yield f'{prefix}.api_key: 必须是字符串或 null'
    for key in ('model_id', 'models_url', 'pricing_provider', 'pricing_model', 'proxy'):
        if key in model and not isinstance(model[key], str):
            yield f'{prefix}.{key}: 必须是字符串'
    try:
        normalize_proxy(model.get('proxy', ''))
        models_urls(model.get('base_url') or default_url, model.get('models_url', ''))
    except ValueError as exc:
        yield f'{prefix}: {exc}'
    yield from _validate_pricing(prefix, model.get('pricing', {}))
    extra = model.get('extra_body', {})
    if not isinstance(extra, dict) or extra.keys() & {'model', 'messages', 'stream', 'max_tokens', 'max_completion_tokens'}:
        yield f'{prefix}.extra_body: 必须是对象且不能覆盖路由/输出上限'


def _validate_pricing(prefix, pricing):
    from hydroai.ai.pricing import valid_rate

    if not isinstance(pricing, dict):
        yield f'{prefix}.pricing: 必须是对象'
        return
    if pricing.get('currency', 'CNY') != 'CNY':
        yield f'{prefix}.pricing: 手工价格必须先换算为 CNY'
    peaks = pricing.get('peaks', [])
    if not isinstance(peaks, list):
        yield f'{prefix}.pricing.peaks: 必须是数组'
        return
    for rate in [pricing, *peaks]:
        if not isinstance(rate, dict):
            yield f'{prefix}.pricing: 峰谷价格必须是对象'
            continue
        for key in ('input', 'output', 'cache_hit', 'peak_input', 'peak_output', 'peak_cache_hit'):
            if key in rate and not valid_rate(rate[key]):
                yield f'{prefix}.pricing.{key}: 必须是非负有限数值'


def _valid_url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False
