"""Web 配置、模型连接与定价接口；写入规则由 config 包定义。"""
import json
import os

from flask import Blueprint, current_app, jsonify, request

from hydroai.ai import services
from hydroai.ai.health import clear_health_cache
from hydroai.ai.providers import CATEGORIES, preset_list
from hydroai.config.inspection import diagnose, price_report
from hydroai.config.io import (
    ENV_NAME, atomic_write_json, merge_config, normalize_config,
    read_config, redact_config, strip_redacted, validate_document,
)
from hydroai.config.manager import ConfigManager, FIELD_TO_JSON
from hydroai.config.models import (
    ModelOverrideError, TIERS, add_model, delete_model, model_names, roles_payload,
    set_model_proxy, set_model_role, switch_model,
)
from hydroai.config.paths import config_file

bp = Blueprint('settings', __name__)


def _path():
    return config_file('config.json', current_app.config['PROJECT_ROOT'])


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('需要 JSON 对象')
    return data


def _error(exc):
    if isinstance(exc, ModelOverrideError):
        return jsonify(error=str(exc)), 409
    if isinstance(exc, ValueError) and not isinstance(exc, (json.JSONDecodeError, UnicodeError)):
        return jsonify(error=str(exc)), 400
    return jsonify(error='无法读取或保存配置，请检查 JSON 格式与文件权限'), 400


def _revision(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return 'missing'
    return f'{stat.st_ino:x}-{stat.st_mtime_ns:x}-{stat.st_size:x}'


@bp.route('/config', methods=['GET', 'POST'])
def config_page():
    path = _path()
    try:
        with current_app.extensions['config_lock']:
            revision = _revision(path)
            if request.method == 'GET':
                doc = read_config(path)
                stored = request.args.get('view') == 'stored'
                if not stored:
                    manager = ConfigManager(path)
                    doc.update({key: getattr(manager.cfg, attr) for attr, key in FIELD_TO_JSON.items()})
                safe = redact_config(doc)
                if not stored:
                    safe['ai_api_key_configured'] = bool(manager.api_key)
                response = jsonify(safe)
                response.set_etag(revision)
                response.headers['Cache-Control'] = 'no-store'
                if not stored:
                    sources = {key: source for attr, key in FIELD_TO_JSON.items()
                               if (source := manager.environment_source(attr))}
                    response.headers['X-Config-Overrides'] = json.dumps(sources, separators=(',', ':'))
                return response
            if 'If-Match' in request.headers and not request.if_match.contains(revision):
                return jsonify(error='配置已更新，请重新载入后合并未保存的修改'), 409
            doc = merge_config(read_config(path), normalize_config(strip_redacted(_body())))
            errors = validate_document(doc)
            if errors:
                return jsonify(error='配置校验失败', errors=errors), 400
            atomic_write_json(path, doc)
        clear_health_cache()
        current_app.extensions['workspace'].emit('配置已更新')
        return jsonify(ok=True)
    except (OSError, ValueError, TypeError) as exc:
        return _error(exc)


@bp.get('/config/doctor')
def config_doctor():
    return jsonify(diagnose(_path()))


def _new_connection(body):
    key = body.get('api_key_value', '')
    reference = body.get('api_key_env', '')
    if not isinstance(key, str) or (reference and (not isinstance(reference, str) or not ENV_NAME.fullmatch(reference))):
        raise ValueError('API Key 或环境变量名无效')
    return {'base_url': services.normalize_base_url(body['base_url']),
            'api_key': key.strip() or (os.environ.get(reference, '') if reference else ''),
            'provider': body.get('provider') or 'compatible',
            'proxy': services.normalize_proxy(body.get('proxy', ''))}


@bp.post('/models/discover')
def models_discover():
    try:
        body = _body()
        if 'base_url' in body:
            config = _new_connection(body)
        else:
            manager = ConfigManager(_path())
            name = body.get('model') or manager.ai_model
            if not isinstance(name, str) or name not in model_names(manager):
                raise ValueError('请选择已经保存的模型连接')
            config = manager.get_model_config(name)
        return jsonify(services.discover_models(config))
    except (OSError, ValueError, TypeError) as exc:
        return _error(exc)


def _change_model(command, *, required, optional=()):
    try:
        body = _body()
        if not set(required) <= body.keys() or body.keys() - set(required) - set(optional):
            raise ValueError('缺少必要字段或包含未知字段')
        with current_app.extensions['config_lock']:
            result = command(_path(), **body)
        clear_health_cache()
        return jsonify(result)
    except (ValueError, TypeError, OSError) as exc:
        return _error(exc)


@bp.post('/models/add')
def models_add():
    return _change_model(add_model, required=('name', 'model_id', 'base_url'),
                         optional=('api_key_env', 'provider', 'pricing_provider',
                                   'set_default', 'api_key_value', 'proxy'))


@bp.get('/api/providers')
def api_providers():
    try:
        manager = ConfigManager(_path())
        active = manager.ai_model
        roles = roles_payload(manager)
        tiers = {role['id']: role['value'] for role in roles if role['id'] in TIERS and role['value']}
        configured = []
        for name in sorted(model_names(manager), key=lambda name: (name != active, name)):
            model = manager.get_model_config(name)
            configured.append({
                'name': name, 'model_id': model.get('model_id') or name,
                'base_url': model['base_url'], 'provider': model.get('provider') or '',
                'api_key_configured': bool(model['api_key']), 'proxy_configured': bool(model.get('proxy')),
                'current': name == active, 'managed': name in manager.models,
                'tiers': [tier for tier, chosen in tiers.items() if chosen == name],
                'pricing_known': bool(model.get('pricing')),
                'roles_using': [role['name'] for role in roles if role['value'] == name],
            })
        return jsonify(presets=preset_list(), categories=CATEGORIES, configured=configured,
                       roles=roles, active={'model': active, 'tiers': tiers})
    except (OSError, ValueError, TypeError) as exc:
        return _error(exc)


@bp.post('/api/models/switch')
def api_models_switch():
    return _change_model(switch_model, required=('name',), optional=('single',))


@bp.post('/api/models/role')
def api_models_role():
    return _change_model(set_model_role, required=('role',), optional=('name',))


@bp.post('/api/models/proxy')
def api_models_proxy():
    return _change_model(set_model_proxy, required=('name',), optional=('proxy',))


@bp.post('/api/models/delete')
def api_models_delete():
    return _change_model(delete_model, required=('name',))


@bp.get('/pricing')
@bp.post('/pricing/refresh')
def pricing_status():
    try:
        return jsonify(price_report(ConfigManager(_path()), refresh=request.method == 'POST'))
    except (ValueError, TypeError, OSError) as exc:
        return _error(exc)
