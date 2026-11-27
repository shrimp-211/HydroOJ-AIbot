from types import SimpleNamespace

import pytest

from hydroai.insight.analyzer import Analyzer
from hydroai.insight.profile import build_profile, summary_text
from hydroai.insight.selector import code_hash, pick_for_problem
from hydroai.insight.cli import _parser, _show
from hydroai.insight.service import InsightService
from hydroai.insight.store import InsightStore


class Config(dict):
    def task_token_limit(self, task):
        return 1000


class Records:
    def __init__(self, rows):
        self.rows = rows
        self.details = {r['_id']: r for r in rows}

    def iter_list(self, **kwargs):
        return iter(self.rows)

    def list(self, **kwargs):
        return {'rdocs': self.rows}

    def get(self, rid):
        return {'rdoc': self.details[rid]}


def make_service(rows, calls=None):
    records = Records(rows)
    api = SimpleNamespace(domain='test', uid=99, records=records,
        problems=SimpleNamespace(detail=lambda pid: {'pid': 'P100', 'docId': 42, 'tags': ['数组'], 'content': '题面'}),
        users=SimpleNamespace(name=lambda uid: str(uid)))
    config = Config(ai_model='test')
    return InsightService(api, config, store=InsightStore(), call_fn=calls), records


def row(rid, uid=1, pid=42, status=2, score=0, code='int main(){}'):
    return {'_id': rid, 'uid': uid, 'pid': pid, 'domainId': 'test', 'status': status,
            'score': score, 'code': code, 'submit_at': int(rid[-1])}


def test_ai_qualitative_diagnosis_persists_and_is_reported():
    from hydroai.insight.analyzer import Analysis
    answer = '{"evidence_sufficient":true,"distance_level":"关键实现待修","root_cause":"循环越界","repair_items":["修复边界"],"missing_concepts":["闭区间处理"]}'
    svc, _ = make_service([row('r1')], lambda *_: answer)
    a = svc.analyze_record('r1')
    assert a.distance is None and a.distance_level == '关键实现待修'
    assert a.repair_items == ['修复边界'] and a.missing_concepts == ['闭区间处理']
    assert Analysis.from_dict(a.to_dict()).distance_level == '关键实现待修'
    report = svc.report(1)
    assert '关键实现待修' in report and '修复边界' in report and '闭区间处理' in report
    assert '待验证' in report


def test_ai_unsupported_or_unexplained_level_stays_uncertain():
    for answer in ('{"distance_level":"仅差细节","root_cause":"边界","evidence_sufficient":true}',
                   '{"distance_level":"局部细节待修","evidence_sufficient":true}'):
        a = Analyzer(Config(ai_model='test'), call_fn=lambda *_: answer).analyze(row('r1'), {'content': '题面'})
        assert a.distance_level == '无法确定'
    answer = '{"distance_level":"关键实现待修","root_cause":"越界","evidence_sufficient":true}'
    a = Analyzer(Config(ai_model='test'), call_fn=lambda *_: answer).analyze(row('r1'), {})
    assert a.distance_level == '无法确定'


def test_default_ai_client_does_not_display_thinking(monkeypatch):
    seen = {}
    class FakeClient:
        def __init__(self, config, *, show_thinking=None):
            seen['show_thinking'] = show_thinking
        def chat(self, *args, **kwargs):
            return {'content': '{"root_cause":"检查"}'}
    monkeypatch.setattr('hydroai.ai.client.AIClient', FakeClient)
    a = Analyzer(Config(ai_model='test')).analyze(row('r1'), {})
    assert a.ai_used and seen['show_thinking'] is False


def test_objective_evidence_never_executes_and_no_distance():
    a = Analyzer(Config(ai_model='test')).analyze(
        dict(row('r1'), testCases=[{'status': 1}, {'status': 2}]), {'tags': ['数组']}, use_ai=False)
    assert a.evidence['cases_passed'] == 1
    assert a.evidence['cases_total'] == 2
    assert a.distance is None
    assert not a.missing_concepts
    assert a.error_type != '思路错误'


def test_one_event_does_not_claim_ability():
    p = build_profile([{'tag': '数组', 'at': 1, 'distance': 0, 'solved': 0, 'error_type': '实现bug'}])
    assert '薄弱' not in summary_text(p)


def test_student_context_validates_identity_and_internal_docid():
    svc, records = make_service([row('r1')])
    result = svc.student_context(1, 'P100', analyze=False)
    assert result['best_submission']['rid'] == 'r1'
    records.details['r1'] = row('r1', uid=2)
    result = svc.student_context(1, 'P100', analyze=False)
    assert result['best_submission'] is None


