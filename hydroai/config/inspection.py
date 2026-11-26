"""供 CLI 和 Web 共用的离线配置诊断与定价报告。"""
from pathlib import Path

from hydroai.ai.protocol import build_request, provider_for
from hydroai.config.io import read_config, validate_document
from hydroai.config.manager import ConfigManager


def diagnose(path):
    errors, warnings, rows = [], [], []
    try:
        if not Path(path).exists():
            errors.append('配置文件不存在，请先运行 init')
        raw = read_config(path)
        errors.extend(validate_document(raw))
    except (OSError, ValueError) as exc:
        return {'ok': False, 'errors': [f'配置读取失败: {type(exc).__name__}'], 'warnings': [], 'models': []}
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'models': []}
    cm = ConfigManager(str(path))
    errors.extend(cm.validate())
    names = {cm.ai_model}
    names.update(name for name in cm.models if not name.startswith('_'))
    for name in sorted(names):
        try:
            settings = cm.get_model_config(name)
        except ValueError:
            errors.append(f'模型 {name}: API 地址无效')
            continue
        row = {'name': name, 'provider': provider_for(settings),
               'api_key_configured': bool(settings['api_key'])}
        if not row['api_key_configured']:
            errors.append(f'模型 {name}: API Key 未配置或环境变量引用不存在')
        try:
            args = build_request(name, [], settings, max_tokens=cm.task_token_limit())
            row['output_limit'] = args.get('max_tokens', args.get('max_completion_tokens'))
        except (TypeError, ValueError) as exc:
            errors.append(f'模型 {name}: 请求参数无效 ({type(exc).__name__})')
        price = cm.pricing_info(name)
        row['pricing'] = price
        if not price['known']:
            warnings.append(f"模型 {name}: 费用未知；{price.get('reason', '')}")
        elif price.get('stale'):
            warnings.append(f'模型 {name}: 正在使用过期的价格缓存')
        rows.append(row)
    if not cm.username or not cm.password:
        warnings.append('OJ 登录凭据未完整配置，求解前需要补充 OJ_USERNAME / OJ_PASSWORD')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'models': rows}


def price_report(manager, *, refresh=False):
    state = manager.price_catalog().snapshot(force=refresh, ttl=manager.pricing_refresh_hours * 3600)
    names = sorted({manager.ai_model} | {name for name in manager.models if not name.startswith('_')})
    return {'ok': bool(state['entries']) if refresh else True,
            **{key: value for key, value in state.items() if key != 'entries'},
            'count': len(state['entries']),
            'models': [{'name': name, **manager.pricing_info(name)} for name in names]}
