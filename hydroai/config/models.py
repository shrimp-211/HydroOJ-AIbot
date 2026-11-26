"""模型连接和任务用途的配置规则，供 CLI 与 Web 共用。"""
import os
from pathlib import Path

from hydroai.ai.services import normalize_base_url, normalize_proxy
from hydroai.config.io import (
    ENV_NAME, atomic_write_bytes, atomic_write_json, check_document, dotenv_entry, read_config,
)
from hydroai.config.manager import ConfigManager

TIERS = ('flash', 'pro', 'max')
ROLE_FIELDS = {
    'default': 'ai_model',
    'editorial': 'editorial_model',
    'vision': 'ai_vision_model',
    'difficulty': 'difficulty_skip_model',
    'benchmark': 'benchmark_model',
    'testdata': 'testdata_model',
}
ROLE_INFO = (
    ('default', '默认模型', '未指定模型时使用'),
    ('flash', 'Flash 路由', '难度 1–2 / 快速尝试'),
    ('pro', 'Pro 路由', '难度 3–5'),
    ('max', 'Max 路由', '难度 6–8'),
    ('editorial', '题解生成', '留空=沿用求解模型'),
    ('vision', '视觉题面', '留空=自动选择'),
    ('difficulty', '难度检测备用', '关闭难度检测时使用；留空=路由首档'),
    ('benchmark', '标程扫描', '留空=默认模型'),
    ('testdata', '测试数据补充', '留空=默认模型'),
)


class ModelOverrideError(ValueError):
    """当前环境变量优先，所请求的切换无法生效。"""


def roles_payload(manager: ConfigManager) -> list[dict]:
    return [
        {'id': role, 'name': title, 'hint': hint, 'optional': role in ROLE_FIELDS and role != 'default',
         'value': (manager.get_tier_model(role) if role in TIERS else getattr(manager.cfg, ROLE_FIELDS[role])) or ''}
        for role, title, hint in ROLE_INFO
    ]


def model_names(manager: ConfigManager) -> set[str]:
    names = {name for name in manager.models if not name.startswith('_')}
    if manager.ai_model and not manager.ai_model.startswith('_'):
        names.add(manager.ai_model)
    return names


def _models(doc: dict) -> dict:
    models = doc.setdefault('models', {})
    if not isinstance(models, dict):
        raise ValueError('models 配置必须是对象，请先修正配置')
    return models


def _tiers(doc: dict) -> dict:
    router = doc.setdefault('model_router', {})
    if not isinstance(router, dict):
        raise ValueError('model_router 配置必须是对象')
    tiers = router.setdefault('tiers', {})
    if not isinstance(tiers, dict):
        raise ValueError('model_router.tiers 配置必须是对象')
    return tiers


def _require_effective_change(manager: ConfigManager, field: str, value: str):
    source = manager.environment_source(field)
    if source and getattr(manager.cfg, field) != value:
        raise ModelOverrideError(f'当前由环境变量 {source} 控制，修改环境后重新启动服务才能切换')


def _select_default(doc: dict, manager: ConfigManager, name: str, single: bool):
    _require_effective_change(manager, 'ai_model', name)
    if single:
        _require_effective_change(manager, 'difficulty_skip_model', '')
        _tiers(doc).update(dict.fromkeys(TIERS, name))
        doc['difficulty_skip_model'] = ''
    doc['ai_model'] = name


def _model_name(value, *, optional=False) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError('模型名称必须是字符串，且不能包含控制字符')
    value = value.strip()
    if (not value and not optional) or value.startswith('_') or len(value) > 256:
        raise ValueError('配置名称不能为空、过长或以 _ 开头')
    return value


def _write_model_key(path: Path, name: str, value: str, doc: dict):
    """全部校验完成后写入凭据；JSON 写入失败时恢复原 .env。"""
    if any(char in value for char in '\r\n\0'):
        raise ValueError('API Key 不能包含换行或空字符')
    env_path = path.with_name('.env')
    old = env_path.read_bytes() if env_path.exists() else None
    lines = old.decode('utf-8-sig').splitlines() if old is not None else []
    entries = [entry for line in lines if (entry := dotenv_entry(line)) and entry[0] == name]
    if any(existing and existing != value for _, existing in entries) or os.environ.get(name) not in (None, '', value):
        raise ValueError('该环境变量已有不同的值，请为新连接使用独立的 API Key 变量名')
    if any(char in value for char in '#\"\''):
        value_to_write = f'"{value}"'
    else:
        value_to_write = value
    lines = [line for line in lines if (dotenv_entry(line) or ('',))[0] != name]
    lines.append(f'{name}={value_to_write}')
    atomic_write_bytes(env_path, ('\n'.join(lines) + '\n').encode('utf-8'))
    try:
        atomic_write_json(path, doc)
    except (OSError, ValueError, TypeError):
        if old is None:
            env_path.unlink(missing_ok=True)
        else:
            atomic_write_bytes(env_path, old)
        raise
    os.environ[name] = value


