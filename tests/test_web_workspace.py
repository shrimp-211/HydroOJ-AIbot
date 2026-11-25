import queue
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

from hydroai.web import app as web

app = web.app


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


def test_workspace_serves_local_assets_and_accessible_tabs():
    client = app.test_client()
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

    for name in ('solve', 'usage', 'config'):
        tab = page.elements[f'nav-{name}']
        panel = page.elements[tab['aria-controls']]
        assert tab['role'] == 'tab'
        assert tab['aria-selected'] == str(name == 'solve').lower()
        assert panel['role'] == 'tabpanel'
        assert panel['aria-labelledby'] == tab['id']
        assert ('hidden' in panel) == (name != 'solve')

    assert {'url', 'contest-url', 'cfg-web-token', 'cfg-advanced-json', 'usage-breakdown-by'} <= page.labels
    assert page.labels <= page.elements.keys()


def test_usage_endpoints_work_on_concurrent_cold_start(tmp_path):
    code = '''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from threading import Barrier
from hydroai.web import app as web

web.ROOT = Path(sys.argv[1])
paths = ('/api/usage/summary', '/api/quota', '/api/usage/series', '/api/usage/breakdown')
ready = Barrier(len(paths))

def get(path):
    ready.wait()
    response = web.app.test_client().get(path)
    assert response.status_code == 200, path
    assert response.is_json, path

with ThreadPoolExecutor(len(paths)) as executor:
    list(executor.map(get, paths))
'''
    subprocess.run([sys.executable, '-c', code, str(tmp_path)],
                   cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)


def test_kill_task_falls_back_when_os_has_no_killpg(monkeypatch):
    class Process:
        killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(web, 'AUTH_TOKEN', '')
    monkeypatch.setattr(web, 'active_proc', process)
    monkeypatch.setattr(web, 'task_running', True)
    monkeypatch.setattr(web, 'emit', lambda _: None)
    monkeypatch.delattr(web.os, 'killpg', raising=False)

    response = app.test_client().post('/kill')

    assert response.status_code == 200
    assert process.killed
    assert web.task_running  # 等待负责回收子进程的线程释放任务槽。


def test_stream_flushes_handshake_before_waiting_for_logs(monkeypatch):
    def empty_queue(*args, **kwargs):
        raise queue.Empty

    monkeypatch.setattr(web, 'AUTH_TOKEN', '')
    monkeypatch.setattr(web.log_queue, 'get', empty_queue)
    client = app.test_client()
    ticket = client.post('/stream/ticket').get_json()['ticket']
    response = client.get('/stream', query_string={'ticket': ticket}, buffered=False)
    try:
        assert response.mimetype == 'text/event-stream'
        assert next(response.response) == b':connected\n\n'
    finally:
        response.close()
