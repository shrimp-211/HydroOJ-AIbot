import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path


class WorkspaceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = {}
        self.labels = set()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            assert attrs['id'] not in self.elements, f'重复 ID：{attrs["id"]}'
            self.elements[attrs['id']] = attrs
        if tag == 'label' and 'for' in attrs:
            self.labels.add(attrs['for'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.assets.append((attrs['href'], 'text/css'))
        if tag == 'script' and 'src' in attrs:
            self.assets.append((attrs['src'], 'javascript'))


def test_workspace_serves_local_assets_and_accessible_tabs(auth, web_app):
    client = auth(web_app.test_client())
    response = client.get('/')
    assert response.status_code == 200
    page = WorkspaceParser()
    page.feed(response.get_data(as_text=True))

    assert len(page.assets) == 2
    for url, content_type in page.assets:
        assert url.startswith('/') and not url.startswith('//')
        asset = client.get(url)
        assert asset.status_code == 200
        assert content_type in asset.content_type

    for name in ('solve', 'usage', 'models', 'config'):
        tab = page.elements[f'nav-{name}']
        panel = page.elements[tab['aria-controls']]
        assert tab['role'] == 'tab'
        assert tab['aria-selected'] == str(name == 'solve').lower()
        assert panel['role'] == 'tabpanel'
        assert panel['aria-labelledby'] == tab['id']
        assert ('hidden' in panel) == (name != 'solve')

    assert {'url', 'contest-url', 'cfg-advanced-json', 'usage-breakdown-by'} <= page.labels
    assert page.labels <= page.elements.keys()


def test_usage_endpoints_work_on_concurrent_cold_start(tmp_path):
    code = '''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from threading import Barrier
from hydroai.web.app import create_app

app = create_app(Path(sys.argv[1]), config={'WEBUI_USERNAME': 'admin', 'WEBUI_PASSWORD': 'test'})
paths = ('/api/usage/summary', '/api/quota', '/api/usage/series', '/api/usage/breakdown')
ready = Barrier(len(paths))

def get(path):
    ready.wait()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess.update(authenticated=True, username='admin')
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.is_json, path

with ThreadPoolExecutor(len(paths)) as executor:
    list(executor.map(get, paths))
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)


def test_kill_route_keeps_task_reserved_until_worker_reaps_it(monkeypatch, auth, web_app):
    class Process:
        def poll(self):
            return None

    workspace = web_app.extensions['workspace']
    process = Process()
    stopped = []
    monkeypatch.setattr(workspace, '_active_proc', process)
    monkeypatch.setattr(workspace, '_task_running', True)
    monkeypatch.setattr(workspace, '_terminate', stopped.append)

    response = auth(web_app.test_client()).post('/kill')

    assert response.status_code == 200
    assert stopped == [process]
    assert workspace.task_running  # 等待负责回收子进程的线程释放任务槽。


def test_stream_flushes_handshake_and_each_client_reads_the_same_logs(auth, web_app):
    workspace = web_app.extensions['workspace']
    workspace.emit('两端都能收到')
    streams = [auth(web_app.test_client()).get('/stream', buffered=False) for _ in range(2)]
    try:
        for response in streams:
            assert response.mimetype == 'text/event-stream'
            assert next(response.response).strip() == b':connected'
            assert '两端都能收到' in next(response.response).decode('utf-8')
    finally:
        for response in streams:
            response.close()
