"""Contest pagination and container persistence regressions, without network access."""
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from contest_catalog import list_contests, positive_int
from config_manager import ConfigManager
from docker_entrypoint import command_args
from runtime_paths import data_dir, data_file


class Session:
    def __init__(self, pages, status=200):
        self.pages, self.calls, self.status = list(pages), [], status

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        documents = self.pages.pop(0) if self.pages else []
        return NS(status_code=self.status, json=lambda: {'tdocs': documents})


def page(prefix, count=50):
    return [{'_id': f'{prefix}-{i}', 'title': 'contest'} for i in range(count)]


def test_contest_list_defaults_to_page_two():
    session = Session([page('x', 1)])
    assert len(list_contests(session, 'https://oj.test/', 'system')) == 1
    assert session.calls[0][0] == 'https://oj.test/d/system/contest'
    assert session.calls[0][1]['params']['page'] == 2


def test_page_count_is_relative_to_start_page():
    session = Session([page('a'), page('b'), page('c')])
    assert len(list_contests(session, 'https://oj.test', 'custom', 2, start_page=5)) == 100
    assert [call[1]['params']['page'] for call in session.calls] == [5, 6]


def test_page_one_is_selectable_and_empty_page_stops():
    session = Session([page('a'), []])
    assert len(list_contests(session, 'https://oj.test', 'd', start_page=1)) == 50
    assert [call[1]['params']['page'] for call in session.calls] == [1, 2]


def test_repeated_page_does_not_duplicate_or_loop():
    session = Session([page('a'), page('a')])
    assert len(list_contests(session, 'https://oj.test', 'd')) == 50
    assert len(session.calls) == 2


def test_http_failure_returns_no_contests():
    assert not list_contests(Session([], 403), 'https://oj.test', 'd')


@pytest.mark.parametrize('value', [0, -1, True, 1.5, 'nope'])
def test_invalid_page_rejected(value):
    with pytest.raises(ValueError):
        positive_int(value)


def test_page_config_precedence(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text('{"contest_start_page": 4}', encoding='utf-8')
    assert ConfigManager(str(path)).contest_start_page == 4
    monkeypatch.setenv('OJ_CONTEST_START_PAGE', '3')
    assert ConfigManager(str(path)).contest_start_page == 3
    assert ConfigManager(str(path), cli_overrides={'contest_start_page': 1}).contest_start_page == 1
    assert ConfigManager(str(tmp_path / 'none.json'), cli_overrides={'contest_start_page': 0}).validate()


def test_daemon_uses_requested_start_page(monkeypatch):
    import contest_daemon
    session = Session([[]])
    monkeypatch.setattr(contest_daemon, 'create_session', lambda **kw: session)
    monkeypatch.setattr(contest_daemon, 'smart_login', lambda *a: True)
    contest_daemon.check_contests('https://oj.test', ['d'], set(), '', '', start_page=7)
    assert session.calls[0][1]['params']['page'] == 7


def test_benchmark_uses_shared_start_page():
    from benchmark_solver import BenchmarkSolver
    solver = BenchmarkSolver.__new__(BenchmarkSolver)
    solver._cfg = NS(monitor_domains=['system'], contest_start_page=4)
    solver._push_notify = lambda *args: None
    solver.root = 'https://oj.test'
    solver.session = Session([page('a', 1)])
    scanned = []
    solver.scan_benchmark_ac = lambda domain, cid: scanned.append((domain, cid)) or []
    assert solver.scan_all_contests() == 0
    assert solver.session.calls[0][1]['params']['page'] == 4
    assert scanned == [('system', 'a-0')]


def test_runtime_paths_and_config_share_data_directory(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'config.json').write_text('{"contest_start_page": 6}', encoding='utf-8')
    monkeypatch.setenv('OJ_DATA_DIR', str(data))
    assert data_dir() == data and data_file('config.json') == data / 'config.json'
    assert ConfigManager().contest_start_page == 6
    from oj_common import load_config
    assert load_config()['contest_start_page'] == 6


def test_daemon_child_keeps_persistent_working_directory(tmp_path, monkeypatch):
    import contest_daemon
    monkeypatch.setenv('OJ_DATA_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(contest_daemon.subprocess, 'run', lambda *a, **kw: calls.append((a, kw)) or NS(returncode=0))
    assert contest_daemon.process_contest({'contest_url': 'https://oj.test/contest/1', 'title': 'x', 'pids': []}, '/app/contest_solver.py')
    assert calls[0][1]['cwd'] == str(tmp_path)


def test_web_saves_into_persistent_data_directory(tmp_path, monkeypatch):
    import webui
    monkeypatch.setenv('OJ_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(webui, 'AUTH_TOKEN', 'test-token')
    client = webui.app.test_client()
    response = client.post('/config', headers={'X-Auth-Token': 'test-token'}, json={'contest_start_page': 3})
    assert response.status_code == 200
    assert json.loads((tmp_path/'config.json').read_text())['contest_start_page'] == 3
    assert ConfigManager().contest_start_page == 3


def test_container_command_dispatch():
    assert command_args(['init', '--model', 'm'])[-3:] == ['init', '--model', 'm']
    assert command_args(['doctor', '--json'])[-2:] == ['doctor', '--json']
    assert command_args(['daemon']).count('--no-interactive') == 1
    assert command_args(['daemon', '--no-interactive']).count('--no-interactive') == 1
    assert command_args(['python', '-c', 'print(1)'])[-2:] == ['-c', 'print(1)']
    with pytest.raises(ValueError):
        command_args(['unknown'])


def test_auxiliary_tools_inherit_initialized_single_model(tmp_path, monkeypatch):
    from benchmark_solver import BenchmarkSolver
    from testdata_supplement import TestDataSupplement
    monkeypatch.setenv('OJ_DATA_DIR', str(tmp_path))
    for key in ('OJ_ROOT', 'OJ_USERNAME', 'OJ_PASSWORD', 'AI_MODEL', 'OJ_AI_MODEL',
                'OJ_TD_MODEL', 'OJ_BENCHMARK_MODEL'):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / 'config.json').write_text(json.dumps({
        'oj_root': 'https://configured.test', 'ai_model': 'configured-model',
        'username': 'configured-user', 'password': 'test-password'}), encoding='utf-8')
    benchmark = BenchmarkSolver()
    supplement = TestDataSupplement('https://configured.test/p/1')
    for tool in (benchmark, supplement):
        assert tool.root == 'https://configured.test'
        assert tool.model == 'configured-model'
        assert tool.username == 'configured-user'
        tool.session.close()


def test_local_execution_error_keeps_actionable_detail(tmp_path, monkeypatch):
    from local_judge import LocalJudge, CompileResult, RunResult
    judge = LocalJudge('cpp', workdir=str(tmp_path))
    monkeypatch.setattr(judge, 'compile', lambda *a, **kw: CompileResult(True, exe='main'))
    monkeypatch.setattr(judge, 'run', lambda *a, **kw: RunResult(
        exit_code=127, stderr='Permission denied: executable is on a noexec mount'))
    report = judge.check_samples('code', [{'in': '1', 'out': '1'}])
    assert report.cases[0].status == 'RE'
    assert 'noexec mount' in report.to_prompt()
