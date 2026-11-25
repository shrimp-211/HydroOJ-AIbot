#!/usr/bin/env python3
"""Initialize and inspect a configuration without making paid AI requests."""
import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from hydroai import __version__ as APP_VERSION
from hydroai.config.paths import config_file

from hydroai.config.io import atomic_write_json, read_config, validate_document
from hydroai.config.manager import ConfigManager
from hydroai.ai.protocol import build_request, provider_for
from hydroai.ai.services import normalize_base_url, discover_models, ModelServiceError
from hydroai.oj.common import load_dotenv


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


def _human_tokens(value) -> str:
    number = float(value or 0)
    for unit, scale in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if number >= scale:
            return f"{number / scale:.2f}{unit}"
    return str(int(number))


def _duration(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    text = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {text}" if days else text


def _tier_label(name) -> str:
    return {"five_hour": "5 小时", "weekly": "每周", "monthly": "每月",
            "credits": "额度"}.get(str(name), str(name))


def usage_report(days=30):
    """逐请求用量汇总（默认最近 30 天）。"""
    from hydroai.usage.store import get_store
    days = max(1, int(days))
    store = get_store()
    now = time.time()
    since = now - days * 86400
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {
        "days": days,
        "summary": store.summary(since=since),
        "today": store.summary(since=today_start),
        "by_model": store.breakdown("model", since=since, limit=20),
        "by_provider": store.breakdown("provider", since=since, limit=20),
        "daily": store.daily(days=min(days, 30)),
    }


def quota_report(manager, *, refresh=False):
    from hydroai.usage.quota import quota_status
    return quota_status(manager, refresh=refresh)


def _print_usage(report):
    s = report["summary"]
    print(f"📊 用量统计（最近 {report['days']} 天）")
    print(f"请求 {s['requests']} 次 | 输入 {_human_tokens(s['input_tokens'])}"
          f" | 输出 {_human_tokens(s['output_tokens'])}"
          f" | 缓存 {_human_tokens(s['cache_hit_tokens'])}"
          f"（命中率 {s['cache_rate']:.1%}）| 预估费用 ¥{s['cost']:.4f}")
    if s["requests"]:
        print(f"平均延迟 {s['avg_latency_s']:.1f}s | 平均输出速度 {s['avg_output_speed']:.1f} tok/s"
              f" | 失败 {s['failed_requests']} 次")
    if s["priced_requests"] < s["requests"]:
        print(f"⚠️ {s['requests'] - s['priced_requests']} 次请求缺少定价，费用可能被低估；"
              f"运行 config_cli.py prices 检查")
    t = report["today"]
    print(f"今日: {t['requests']} 次 | {_human_tokens(t['total_tokens'])} token"
          f" | 缓存 {t['cache_rate']:.0%} | ¥{t['cost']:.4f}")
    models = [m for m in report["by_model"] if m["requests"]]
    if models:
        print("按模型:")
        for row in models:
            print(f"  {row['key'] or '(未知)'}  {row['requests']} 次"
                  f"  {_human_tokens(row['total_tokens'])} token"
                  f"  缓存 {row['cache_rate']:.0%}  ¥{row['cost']:.4f}")


def _print_quota(report):
    items = report.get("items", [])
    if not items:
        print("未配置额度。可在 config/config.json 的 quotas 段添加"
              "（见 config/config.example.json）。")
        return
    print(f"💰 额度状态（{report['total']} 项，低余额/异常 {report['low']} 项）")
    for item in items:
        name = item["id"]
        kind = item["kind"]
        status = item.get("status", "?")
        mark = {"ok": "✅", "low": "⚠️", "error": "❌", "stale": "🕓"}.get(status, "•")
        if kind == "plan" or item.get("unit") == "percent":
            remaining = item.get("remaining")
            text = f"剩余 {remaining:.1f}%" if remaining is not None else "额度不可用"
            if item.get("plan_name"):
                text += f"（{item['plan_name']}）"
            print(f"  {mark} {name} [{kind}/{item.get('source', '')}] {text}")
            for tier in item.get("tiers") or []:
                line = (f"     └ {_tier_label(tier.get('name'))}: "
                        f"剩余 {tier.get('remaining_percent', 0):.1f}%")
                if tier.get("reset_in_s") is not None:
                    line += f"，重置 {_duration(tier['reset_in_s'])}"
                print(line)
            if item.get("error"):
                print(f"     └ {item['error']}")
            continue
        if kind == "balance" or item.get("unit") == "currency":
            remaining = item.get("remaining")
            text = f"剩余 {item.get('currency', 'CNY')} {remaining:.4f}" if remaining is not None else "余额不可用"
            if item.get("limit") is not None:
                text += f" / 总额 {item['limit']:.2f}"
        else:
            used, limit = item.get("used"), item.get("limit")
            text = (f"已用 {used} / {limit} {item.get('unit')}"
                    if used is not None and limit is not None else "用量不可用")
            if item.get("ratio") is not None:
                text += f"，剩余 {item['ratio']:.1%}"
            if item.get("window"):
                text += f"（窗口 {item['window']}）"
        reset = item.get("reset_in_s")
        if reset is not None:
            text += f"，重置倒计时 {_duration(reset)}"
        source = item.get("source", "")
        print(f"  {mark} {name} [{kind}/{source}] {text}")
        if item.get("error"):
            print(f"     └ {item['error']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {APP_VERSION}')
    parser.add_argument('--config', default=str(config_file('config.json')), help='配置文件路径')
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
    usage = commands.add_parser('usage', help='查看逐请求 token/费用统计')
    usage.add_argument('--days', type=int, default=30, help='统计最近 N 天（默认 30）')
    usage.add_argument('--prune', action='store_true',
                       help='按 usage_retention_days 立即清理旧明细')
    usage.add_argument('--json', action='store_true')
    quota = commands.add_parser('quota', help='查看订阅额度与账户余额')
    quota.add_argument('--refresh', action='store_true', help='强制刷新远端余额，忽略缓存')
    quota.add_argument('--json', action='store_true')
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
        if args.command == 'usage':
            report = usage_report(args.days)
            if args.prune:
                from hydroai.usage.store import get_store
                retention = ConfigManager(args.config).usage_retention_days
                report['pruned'] = get_store().prune(retention)
                report['retention_days'] = retention
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_usage(report)
                if args.prune:
                    print(f"🧹 已清理 {report.get('pruned', 0)} 条早于 "
                          f"{report.get('retention_days', 365)} 天的明细")
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
        if args.command == 'quota':
            report = quota_report(manager, refresh=args.refresh)
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                _print_quota(report)
            return 0
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
