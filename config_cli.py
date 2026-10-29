#!/usr/bin/env python3
"""Initialize and inspect a configuration without making paid AI requests."""
import argparse
import json
from pathlib import Path
from runtime_paths import data_file

from config_io import atomic_write_json, read_config, validate_document
from config_manager import ConfigManager
from ai_protocol import build_request, provider_for
from model_services import normalize_base_url, discover_models, ModelServiceError
from oj_common import load_dotenv


def initialize(path, *, model, base_url, oj_root, provider='compatible', profile='balanced'):
    path = Path(path)
    if path.exists():
        raise ValueError('配置文件已存在，拒绝覆盖；请指定新的 --config 路径')
    cfg = {
        'oj_root': oj_root.rstrip('/'), 'oj_base': oj_root.rstrip('/') + '/d/system',
        'ai_model': model, 'ai_base_url': normalize_base_url(base_url), 'ai_api_key': 'env:AI_API_KEY',
        'ai_max_tokens': 16384, 'ai_reasoning_effort': '',
        'models': {model: {'provider': provider, 'supports_reasoning_effort': False,
                           'api_key': 'env:AI_API_KEY', 'max_tokens': 16384}},
        'model_router': {'tiers': {'flash': model}},
        'ai_task_tokens': {'default': 16384, 'solve': 16384, 'plan': 4096,
                           'reference': 8192, 'difficulty': 1024, 'editorial': 8192},
        'pricing_auto_update': True, 'pricing_refresh_hours': 6, 'pricing_usd_to_cny': 0.0,
        'contest_start_page': 2, 'compact_history': True, 'difficulty_detect_enable': profile != 'economy',
        'solve_candidates': 2 if profile == 'economy' else 3,
        'agent_stress_enable': True, 'editorial_enable': True,
        'show_thinking': False, 'ai_vision_model': '',
        'msg_whitelist': [], 'msg_push_list': [], 'msg_superuser': [], 'benchmark_users': [],
    }
    errors = validate_document(cfg)
    if errors:
        raise ValueError('; '.join(errors))
    atomic_write_json(path, cfg)
    env = path.with_name('.env')
    if not env.exists():
        with env.open('x', encoding='utf-8') as stream:
            stream.write('OJ_USERNAME=\nOJ_PASSWORD=\nAI_API_KEY=\n')
    return cfg


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