def test_only_same_student_reference_and_no_repeat_ai():
    calls = []
    def call(messages, model):
        calls.append(messages)
        return '{"error_type":"实现bug","root_cause":"检查边界"}'
    rows = [row('r1', code='wrong'), row('r2', uid=2, status=1, code='private secret')]
    svc, records = make_service(rows, call)
    svc.analyze_record('r1')
    svc.analyze_record('r1')
    assert len(calls) == 1
    assert 'private secret' not in str(calls)


def test_degraded_analysis_persisted_and_upgradable():
    svc, _ = make_service([row('r1')])
    first = svc.analyze_record('r1', use_ai=False)
    assert svc.store.get_analysis('r1') is not None
    assert not first.ai_used
    svc.analyzer._call = lambda *args: '{"root_cause":"边界"}'
    assert svc.analyze_record('r1').ai_used


def test_new_progress_after_analyzed_can_be_selected():
    rows = [row('r1', score=80), row('r2', score=40, code='different')]
    picked = pick_for_problem(rows, analyzed={'r1'})
    assert picked and picked[0]['_id'] == 'r2'


def test_same_code_new_record_reuses_analysis_and_preserves_evidence():
    calls = []
    def call(messages, model):
        calls.append(messages)
        return '{"root_cause":"检查循环"}'
    svc, _ = make_service([row('r1', code='same'), row('r2', score=45, code='same')], call)
    svc.analyze_record('r1')
    second = svc.analyze_record('r2')
    assert len(calls) == 1
    assert second.evidence['oj_score'] == 45
    assert second.evidence['reused_analysis_rid'] == 'r1'


def test_reference_must_match_domain_uid_pid_and_status():
    prompts = []
    def call(messages, model):
        prompts.append(str(messages))
        return '{"root_cause":"检查"}'
    own = row('r3', status=1, code='own solution')
    foreign = row('r2', uid=2, status=1, code='secret other')
    svc, _ = make_service([row('r1', code='wrong'), foreign, own], call)
    svc.analyze_record('r1')
    assert 'own solution' in prompts[0]
    assert 'secret other' not in prompts[0]


def test_mismatched_list_rows_are_ignored_and_history_persisted():
    svc, _ = make_service([row('r1'), row('r2', status=1), row('r3', uid=2),
                           dict(row('r4'), domainId='other'), row('r5', pid=99)])
    ctx = svc.student_context(1, 'P100', analyze=False)
    assert ctx['attempts'] == 2 and ctx['solved']
    history = svc.profile(1)['submission_history']
    assert history == {'total': 2, 'accepted': 1, 'failed': 1, 'solved_pids': ['42']}


def test_hash_does_not_ignore_string_literals_or_indentation():
    assert code_hash('print("a b")') != code_hash('print("ab")')
    assert code_hash('if x:\n  f()') != code_hash('if x:\nf()')
    assert code_hash('abc  \r\n') == code_hash('abc\n')


def test_cli_formats_unknown_distance_and_domain_flag():
    args = _parser().parse_args(['--domain', 'test', 'analyze', 'r1'])
    assert args.domain == 'test'
    a = Analyzer(Config(ai_model='test')).analyze(row('r1'), {}, use_ai=False)
    assert '距离' not in _show(a)


def test_same_rid_other_domain_cannot_overwrite_existing_data():
    store = InsightStore()
    analysis = Analyzer(Config(ai_model='test')).analyze(row('r1'), {}, use_ai=False).to_dict()
    store.save_analysis('first', analysis)
    with pytest.raises(ValueError):
        store.save_analysis('second', analysis)
    assert store.get_analysis('r1', 'first') == analysis


def test_cached_legacy_distance_and_capability_claim_are_not_returned():
    svc, _ = make_service([row('r1')])
    legacy = {'rid': 'r1', 'uid': 1, 'pid': '42', 'status': 2, 'score': 0,
              'ai_used': True, 'distance': 93, 'distance_level': '仅差细节',
              'missing_concepts': ['不会数组'], 'root_cause': '下标越界'}
    svc.store.save_analysis('test', legacy)
    a = svc.analyze_record('r1')
    assert a.distance is None and a.missing_concepts == []
    assert a.root_cause.startswith('模型推测')
    assert svc.store.get_analysis('r1')['distance'] == 93


def test_missing_or_mismatched_domain_cannot_supply_student_code():
    for domain in (None, 'other'):
        bad = row('r1')
        if domain is None:
            bad.pop('domainId')
        else:
            bad['domainId'] = domain
        svc, _ = make_service([bad])
        assert svc.student_context(1, 'P100', analyze=False)['best_submission'] is None


