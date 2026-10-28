#!/usr/bin/env python3
"""Initialize and inspect a configuration without making paid AI requests."""
import argparse
import json
from pathlib import Path
from runtime_paths import data_file

from config_io import atomic_write_json, read_config, validate_document
from config_manager import ConfigManager
from ai_protocol import build_request, provider_for
from oj_common import load_dotenv


def initialize(path, *, model, base_url, oj_root, provider='compatible', profile='balanced'):
    path = Path(path)
    if path.exists():
        raise ValueError('配置文件已存在，拒绝覆盖；请指定新的 --config 路径')
    cfg = {
        'oj_root': oj_root.rstrip('/'), 'oj_base': oj_root.rstrip('/') + '/d/system',
        'ai_model': model, 'ai_base_url': base_url, 'ai_api_key': 'env:AI_API_KEY',
        'ai_max_tokens': 16384, 'ai_reasoning_effort': '',
        'models': {model: {'provider': provider, 'supports_reasoning_effort': False,
                           'api_key': 'env:AI_API_KEY', 'max_tokens': 16384}},
        'model_router': {'tiers': {'flash': model}},
        'ai_task_tokens': {'default': 16384, 'solve': 16384, 'plan': 4096,
                           'reference': 8192, 'difficulty': 1024, 'editorial': 8192},
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
        settings = cm.get_model_config(name)
        row = {'name': name, 'provider': provider_for(settings),
               'api_key_configured': bool(settings['api_key'])}
        if not row['api_key_configured']:
            errors.append(f'模型 {name}: API Key 未配置或环境变量引用不存在')
        try:
            args = build_request(name, [], settings, max_tokens=cm.task_token_limit())
            row['output_limit'] = args.get('max_tokens', args.get('max_completion_tokens'))
        except (TypeError, ValueError) as exc:
            errors.append(f'模型 {name}: 请求参数无效 ({type(exc).__name__})')
        rows.append(row)
    if not cm.username or not cm.password:
        warnings.append('OJ 登录凭据未完整配置，求解前需要补充 OJ_USERNAME / OJ_PASSWORD')
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'models': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(data_file('config.json')), help='配置文件路径')
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='创建最小可用配置与空白 .env，不覆盖已有文件')
    init.add_argument('--model', required=True)
    init.add_argument('--base-url', required=True)
    init.add_argument('--oj-root', required=True)
    init.add_argument('--provider', choices=['compatible', 'openai', 'deepseek', 'zhipu'], default='compatible')
    init.add_argument('--profile', choices=['balanced', 'economy'], default='balanced')
    doctor = commands.add_parser('doctor', help='离线检查配置、密钥引用和模型参数；不发 API 请求')
    doctor.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'init':
        try:
            initialize(args.config, model=args.model, base_url=args.base_url, oj_root=args.oj_root,
                       provider=args.provider, profile=args.profile)
        except (OSError, ValueError) as exc:
            parser.exit(1, f'{exc}\n')
        print(f'已生成 {args.config}。请填写同目录 .env，再运行 doctor。')
        return 0
    load_dotenv(str(Path(args.config).with_name('.env')))
    report = diagnose(args.config)
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