def add_model(path, *, name, model_id, base_url, api_key_env='AI_API_KEY',
              provider='compatible', pricing_provider='', set_default=False):
    import re
    if not isinstance(name, str) or not name.strip() or name.startswith('_') or len(name) > 256:
        raise ValueError('配置名称不能为空、过长或以 _ 开头')
    if not isinstance(model_id, str) or not model_id.strip() or any(ord(c) < 32 for c in name + model_id):
        raise ValueError('模型 ID 必须是非空字符串，且不能包含控制字符')
    if not isinstance(api_key_env, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', api_key_env):
        raise ValueError('API Key 环境变量名无效')
    doc = read_config(path)
    models = doc.setdefault('models', {})
    if not isinstance(models, dict):
        raise ValueError('models 配置必须是对象，请先修正配置')
    if name in models:
        raise ValueError('该配置名称已存在，请使用新名称；已有模型可在 JSON 中修改')
    models[name] = {'model_id': model_id.strip(), 'base_url': normalize_base_url(base_url),
                    'api_key': 'env:' + api_key_env, 'provider': provider, 'max_tokens': 16384,
                    'reasoning_effort': '', 'supports_reasoning_effort': False}
    if pricing_provider:
        models[name]['pricing_provider'] = pricing_provider
    if set_default:
        doc['ai_model'] = name
        # Explicitly selecting a single default replaces the old tier routing as well.
        doc['model_router'] = {'tiers': {'flash': name}}
        doc['difficulty_skip_model'] = ''
    errors = validate_document(doc)
    if errors:
        raise ValueError('; '.join(errors))
    atomic_write_json(path, doc)
    return {'ok': True, 'name': name, 'model_id': model_id.strip()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(data_file('config.json')), help='配置文件路径')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='创建单模型配置，不覆盖已有文件')
    init.add_argument('--model', required=True)
    init.add_argument('--base-url', required=True)
    init.add_argument('--oj-root', required=True)
    init.add_argument('--provider', choices=['compatible', 'openai', 'deepseek', 'zhipu'], default='compatible')
    init.add_argument('--profile', choices=['balanced', 'economy'], default='balanced')
    doctor = commands.add_parser('doctor', help='离线检查配置；不发 API 请求')
    doctor.add_argument('--json', action='store_true')
    models = commands.add_parser('models', help='使用已保存连接获取远端模型列表；不请求生成')
    models.add_argument('--model', help='已配置的模型名称，默认 ai_model')
    models.add_argument('--json', action='store_true')
    prices = commands.add_parser('prices', help='查看定价；可刷新 models.dev 价格目录')
    prices.add_argument('--refresh', action='store_true')
    prices.add_argument('--usd-to-cny', type=float, help='保存 USD 到 CNY 的换算率，需自行维护')
    prices.add_argument('--json', action='store_true')
    add = commands.add_parser('add-model', help='新增独立模型连接；只保存环境变量引用')
    add.add_argument('name', help='本地配置名称，可与 API 模型 ID 不同')
    add.add_argument('--model-id', required=True)
    add.add_argument('--base-url', required=True)
    add.add_argument('--api-key-env', default='AI_API_KEY')
    add.add_argument('--provider', choices=['compatible', 'openai', 'deepseek', 'zhipu'], default='compatible')
    add.add_argument('--pricing-provider', default='', help='models.dev 供应商 ID；代理服务须明确指定')
    add.add_argument('--set-default', action='store_true', help='设为默认，并切换为该模型的单模型路由')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            initialize(args.config, model=args.model, base_url=args.base_url, oj_root=args.oj_root,
                       provider=args.provider, profile=args.profile)
            print(f'已生成 {args.config}。请填写同目录 .env，再运行 doctor。')
            return 0
        load_dotenv(str(Path(args.config).with_name('.env')))
        if args.command == 'add-model':
            report = add_model(args.config, name=args.name, model_id=args.model_id, base_url=args.base_url,
                               api_key_env=args.api_key_env, provider=args.provider,
                               pricing_provider=args.pricing_provider, set_default=args.set_default)
            print(json.dumps(report, ensure_ascii=False))
            return 0
        manager = ConfigManager(args.config)
        if args.command == 'models':
            name = args.model or manager.ai_model
            if name != manager.ai_model and name not in manager.models:
                raise ValueError('模型配置不存在，请先运行 add-model')
            report = discover_models(manager.get_model_config(name))
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print('连接地址: ' + report['base_url'])
                print('\n'.join(row['id'] for row in report['models']) or '服务返回空模型列表')
            return 0
        if args.command == 'prices':
            if args.usd_to_cny is not None:
                patch = read_config(args.config)
                patch['pricing_usd_to_cny'] = args.usd_to_cny
                errors = validate_document(patch)
                if errors:
                    raise ValueError('; '.join(errors))
                atomic_write_json(args.config, patch)
                manager.reload()
            report = price_report(manager, refresh=args.refresh)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report['ok'] else 1
        report = diagnose(args.config)
    except (OSError, ValueError) as exc:
        # OSError and malformed JSON may contain local contents/paths; do not echo them.
        message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else '无法读取或保存配置文件'
        parser.exit(1, message + '\n')
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('配置检查通过' if report['ok'] else '配置检查未通过')
        for row in report['models']:
            print(f"  {row['name']} ({row['provider']}): 输出上限 {row.get('output_limit', '?')}, 密钥 {'已配置' if row['api_key_configured'] else '缺失'}")
        for message in report['errors'] + report['warnings']:
            print('  - ' + message)
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
