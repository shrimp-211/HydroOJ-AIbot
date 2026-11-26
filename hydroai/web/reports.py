"""仪表盘、请求用量与额度的只读视图。"""
import time
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from hydroai.config.manager import ConfigManager
from hydroai.config.paths import config_file, data_file
from hydroai.usage import QuotaManager, get_store
from hydroai.usage.dashboard import Dashboard

bp = Blueprint('reports', __name__)


def _usage_store():
    return get_store(data_file('usage.db', current_app.config['PROJECT_ROOT']))


def _today_start() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _bounded(name, default, maximum):
    return max(1, min(request.args.get(name, default, type=int), maximum))


@bp.get('/api/usage/summary')
def api_usage_summary():
    days = _bounded('days', 30, 3660)
    store = _usage_store()
    return jsonify(days=days, summary=store.summary(since=time.time() - days * 86400),
                   today=store.summary(since=_today_start()))


@bp.get('/api/usage/series')
def api_usage_series():
    days = _bounded('days', 30, 366)
    return jsonify(days=days, series=_usage_store().daily(days=days))


@bp.get('/api/usage/breakdown')
def api_usage_breakdown():
    by = request.args.get('by', 'model')
    if by not in ('model', 'provider', 'task', 'problem_id', 'day', 'hour'):
        return jsonify(error='不支持的统计维度'), 400
    days = _bounded('days', 30, 3660)
    rows = _usage_store().breakdown(by, since=time.time() - days * 86400,
                                    limit=_bounded('limit', 20, 500))
    return jsonify(by=by, days=days, rows=rows)


@bp.get('/api/usage/recent')
def api_usage_recent():
    days = _bounded('days', 7, 3660)
    rows = _usage_store().recent(_bounded('limit', 50, 500), since=time.time() - days * 86400)
    return jsonify(days=days, requests=rows)


@bp.get('/api/usage/heatmap')
def api_usage_heatmap():
    return jsonify(_usage_store().heatmap(days=_bounded('days', 365, 3660)))


@bp.get('/api/quota')
@bp.post('/api/quota/refresh')
def api_quota():
    path = config_file('config.json', current_app.config['PROJECT_ROOT'])
    extensions = current_app.extensions
    try:
        with extensions['quota_lock']:
            cached_path, manager = extensions.get('quota_manager', (None, None))
            if cached_path != path:
                manager = QuotaManager(ConfigManager(path), store=_usage_store())
                extensions['quota_manager'] = (path, manager)
        return jsonify(manager.summary(refresh=request.method == 'POST'))
    except (OSError, ValueError, TypeError):
        return jsonify(items=[], total=0, low=0, errors=0, error='无法读取额度配置')


def _dashboard():
    return Dashboard(data_file('dashboard.json', current_app.config['PROJECT_ROOT']))


@bp.get('/api/stats')
def api_stats():
    stats = _dashboard().stats()
    values = {key: stats[key] for key in ('total', 'ac', 'fail', 'ac_rate', 'total_tokens')}
    store = _usage_store()
    return jsonify(**values, total_cost=round(stats['total_cost'], 4),
                   usage=store.summary(), by_model=store.breakdown('model', limit=10))


@bp.get('/api/today')
def api_today():
    stats = _dashboard().today(limit=20)
    stats['cost'] = round(stats['cost'], 4)
    stats.pop('cache_hit', None)
    return jsonify(**stats, usage=_usage_store().summary(since=_today_start()))


@bp.get('/api/history')
def api_history():
    return jsonify(history=_dashboard().recent(_bounded('n', 20, 500)))