def add_model(path, *, name, model_id, base_url, api_key_env='AI_API_KEY', provider='compatible',
              pricing_provider='', set_default=False, proxy='', api_key_value=''):
    name = _model_name(name)
    if not isinstance(model_id, str) or not model_id.strip() or any(ord(c) < 32 for c in model_id):
        raise ValueError('模型 ID 必须是非空字符串，且不能包含控制字符')
    if not isinstance(api_key_env, str) or not ENV_NAME.fullmatch(api_key_env):
        raise ValueError('API Key 环境变量名无效')
    if type(set_default) is not bool:
        raise ValueError('set_default 必须是 bool')
    if not isinstance(api_key_value, str):
        raise ValueError('API Key 必须是字符串')
    path = Path(path)
    doc = read_config(path)
    models = _models(doc)
    if name in models:
        raise ValueError('该配置名称已存在，请使用新名称；已有模型可在 JSON 中修改')
    model_id = model_id.strip()
    settings = {'model_id': model_id, 'base_url': normalize_base_url(base_url), 'api_key': 'env:' + api_key_env,
                'provider': provider, 'max_tokens': 16384, 'reasoning_effort': '', 'supports_reasoning_effort': False}
    if pricing_provider:
        settings['pricing_provider'] = pricing_provider
    if proxy := normalize_proxy(proxy):
        settings['proxy'] = proxy
    models[name] = settings
    if set_default:
        _select_default(doc, ConfigManager(path), name, single=True)
    check_document(doc)
    if api_key_value.strip():
        _write_model_key(path, api_key_env, api_key_value.strip(), doc)
    else:
        atomic_write_json(path, doc)
    return {'ok': True, 'name': name, 'model_id': model_id}


def switch_model(path, *, name, single=True):
    name = _model_name(name)
    if type(single) is not bool:
        raise ValueError('single 必须是 bool')
    doc = read_config(path)
    manager = ConfigManager(path)
    if name not in model_names(manager):
        raise ValueError('模型不存在，请先添加')
    _select_default(doc, manager, name, single=single)
    check_document(doc)
    atomic_write_json(path, doc)
    saved = ConfigManager(path)
    tiers = {tier: value for tier in TIERS if (value := saved.get_tier_model(tier))}
    return {'ok': True, 'active': saved.ai_model, 'single': single, 'tiers': tiers}


def set_model_role(path, *, role, name=''):
    if not isinstance(role, str) or (role not in ROLE_FIELDS and role not in TIERS):
        raise ValueError('未知的模型用途')
    name = _model_name(name, optional=role in ROLE_FIELDS and role != 'default')
    doc = read_config(path)
    manager = ConfigManager(path)
    if name and name not in model_names(manager):
        raise ValueError('模型不存在，请先添加')
    if role in TIERS:
        _tiers(doc)[role] = name
    else:
        field = ROLE_FIELDS[role]
        _require_effective_change(manager, field, name)
        doc[field] = name
    check_document(doc)
    atomic_write_json(path, doc)
    return {'ok': True, 'roles': roles_payload(ConfigManager(path))}


def set_model_proxy(path, *, name, proxy=''):
    name, proxy = _model_name(name), normalize_proxy(proxy)
    doc = read_config(path)
    model = _models(doc).get(name)
    if not isinstance(model, dict):
        raise ValueError('模型不存在或配置格式错误')
    if proxy:
        model['proxy'] = proxy
    else:
        model.pop('proxy', None)
    check_document(doc)
    atomic_write_json(path, doc)
    return {'ok': True, 'proxy_configured': bool(proxy)}


def delete_model(path, *, name):
    name = _model_name(name)
    doc = read_config(path)
    manager = ConfigManager(path)
    models = _models(doc)
    if name not in models:
        raise ValueError('模型不存在')
    if name in (doc.get('ai_model'), manager.ai_model):
        raise ValueError('当前默认模型不能删除，请先切换默认模型')
    tiers = _tiers(doc)
    using = [role['name'] for role in roles_payload(manager)
             if name in (role['value'], (tiers.get(role['id']) if role['id'] in TIERS
                                          else doc.get(ROLE_FIELDS[role['id']])))]
    if using:
        raise ValueError('该模型仍被以下用途使用，请先切换: ' + '、'.join(using))
    del models[name]
    check_document(doc)
    atomic_write_json(path, doc)
    return {'ok': True}
