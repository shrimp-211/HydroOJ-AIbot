"""Flask 管理面板的应用组装、认证和运行入口。"""
import argparse
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from flask import (
    Blueprint, Flask, Response, current_app, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.serving import run_simple

from hydroai import __version__ as APP_VERSION
from hydroai.config.io import load_dotenv
from hydroai.config.paths import project_root
from hydroai.web.reports import bp as reports
from hydroai.web.runtime import Workspace
from hydroai.web.settings import bp as settings

routes = Blueprint('workspace', __name__)
PUBLIC_ENDPOINTS = {'workspace.login', 'workspace.status', 'workspace.api_version', 'static'}
ALLOWED_SCRIPTS = {'oj_solver', 'contest_solver', 'benchmark_solver', 'testdata_supplement'}


def _enabled(name):
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def create_app(root=None, *, config=None):
    app = Flask(__name__)
    app.config.update(
        PROJECT_ROOT=Path(root) if root is not None else project_root(),
        SECRET_KEY=os.environ.get('WEBUI_SECRET_KEY') or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=_enabled('WEBUI_COOKIE_SECURE'),
        WEBUI_USERNAME=os.environ.get('WEBUI_USERNAME') or 'admin',
        WEBUI_PASSWORD=os.environ.get('WEBUI_PASSWORD', ''),
    )
    if config:
        app.config.update(config)
    app.extensions['workspace'] = Workspace(app.config['PROJECT_ROOT'])
    app.extensions['config_lock'] = threading.Lock()
    app.extensions['quota_lock'] = threading.Lock()
    app.register_blueprint(routes)
    app.register_blueprint(settings)
    app.register_blueprint(reports)
    return app


def _workspace() -> Workspace:
    return current_app.extensions['workspace']


def _authenticated() -> bool:
    return bool(current_app.config['WEBUI_PASSWORD'] and session.get('authenticated')
                and session.get('username') == current_app.config['WEBUI_USERNAME'])


def _safe_next(value) -> str:
    target = value or '/'
    decoded = unquote(target)
    if (not decoded.startswith('/') or not target.startswith('/') or decoded.startswith('//')
            or '\\' in decoded or any(ord(char) < 32 for char in decoded)):
        return '/'
    return target


def _same_credential(value: str, expected: str) -> bool:
    return secrets.compare_digest(value.encode('utf-8'), expected.encode('utf-8'))


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return None
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == 'https' else 80
        return parts.scheme, parts.hostname, port
    except ValueError:
        return None


def _allows_write_origin() -> bool:
    site = request.headers.get('Sec-Fetch-Site')
    if site not in (None, 'same-origin', 'none'):
        return False
    source_url = request.headers.get('Origin', request.headers.get('Referer'))
    if source_url is None:
        return True  # 保留不发送浏览器来源信息的脚本客户端。
    source = _origin(source_url)
    if source is None:
        return False
    scheme = request.scheme
    if scheme == 'http' and source[0] == 'https' and site == 'same-origin':
        # 浏览器已确认同源，允许保留原始 Host 的 HTTPS 终止代理；不信任转发头。
        scheme = 'https'
    return source == _origin(f'{scheme}://{request.host}')


@routes.before_app_request
def check_request_access():
    endpoint = request.endpoint
    if endpoint is None:
        return None
    if endpoint not in PUBLIC_ENDPOINTS and not _authenticated():
        if endpoint == 'workspace.index':
            return redirect(url_for('workspace.login', next=request.path))
        return jsonify(error='未授权', code='unauthorized'), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not _allows_write_origin():
        return jsonify(error='请求来源与管理面板不同，请从面板页面操作', code='origin_mismatch'), 403
    return None


@routes.route('/login', methods=['GET', 'POST'])
def login():
    next_path = _safe_next(request.args.get('next') or request.form.get('next'))
    username = current_app.config['WEBUI_USERNAME']
    password = current_app.config['WEBUI_PASSWORD']
    error, status = '', 200
    if not password:
        error, status = '服务器未配置 WEBUI_PASSWORD，Web 已禁用。', 503
    elif request.method == 'GET' and _authenticated():
        return redirect(next_path)
    elif request.method == 'POST':
        valid_user = _same_credential(request.form.get('username', ''), username)
        valid_password = _same_credential(request.form.get('password', ''), password)
        if valid_user and valid_password:
            session.clear()
            session.update(authenticated=True, username=username)
            return redirect(next_path)
        error, status = '账号或密码不正确，请重试。', 401
        time.sleep(0.4)
    return render_template('login.html', error=error, next_path=next_path, app_version=APP_VERSION), status


@routes.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('workspace.login'))


