"""旧工作流到共享 OJ 资源层的离线契约。"""
from unittest.mock import Mock

import pytest
import requests

from hydroai.oj.api import OJApi, OJError

from hydroai.oj.client import OJClient
from hydroai.solve.benchmark import BenchmarkSolver
from hydroai.solve.testdata import TestDataSupplement as Supplement


def test_solver_uses_shared_submission_and_solution():
    client = OJClient({'oj_root': 'https://oj', 'oj_base': 'https://oj/d/x',
                       'cookie_jar': '', 'verify_timeout': 1, 'lang': 'cc.cc14o2'})
    api = Mock()
    api.session = client.session
    api.records.submit.return_value = 'abc123'
    api.solutions.post.return_value = 'ps1'
    client._api = api
    assert client.submit_code('1', 'code', 'contest') == 'abc123'
    api.records.submit.assert_called_once_with('1', 'code', lang='cc.cc14o2', tid='contest')
    assert client.post_solution('1', 'solution') == 'ps1'
    api.solutions.post.assert_called_once_with('1', 'solution')


def test_benchmark_solution_uses_shared_resource():
    solver = BenchmarkSolver.__new__(BenchmarkSolver)
    import threading
    solver._oj_sem = threading.Semaphore(1)
    api = Mock()
    api.solutions.post.return_value = 'ps1'
    solver._gateway = Mock(return_value=api)
    assert solver.post_solution('x', '1', 'solution') == 'ps1'
    solver._gateway.assert_called_once_with('x')
    api.solutions.post.assert_called_once_with('1', 'solution')


def test_testdata_submission_uses_shared_resource(monkeypatch):
    supplement = Supplement.__new__(Supplement)
    supplement.root = 'https://oj'
    supplement.url = 'https://oj/d/x/p/1'
    supplement._parsed = {'domain_id': 'x', 'pids': ['1']}
    supplement.session = Mock()
    api = Mock()
    api.session = supplement.session
    api.with_domain.return_value = api
    api.records.submit.return_value = 'abc123'
    api.records.get.return_value = {'rdoc': {'score': 100, 'testCases': [{'status': 1}]}}
    supplement._api = api
    monkeypatch.setattr('hydroai.solve.testdata.time.sleep', lambda _: None)
    assert supplement.submit_and_verify_ac('code', 'cpp')
    api.records.submit.assert_called_once_with('1', 'code', lang='cpp')
    api.records.get.assert_called_once_with('abc123')


def test_submission_accepts_legacy_json_rid():
    session = Mock()
    session.request.return_value = Mock(status_code=200, headers={},
                                        json=Mock(return_value={'rid': 'abc123'}))
    api = OJApi('https://oj', session=session, min_interval=0)
    assert api.records.submit('1', 'code') == 'abc123'


@pytest.mark.parametrize('status, expected', [(200, True), (302, False), (403, False)])
def test_attend_preserves_json_request_and_http_status_contract(status, expected):
    session = Mock()
    session.request.return_value = Mock(status_code=status, json=Mock(side_effect=ValueError))
    api = OJApi('https://oj', 'test', session=session, min_interval=0)
    assert api.contests.attend('training-contest') is expected
    method, url = session.request.call_args.args
    assert (method, url) == ('POST', 'https://oj/d/test/contest/training-contest')
    assert session.request.call_args.kwargs['json'] == {'operation': 'attend'}
    assert session.request.call_args.kwargs['allow_redirects'] is False


@pytest.mark.parametrize('failure', [500, requests.Timeout()])
def test_legacy_message_does_not_replay_ambiguous_failure(failure):
    session = Mock()
    if isinstance(failure, Exception):
        session.request.side_effect = failure
    else:
        session.request.return_value = Mock(status_code=failure, json=Mock(return_value={}))
    api = OJApi('https://oj', session=session, min_interval=0, retry_message_403=True)
    api._relogin = Mock(return_value=True)
    with pytest.raises(OJError):
        api.messages.send(1, 'test')
    assert session.request.call_count == 1
    api._relogin.assert_not_called()


def test_403_replay_is_limited_to_legacy_message():
    session = Mock()
    session.request.return_value = Mock(status_code=403, json=Mock(return_value={}))
    api = OJApi('https://oj', session=session, min_interval=0, retry_message_403=True)
    api._relogin = Mock(return_value=True)
    with pytest.raises(OJError):
        api.solutions.post('1', 'solution')
    assert session.request.call_count == 1
    api._relogin.assert_not_called()

