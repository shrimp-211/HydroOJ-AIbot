import logging
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Timeout

from hydroai.monitor import daemon
from hydroai.solve import contest, benchmark, testdata


@pytest.fixture
def batch_session(monkeypatch):
    from hydroai.oj import common

    session = Mock()
    session.cookies = []
    monkeypatch.setattr(common, 'setup_logging', lambda **kw: None)
    monkeypatch.setattr(contest, 'load_dotenv', lambda: None)
    monkeypatch.setattr(contest, 'ConfigManager',
                        lambda: SimpleNamespace(username='u', password='p', oj_root='https://oj.example.test'))
    monkeypatch.setattr(contest, 'create_session', lambda **kw: session)
    monkeypatch.setattr(contest, 'smart_login', lambda *a: True)
    monkeypatch.setattr(contest, 'fetch_user_id', lambda *a: 1)
    monkeypatch.setattr(contest, '_push_session', None)
    return session


@pytest.fixture
def readonly_batch(monkeypatch, batch_session):
    for name in ('attend_contest', 'solve_one', '_push', '_run_supplement', 'data_file'):
        monkeypatch.setattr(contest, name, Mock(side_effect=AssertionError(name)))
    return batch_session


def _batch_response(status, data=None):
    json = Mock(side_effect=data) if isinstance(data, Exception) else Mock(return_value=data)
    return SimpleNamespace(status_code=status, json=json)


def test_contest_dry_run_discovers_without_writes(monkeypatch, caplog, readonly_batch):
    readonly_batch.get.side_effect = [
        _batch_response(200, {'tdoc': {'title': 'Contest', 'pids': [101, 102]}}),
        _batch_response(200, {'tdoc': {'title': 'Training'}, 'pids': [201]}),
    ]
    monkeypatch.setattr(sys, 'argv', ['contest', 'https://oj.example.test/d/system/contest/abc',
                                      'https://oj.example.test/training/def', 'https://oj.example.test/p/301', '--dry-run'])
    with caplog.at_level(logging.INFO):
        contest.main()
    assert all(str(pid) in caplog.text for pid in [101, 102, 201, 301])
    assert readonly_batch.get.call_count == 2
    readonly_batch.post.assert_not_called()


@pytest.mark.parametrize(('path', 'response', 'reason'), [
    ('/d/system/contest/abc', _batch_response(403), 'HTTP 403'),
    ('/training/def', _batch_response(404), 'HTTP 404'),
    ('/d/system/contest/abc', _batch_response(200, {'tdoc': {'title': 'Contest'}}), '题目列表'),
    ('/training/def', _batch_response(200, ValueError('not JSON')), 'JSON'),
    ('/training/def', Timeout, 'Timeout'),
])
def test_contest_dry_run_reports_failed_target_and_continues(monkeypatch, caplog, readonly_batch, path, response, reason):
    readonly_batch.get.side_effect = [response]
    monkeypatch.setattr(sys, 'argv', ['contest', 'https://oj.example.test' + path,
                                      'https://oj.example.test/p/301', '--dry-run'])
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit) as exc:
        contest.main()
    assert exc.value.code == 1
    assert reason in caplog.text
    assert '/p/301' in caplog.text
    readonly_batch.post.assert_not_called()


def test_contest_dry_run_allows_empty_problem_lists(monkeypatch, caplog, readonly_batch):
    readonly_batch.get.side_effect = [
        _batch_response(200, {'tdoc': {'pids': []}}), _batch_response(200, {'pids': []}),
    ]
    monkeypatch.setattr(sys, 'argv', ['contest', 'https://oj.example.test/d/system/contest/abc',
                                      'https://oj.example.test/training/def', '--dry-run'])
    with caplog.at_level(logging.INFO):
        contest.main()
    assert caplog.text.count('pids=[]') == 2
    readonly_batch.post.assert_not_called()


def test_contest_normal_run_reports_discovery_failure_after_valid_work(monkeypatch, batch_session, tmp_path):
    batch_session.get.return_value = _batch_response(404)
    batch_session.post.return_value = _batch_response(200)
    monkeypatch.setattr(contest, '_has_testdata', lambda *a: True)
    monkeypatch.setattr(contest, '_push', Mock())
    solve = Mock(return_value={'ok': True, 'is_ac': True, 'time_ms': 1, 'score': 100})
    monkeypatch.setattr(contest, 'solve_one', solve)
    monkeypatch.setattr(contest, 'data_file', lambda *a: tmp_path / 'cookies.json')
    monkeypatch.setattr(sys, 'argv', ['contest', 'https://oj.example.test/d/system/contest/abc',
                                      'https://oj.example.test/p/301', '--force', '--no-submit'])
    with pytest.raises(SystemExit) as exc:
        contest.main()
    assert exc.value.code == 1
    solve.assert_called_once_with('https://oj.example.test/d/system/p/301', str(tmp_path / 'cookies.json'), False, '', True)


def test_submit_reports_domain_record_url(caplog):
    from hydroai.oj import client as oj_client

    config = {'oj_root': 'https://oj.test', 'oj_base': 'https://oj.test/d/contest',
              'cookie_jar': '', 'verify_timeout': 1, 'lang': 'cc.cc14o2'}
    oj = oj_client.OJClient(config)
    oj.session = Mock()
    oj.session.post.return_value = SimpleNamespace(
        status_code=302, headers={'Location': 'https://oj.test/d/contest/record/abc123'})
    with caplog.at_level(logging.INFO):
        assert oj.submit_code('1000', 'int main(){}', contest_id='c1') == 'abc123'
    assert 'https://oj.test/d/contest/record/abc123' in caplog.text
    assert 'https://oj.test/record/abc123' not in caplog.text


