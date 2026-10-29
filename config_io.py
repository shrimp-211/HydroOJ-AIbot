"""Configuration file IO shared by the CLI, Web UI and runtime manager."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile

LEGACY_SECTIONS = {
    'oj': {'root': 'oj_root', 'base': 'oj_base', 'username': 'username', 'password': 'password'},
    'ai': {'base_url': 'ai_base_url', 'model': 'ai_model', 'ai_api_key': 'ai_api_key',
           'api_key': 'ai_api_key', 'max_tokens': 'ai_max_tokens', 'reasoning_effort': 'ai_reasoning_effort'},
    'code': {'lang': 'code_lang'},
    'monitor': {'domains': 'monitor_domains', 'interval': 'monitor_interval', 'state_file': 'monitor_state_file', 'start_page': 'contest_start_page'},
}


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
    path = Path(path)
    if not path.exists():
        return {}
    return normalize_config(json.loads(path.read_text(encoding='utf-8-sig')))


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


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


def redact_config(data):
    """Never return credentials from any nested model/provider setting."""
    if isinstance(data, list):
        return [redact_config(item) for item in data]
    if not isinstance(data, dict):
        return data
    result = {}
    for key, value in data.items():
        low = key.lower()
        if any(word in low for word in ('password', 'api_key', 'secret', 'authorization', 'access_token')):
            result[key + '_configured'] = bool(value)
        else:
            result[key] = redact_config(value)
    return result


def merge_config(current, patch):
    result = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def validate_document(data: dict) -> list[str]:
    """Validate supplied values without displaying secret values or contacting services."""
    from dataclasses import asdict
    from config_manager import AppConfig, JSON_TO_FIELD
    defaults = asdict(AppConfig())
    errors = []
    for key, value in data.items():
        attr = JSON_TO_FIELD.get(key)
        if not attr:
            continue  # Allow extension settings used by other entry points.
        default = defaults[attr]
        if type(value) is not type(default) and not (attr == 'pricing_usd_to_cny' and type(value) is int):
            # Runtime accepts string scalars for legacy configs; the editor writes typed JSON.
            errors.append(f'{key}: 应为 {type(default).__name__}')
    for key in ('oj_root', 'oj_base', 'ai_base_url'):
        if key in data and not _valid_url(data[key]):
            errors.append(f'{key}: 需要 http(s) URL')
    for key in ('ai_max_tokens', 'verify_timeout', 'monitor_interval', 'contest_start_page', 'pricing_refresh_hours', 'agent_max_steps',
                'agent_max_submissions', 'agent_max_seconds', 'agent_run_timeout', 'solve_candidates'):
        if key in data and (type(data[key]) is not int or data[key] <= 0):
            errors.append(f'{key}: 必须是正整数')
    from model_pricing import valid_rate
    from model_services import normalize_base_url, models_urls
    if 'pricing_usd_to_cny' in data and not valid_rate(data['pricing_usd_to_cny']):
        errors.append('pricing_usd_to_cny: 必须是非负有限数值，0 表示未设置')
    if data.get('ai_base_url'):
        try:
            normalize_base_url(data['ai_base_url'])
        except ValueError as exc:
            errors.append(str(exc))
    budgets = data.get('ai_task_tokens', {})
    if isinstance(budgets, dict):
        for task, value in budgets.items():
            if type(value) is not int or value <= 0:
                errors.append(f'ai_task_tokens.{task}: 必须是正整数')
    models = data.get('models', {})
    if isinstance(models, dict):
        for name, model in models.items():
            if name.startswith('_'):
                continue
            if not isinstance(model, dict):
                errors.append(f'models.{name}: 必须是对象')
                continue
            prefix = f'models.{name}'
            if model.get('base_url') and not _valid_url(model['base_url']):
                errors.append(f'{prefix}.base_url: 需要 http(s) URL')
            if model.get('provider', 'auto') not in ('auto', 'openai', 'deepseek', 'zhipu', 'compatible'):
                errors.append(f'{prefix}.provider: 不支持的供应商')
            if model.get('token_parameter') not in (None, '', 'max_tokens', 'max_completion_tokens'):
                errors.append(f'{prefix}.token_parameter: 不支持的参数名')
            for key in ('max_tokens', 'timeout', 'max_retries'):
                value = model.get(key)
                if value is not None and (type(value) not in (int, float) or value < (0 if key == 'max_retries' else 1)):
                    errors.append(f'{prefix}.{key}: 无效数值')
                elif value is not None and key != 'timeout' and int(value) != value:
                    errors.append(f'{prefix}.{key}: 必须是整数')
            for key in ('stream_usage', 'supports_reasoning_effort', 'vision'):
                if key in model and type(model[key]) is not bool:
                    errors.append(f'{prefix}.{key}: 必须是 bool')
            if model.get('thinking') not in (None, 'enabled', 'disabled'):
                errors.append(f'{prefix}.thinking: 必须是 enabled 或 disabled')
            if 'api_key' in model and model['api_key'] is not None and not isinstance(model['api_key'], str):
                errors.append(f'{prefix}.api_key: 必须是字符串或 null')
            for key in ('model_id', 'models_url', 'pricing_provider', 'pricing_model'):
                if key in model and not isinstance(model[key], str):
                    errors.append(f'{prefix}.{key}: 必须是字符串')
            if isinstance(model.get('models_url', ''), str):
                try:
                    models_urls(model.get('base_url') or data.get('ai_base_url') or defaults['ai_base_url'], model.get('models_url', ''))
                except ValueError as exc:
                    errors.append(f'{prefix}: {exc}')
            pricing = model.get('pricing', {})
            if not isinstance(pricing, dict):
                errors.append(f'{prefix}.pricing: 必须是对象')
            else:
                if pricing.get('currency', 'CNY') != 'CNY':
                    errors.append(f'{prefix}.pricing: 手工价格必须先换算为 CNY')
                rates = [pricing] + (pricing.get('peaks', []) if isinstance(pricing.get('peaks', []), list) else [])
                for rate in rates:
                    if not isinstance(rate, dict):
                        errors.append(f'{prefix}.pricing: 峰谷价格必须是对象')
                        continue
                    for key in ('input', 'output', 'cache_hit', 'peak_input', 'peak_output', 'peak_cache_hit'):
                        if key in rate and not valid_rate(rate[key]):
                            errors.append(f'{prefix}.pricing.{key}: 必须是非负有限数值')
            extra = model.get('extra_body', {})
            if not isinstance(extra, dict) or set(extra) & {'model', 'messages', 'stream', 'max_tokens', 'max_completion_tokens'}:
                errors.append(f'{prefix}.extra_body: 必须是对象且不能覆盖路由/输出上限')
    router = data.get('model_router', {})
    if isinstance(router, dict):
        for key in ('tiers', 'thinking_levels'):
            entries = router.get(key, {})
            if not isinstance(entries, dict) or any(not isinstance(v, str) for v in entries.values()):
                errors.append(f'model_router.{key}: 必须是字符串映射')
    return errors


def _valid_url(value):
    from urllib.parse import urlparse
    try:
        parsed = urlparse(value)
        return parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username
    except (ValueError, TypeError, AttributeError):
        return False
