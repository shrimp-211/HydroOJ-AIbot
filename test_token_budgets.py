"""Token-budget and context regressions; no paid model calls."""
from copy import deepcopy
import pytest
from ai_client import AIClient
from ai_context import repair_messages
from config_manager import ConfigManager
from editorial import EditorialWriter, enforce_code_block
from solver_agent import SolverAgent, AttemptOutcome
from test_agent import _ScriptedAI, _problem, GOOD
from test_editorial import _FakeAI, _editorial, AC_CODE
from test_optimizations import TestSolveFlowOffline as _SolveFlow, _verdict


def test_repair_keeps_complete_statement_and_current_code(tmp_path):
    statement = 'long statement ' * 1000 + 'CRITICAL_FINAL_CONSTRAINT'
    history = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': statement}]
    for i in range(8):
        history += [{'role': 'assistant', 'content': ('old explanation and code ' * 1000)},
                    {'role': 'user', 'content': f'failed {i}'}]
    original = deepcopy(history)
    result = repair_messages(history, 'CURRENT_CODE', 'cpp', 'LATEST_COUNTEREXAMPLE')
    assert history == original and len(result) == 4
    assert result[1]['content'] == statement and 'CURRENT_CODE' in result[2]['content']
    assert 'LATEST_COUNTEREXAMPLE' in result[-1]['content'] and 'failed 7' in result[-1]['content']
    assert len(str(result)) < len(str(history)) / 4


def test_default_budget_and_explicit_model_cap(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'), cli_overrides={
        'ai_model': 'm', 'models': {'m': {'max_tokens': 100000}}})
    ai = AIClient(cm)
    assert ai._build_args([], 'm')['max_tokens'] == 16384
    assert ai._build_args([], 'm', max_tokens=4096)['max_tokens'] == 4096
    cm.set_override(models={'m': {'max_tokens': 1000}})
    assert ai._build_args([], 'm', max_tokens=4096)['max_tokens'] == 1000


def test_agent_plan_retains_tail_constraints_and_uses_plan_budget(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'))
    ai = _ScriptedAI(cm, [GOOD])
    agent = SolverAgent(ai, cm)
    problem = _problem()
    problem['content'] = 'x' * 10000 + 'TAIL_CONSTRAINT'
    agent._plan(problem, 'm', '', AttemptOutcome())
    assert 'TAIL_CONSTRAINT' in ai.calls[0]


def test_agent_budget_includes_calls_in_current_attempt(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'))
    ai = _ScriptedAI(cm, [GOOD])
    agent = SolverAgent(ai, cm, remaining_budget=lambda: 0.01)
    out = AttemptOutcome()
    assert agent._call('first', 'm', '', out)
    assert not agent._call('second', 'm', '', out)
    assert out.is_cost_capped and len(ai.calls) == 1


def test_editorial_inserts_code_without_asking_model_to_repeat_it(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'))
    prose = _editorial().split('```')[0]
    ai = _FakeAI(cm, [prose])
    writer = EditorialWriter(ai, cm)
    result = writer.write(_problem(), AC_CODE)
    assert result.count(AC_CODE) == 1
    assert result.count('## 代码') == 1
    assert '不要输出代码块' in ai.calls[0]
    assert len(ai.calls) == 1


def test_editorial_fix_sends_code_once_and_keeps_long_code(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'))
    code = 'long code ' * 1000 + 'CODE_END'
    writer = EditorialWriter(_FakeAI(cm), cm)
    prompt = writer._build_fix_prompt(enforce_code_block('## 代码', code, 'cpp'), ['issue'], code, 'cpp')
    assert prompt.count('CODE_END') == 1
    assert code in prompt


def test_accepted_first_attempt_skips_difficulty_request(tmp_path, monkeypatch):
    result, oj, ai = _SolveFlow()._run(tmp_path, monkeypatch, [_verdict(True)],
                                               cli={'editorial_enable': False})
    assert result['is_ac'] and not ai.difficulty_probes
    assert result['total_cost'] == pytest.approx(0.01)


def test_difficulty_cost_is_part_of_total(tmp_path, monkeypatch):
    result, oj, ai = _SolveFlow()._run(tmp_path, monkeypatch,
        [_verdict(False, 0), _verdict(True)], cli={'editorial_enable': False})
    assert len(ai.calls) == 3 and ai.difficulty_probes
    assert result['total_cost'] == pytest.approx(0.03)
    assert result['total_usage']['total'] == 90


def test_editorial_usage_callback_and_cap(tmp_path):
    cm = ConfigManager(str(tmp_path/'none.json'))
    ai = _FakeAI(cm, ['short', _editorial()])
    records = []
    writer = EditorialWriter(ai, cm, on_usage=records.append, cost_exceeded=lambda: bool(records))
    writer.write(_problem(), AC_CODE)
    assert len(records) == 1 and len(ai.calls) == 1