def test_process_contest_preserves_environment(monkeypatch, tmp_path):
    monkeypatch.delenv('OJ_USERNAME', raising=False)
    monkeypatch.delenv('OJ_PASSWORD', raising=False)
    monkeypatch.setenv('OJ_ROOT', 'https://configured')
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(daemon.subprocess, 'run', run)
    monkeypatch.setattr(daemon, 'data_dir', lambda *a: tmp_path)
    assert daemon.process_contest({'contest_url': 'https://other/d/x/contest/c', 'title': 'C', 'pids': []}, 'solver.py')
    env = run.call_args.kwargs['env']
    assert 'OJ_USERNAME' not in env and 'OJ_PASSWORD' not in env
    assert env['OJ_ROOT'] == 'https://configured'


def test_daemon_once_waits_for_scan(monkeypatch, tmp_path):
    from hydroai.config import manager
    from hydroai.usage import dashboard
    cfg = SimpleNamespace(repair=lambda: [], validate=lambda: [], oj_root='https://oj', username='u', password='p',
                          monitor_domains=['system'], monitor_state_file=str(tmp_path / 'state.json'),
                          monitor_interval=30, contest_start_page=1, msg_push_events={})
    monkeypatch.setattr(manager, 'ConfigManager', lambda: cfg)
    monkeypatch.setattr(dashboard, 'Dashboard', lambda: object())
    monkeypatch.setattr(daemon, 'load_dotenv', lambda: None)
    monkeypatch.setattr(daemon, '_script_dir', lambda: tmp_path)
    (tmp_path / 'contest_solver.py').touch()
    done = threading.Event()
    pending = {'domain': 'system', 'cid': 'abc', 'title': 'C', 'pids': [1],
               'contest_url': 'https://oj/d/system/contest/abc'}
    def scan(*args, **kwargs):
        return [pending]
    def run_child(args, **kwargs):
        if args[1].endswith('contest_solver.py'):
            done.wait(0.15)
            done.set()
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(daemon.subprocess, 'run', run_child)
    monkeypatch.setattr(daemon, 'data_dir', lambda *a: tmp_path)
    monkeypatch.setattr(daemon, 'check_contests', scan)
    monkeypatch.setattr(daemon.time, 'sleep', lambda *_: None)
    monkeypatch.setattr(sys, 'argv', ['daemon', '--once', '--no-msg-backend'])
    daemon.main()
    assert done.is_set()
    assert daemon.load_state(cfg.monitor_state_file) == {'abc'}


@pytest.mark.parametrize('cls,attr,env', [(benchmark.BenchmarkSolver, 'benchmark_model', 'OJ_BENCHMARK_MODEL'),
                                         (testdata.TestDataSupplement, 'testdata_model', 'OJ_TD_MODEL')])
def test_worker_uses_resolved_model_and_config(monkeypatch, cls, attr, env):
    from hydroai.config import manager
    from hydroai.ai import client
    cfg = SimpleNamespace(oj_root='https://oj', username='u', password='p', ai_model='fallback',
                          benchmark_model='resolved-benchmark', testdata_model='resolved-testdata',
                          cfg=SimpleNamespace(benchmark_model='raw', testdata_model='raw'),
                          get=lambda *args: [])
    monkeypatch.setenv(env, 'environment')
    monkeypatch.setattr(manager, 'ConfigManager', lambda: cfg)
    monkeypatch.setattr(benchmark, 'load_dotenv', lambda: None)
    monkeypatch.setattr(testdata, 'load_dotenv', lambda: None)
    constructor = Mock(return_value=object())
    monkeypatch.setattr(client, 'AIClient', constructor)
    worker = cls('https://oj/p/1') if cls is testdata.TestDataSupplement else cls()
    assert worker.model == getattr(cfg, attr)
    (worker._get_ai_client() if cls is testdata.TestDataSupplement else worker._get_ai())
    constructor.assert_called_once_with(cfg)
    setattr(cfg, attr, '')
    fallback = cls('https://oj/p/1') if cls is testdata.TestDataSupplement else cls()
    explicit = cls('https://oj/p/1', model='explicit') if cls is testdata.TestDataSupplement else cls(model='explicit')
    assert fallback.model == 'fallback'
    assert explicit.model == 'explicit'


def test_contest_normal_discovery_attends_before_read(monkeypatch):
    actions = []
    monkeypatch.setattr(contest, 'attend_contest', lambda *a: actions.append('attend'))
    session = Mock()
    def get(*args, **kwargs):
        actions.append('read')
        return SimpleNamespace(status_code=200, json=lambda: {'tdoc': {'title': 'C', 'pids': [42]}})
    session.get.side_effect = get
    url = 'https://oj/d/system/contest/abc'
    info = contest.parse_contest_or_problem(url)
    assert list(contest.discover_problems(session, url, info)) == [('https://oj', 'system', 42, 'C', 'abc')]
    assert actions == ['attend', 'read']