@routes.get('/')
def index():
    return render_template('index.html', app_version=APP_VERSION)


@routes.get('/status')
def status():
    workspace = _workspace()
    daemon = workspace.daemon_status()
    return jsonify(task_running=workspace.task_running, daemon_running=daemon['running'],
                   daemon_pid=daemon['pid'], version=APP_VERSION)


@routes.get('/api/version')
def api_version():
    return jsonify(version=APP_VERSION)


def _start_task(args):
    if not _workspace().start_task(args):
        return jsonify(error='已有任务在运行'), 409
    return jsonify(ok=True)


@routes.post('/run/<name>')
def run_named(name):
    if name not in ALLOWED_SCRIPTS:
        return jsonify(error='不支持的任务'), 400
    body = request.get_json(silent=True) if request.data else {}
    if not isinstance(body, dict):
        return jsonify(error='需要 JSON 对象'), 400
    args = body.get('args', [])
    if not isinstance(args, list) or len(args) > 30 or not all(
            isinstance(arg, str) and len(arg) <= 2000 and '\0' not in arg for arg in args):
        return jsonify(error='参数必须是字符串数组'), 400
    return _start_task([name + '.py', *args])


@routes.post('/daemon/<action>')
def daemon_action(action):
    workspace = _workspace()
    if action == 'once':
        return _start_task(['contest_daemon.py', '--once', '--no-interactive'])
    if action == 'start':
        ok, error = workspace.start_daemon()
        if not ok:
            workspace.emit(error)
            return jsonify(error=error + '，请查看实时控制台'), 500
        workspace.emit('守护进程已启动')
        return jsonify(ok=True, daemon_running=True, daemon_pid=workspace.daemon_status()['pid'])
    if action == 'stop':
        ok, error = workspace.stop_daemon()
        if not ok:
            return jsonify(error=error, daemon_running=True), 500
        workspace.emit('守护进程已停止')
        return jsonify(ok=True, daemon_running=False)
    return jsonify(error='未知操作'), 400


@routes.post('/kill')
def kill_task():
    if not _workspace().stop_task():
        return jsonify(error='没有正在运行的任务'), 400
    return jsonify(ok=True)


@routes.get('/history')
def get_history():
    return jsonify(_workspace().history())


@routes.get('/stream')
def stream():
    return Response(_workspace().log_stream(request.headers.get('Last-Event-ID')),
                    mimetype='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


@routes.post('/clear')
def clear():
    _workspace().clear_logs()
    return jsonify(ok=True)


@routes.post('/api/reload-prompts')
def api_reload_prompts():
    from hydroai.ai.client import AIClient

    try:
        AIClient.reload_class_prompts()
    except (OSError, ValueError, TypeError):
        return jsonify(ok=False, error='无法加载提示词，请检查文件格式'), 500
    return jsonify(ok=True, msg='提示词已重载，新启动的任务会使用提示词文件')


app = create_app()


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description='HydroAI Web 管理面板')
    parser.add_argument('--port', type=int, default=5000, help='端口（默认 5000）')
    parser.add_argument('--host', default='127.0.0.1', help='绑定地址')
    parser.add_argument('--username', default=os.environ.get('WEBUI_USERNAME', 'admin'),
                        help='登录用户名（或 WEBUI_USERNAME）')
    parser.add_argument('--password', default=os.environ.get('WEBUI_PASSWORD', ''),
                        help='登录密码（或 WEBUI_PASSWORD）')
    parser.add_argument('--debug', action='store_true', help='启用调试页面')
    args = parser.parse_args(argv)
    if not args.password:
        parser.error('必须设置 WEBUI_PASSWORD（或 --password）才能启动 WebUI')

    web = create_app(config={'WEBUI_USERNAME': args.username or 'admin', 'WEBUI_PASSWORD': args.password})
    if sys.platform == 'win32' and sys.stdout.isatty() and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(f'[*] HydroAI WebUI 已启用登录认证: http://{args.host}:{args.port}')
    if _enabled('WEBUI_AUTOSTART_DAEMON'):
        ok, error = web.extensions['workspace'].start_daemon()
        print('[*] 守护进程已自动启动' if ok else f'[-] 守护进程自动启动失败: {error}')
    run_simple(args.host, args.port, web, use_reloader=False,
               use_debugger=args.debug, use_evalex=False, threaded=True)


if __name__ == '__main__':
    main()
