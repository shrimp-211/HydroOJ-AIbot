"""Agent 求解层测试：本地评测工具 + agent 循环 + 配置接线。"""
import types
from pathlib import Path

import pytest

from local_judge import (CaseResult, CheckReport, CompileResult, LocalJudge,
                         StressResult, detect_toolchain, outputs_match,
                         parse_time_limit)
from oj_samples import extract_samples
from oj_solver import AIClient, Config, OJClient, SolverOrchestrator, build_cli_overrides
from solver_agent import SolverAgent, extract_labeled_blocks

HAS_CPP = detect_toolchain("cpp") is not None
cpp_only = pytest.mark.skipif(not HAS_CPP, reason="本机没有 C++ 编译器")

FENCE = "```"
# 裸代码给本地评测器；同一份内容包上围栏后交给（假）模型
GOOD_CODE = ('#include <cstdio>\nint main(){int a,b;'
             'if(scanf("%d %d",&a,&b)!=2)return 0;printf("%d\\n",a+b);}')
BAD_CODE = ('#include <cstdio>\nint main(){int a,b;'
            'scanf("%d %d",&a,&b);printf("%d\\n",a-b);}')
BROKEN_CODE = "int main(){ this is not c++ }"
GOOD = f"```cpp\n{GOOD_CODE}\n```"
BAD = f"```cpp\n{BAD_CODE}\n```"
BROKEN = f"```cpp\n{BROKEN_CODE}\n```"
GOOD_WITH_PROSE = f"解题思路：直接读入两个数相加。\n{GOOD}"
BRUTE = ('#include <cstdio>\nint main(){int a,b;'
         'if(scanf("%d %d",&a,&b)!=2)return 0;printf("%d\\n",a+b);}')
GEN = ('#include <cstdio>\n#include <cstdlib>\n'
       'int main(){srand(1234);printf("%d %d\\n",rand()%50,rand()%50);}')


def _cfg(tmp_path, **over):
    fixture = Path(__file__).with_name("config.json")
    base = {"agent_enabled": True}
    base.update(over)
    return Config(config_path=str(fixture if fixture.exists() else tmp_path / "none.json"),
                  cli_overrides=base)


class TestSampleExtraction:
    def test_numbered_fences(self):
        text = f"{FENCE}input1\n1 2\n{FENCE}\n{FENCE}output1\n3\n{FENCE}"
        assert extract_samples(text) == [{"in": "1 2", "out": "3", "n": 1}]

    def test_unnumbered_fences(self):
        text = f"{FENCE}input\n1 2\n{FENCE}\n{FENCE}output\n3\n{FENCE}"
        assert extract_samples(text) == [{"in": "1 2", "out": "3", "n": 1}]

    def test_labeled_sections(self):
        text = ("## 样例输入 #1\n" + FENCE + "\n1 2\n" + FENCE +
                "\n## 样例输出 #1\n" + FENCE + "\n3\n" + FENCE)
        assert extract_samples(text) == [{"in": "1 2", "out": "3", "n": 1}]

    def test_plain_text_labels(self):
        assert extract_samples("输入：\n5 7\n输出：\n12\n") == \
            [{"in": "5 7", "out": "12", "n": 1}]

    def test_no_samples(self):
        assert extract_samples("没有测试数据") == []


class TestOutputCompare:
    def test_trailing_whitespace_ignored(self):
        assert outputs_match("3\n", "3")[0]
        assert outputs_match("1 2  \n3\n\n", "1 2\n3")[0]

    def test_token_equal_but_format_differs(self):
        passed, why = outputs_match("1 2 3", "1  2\n3")
        assert passed and "格式" in why

    def test_really_different(self):
        assert not outputs_match("3", "-1")[0]

    def test_time_limit_parsing(self):
        assert parse_time_limit("1000ms") == 1.0
        assert parse_time_limit("2s") == 2.0
        assert parse_time_limit("500") == 0.5
        assert parse_time_limit("") == 1.0


@cpp_only
class TestLocalJudgeCpp:
    def test_compile_error_caught(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        rep = j.check_samples(BROKEN_CODE, [{"in": "1 2", "out": "3", "n": 1}])
        assert not rep.compile_ok and "编译失败" in rep.to_prompt()

    def test_samples_pass_and_fail(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        samples = [{"in": "1 2", "out": "3", "n": 1}, {"in": "5 7", "out": "12", "n": 2}]
        assert j.check_samples(GOOD_CODE, samples).ok
        bad = j.check_samples(BAD_CODE, samples)
        assert not bad.ok and [c.status for c in bad.cases] == ["WA", "WA"]
        text = bad.to_prompt()
        assert "-1" in text and "3" in text

    def test_compile_only_when_no_samples(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        rep = j.check_samples(GOOD_CODE, [])
        assert rep.compile_ok and rep.cases == []

    def test_stress_detects_wrong_solution(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        st = j.stress(BAD_CODE, BRUTE, GEN, rounds=5)
        assert st.applicable and not st.ok
        assert st.counterexample and st.counterexample["stdin"].strip()

    def test_stress_passes_correct_solution(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        st = j.stress(GOOD_CODE, BRUTE, GEN, rounds=5)
        assert st.applicable and st.ok

    def test_broken_reference_does_not_block(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path))
        st = j.stress(GOOD_CODE, BROKEN_CODE, GEN, rounds=3)
        assert st.applicable is False and "暴力解编译失败" in st.detail

    def test_timeout_is_reported(self, tmp_path):
        j = LocalJudge("cpp", workdir=str(tmp_path), run_timeout=2)
        loop = "#include <cstdio>\nint main(){for(;;){}}"
        rep = j.check_samples(loop, [{"in": "", "out": "x", "n": 1}])
        assert [c.status for c in rep.cases] == ["TLE"]


class TestLocalJudgeDegrade:
    def test_no_toolchain(self, monkeypatch, tmp_path):
        monkeypatch.setattr("local_judge.detect_toolchain", lambda ext: None)
        j = LocalJudge("cpp", workdir=str(tmp_path))
        assert not j.available
        rep = j.check_samples(GOOD_CODE, [{"in": "1 2", "out": "3", "n": 1}])
        assert not rep.compile_ok and "编译器" in rep.compile_message

    def test_unknown_language(self, tmp_path):
        assert not LocalJudge("brainfuck", workdir=str(tmp_path)).available


class _ScriptedAI(AIClient):
    """按提示词内容返回预设内容，模拟模型。"""

    def __init__(self, cfg, codes, brute=BRUTE, gen=GEN):
        super().__init__(cfg)
        self.codes = list(codes)
        self.brute, self.gen = brute, gen
        self.calls = []
        self.chat_calls = []      # [(model, prompt)]
        self.editorial = ""       # 题解生成时的返回（空则用通用占位）
        self._code_idx = 0

    def _call_ai(self, user_prompt, system_msg, use_stream=False, images=None,
                 model="", effort=""):
        """原三层循环走的是 _call_ai，这里复用同一套脚本内容。"""
        r = self.chat([{"role": "user", "content": user_prompt}], model=model)
        content = r["content"]
        return {"solution_md": content,
                "code": self._extract_code(content, self.config.lang_ext),
                "raw": content, "usage": r["usage"], "elapsed_s": r["elapsed_s"],
                "cost": r["cost"], "model": model or self.config["ai_model"]}

    def chat(self, messages, model="", use_stream=False, max_tokens=0, effort=""):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        self.chat_calls.append((model, prompt))
        # 用各模板独有的开头判别调用类型（注意 implement/repair 会嵌入 plan 正文，
        # 不能拿 plan 里的内容当判别标记）
        if "写作要求" in prompt:                    # editorial_write
            content = self.editorial or "## 题意\n占位\n## 代码\n```cpp\nx\n```"
        elif "需要修正的问题" in prompt:            # editorial_fix
            content = self.editorial or "## 题意\n占位\n"
        elif "提供**暴力解**" in prompt:
            content = f"```brute\n{self.brute}\n```\n```gen\n{self.gen}\n```"
        elif "先分析题目，给出解题方案" in prompt:
            content = "问题本质：求和。算法：直接读入输出。"
        else:
            # 实现与修正都从这里取：按顺序给出下一份代码
            content = self.codes[min(self._code_idx, len(self.codes) - 1)]
            self._code_idx += 1
        return {"content": content,
                "usage": {"input": 10, "output": 20, "total": 30, "cache_hit": 0},
                "cost": 0.01, "elapsed_s": 0.1,
                "model": model or self.config["ai_model"]}

    @property
    def repair_prompts(self):
        return [c for c in self.calls if "没通过验证" in c]


class _FakeJudge:
    """可编排的本地评测器：按队列返回检查结果。"""

    available = True

    def __init__(self, reports=(), stresses=()):
        self._reports = list(reports)
        self._stresses = list(stresses)
        self.checked = []

    def describe(self):
        return "fake-judge"

    def compile(self, code, tag="main"):
        return CompileResult(True, exe="x")

    def check_samples(self, code, samples, timeout=None):
        self.checked.append(code)
        return self._reports.pop(0) if self._reports else _ok_report()

    def stress(self, code, ref, gen, rounds=30, timeout=None):
        return self._stresses.pop(0) if self._stresses else StressResult(
            rounds, None, "", applicable=True)


def _ok_report():
    return CheckReport(compile_ok=True, cases=[CaseResult(1, "AC")])


def _ce_report(msg="error: expected ';' before '}'"):
    return CheckReport(compile_ok=False, compile_message=msg)


def _wa_report():
    return CheckReport(compile_ok=True, cases=[CaseResult(
        1, "WA", stdin="1 2", expected="3", actual="-1", detail="输出不一致")])


def _stress_fail():
    return StressResult(1, {"stdin": "7 9", "expected": "16", "actual": "-2"},
                        "输出与暴力解不一致", applicable=True)


def _problem():
    return {"pid": "1", "title": "A+B",
            "content": f"{FENCE}input1\n1 2\n{FENCE}\n{FENCE}output1\n3\n{FENCE}",
            "time_limit": "1000ms", "memory_limit": "256MiB", "images": [],
            "io_method": "", "tags": [], "url": "https://oj.example/p/1"}


def _verdict(ac=True, score=100):
    return {"is_ac": ac, "score": score, "time_ms": 5.0, "memory_kb": 1000,
            "case_summary": "AC" if ac else "WA",
            "errors_text": "" if ac else "用例 3 WA", "is_system_error": False}


def _agent(tmp_path, reports, stresses=(), verdicts=(True,)):
    cfg = _cfg(tmp_path)
    ai = _ScriptedAI(cfg, [GOOD, GOOD])
    submitted = []
    queue = list(verdicts)

    def submit_fn(code):
        submitted.append(code)
        return f"rid{len(submitted)}"

    agent = SolverAgent(ai, cfg, _FakeJudge(reports, stresses),
                        submit_fn=submit_fn,
                        judge_fn=lambda rid: _verdict(queue.pop(0) if queue else True),
                        banner_fn=lambda m, e, u, c: "// banner\n")
    return agent, ai, submitted


class TestSolverAgent:
    def test_compile_error_never_submitted(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ce_report(), _ok_report()])
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac and out.submissions == 1 and len(submitted) == 1
        assert any("编译失败" in x for x in out.journal)
        assert out.local_saves >= 1
        assert "expected ';'" in ai.repair_prompts[-1]

    def test_sample_failure_feeds_diff(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_wa_report(), _ok_report()])
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac and len(submitted) == 1
        prompt = ai.repair_prompts[-1]
        assert "样例 1 WA" in prompt and "-1" in prompt and "3" in prompt

    def test_stress_counterexample_blocks_submission(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ok_report(), _ok_report()],
                                      stresses=[_stress_fail()])
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac
        assert any("对拍" in x for x in out.journal)
        assert "7 9" in ai.repair_prompts[-1]
        assert len(submitted) == 1

    def test_ac_returns_code_and_solution(self, tmp_path):
        cfg = _cfg(tmp_path)
        ai = _ScriptedAI(cfg, [GOOD_WITH_PROSE])
        agent = SolverAgent(ai, cfg, _FakeJudge([_ok_report()]),
                            submit_fn=lambda c: "rid1", judge_fn=lambda rid: _verdict(True))
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac and "int main" in out.code
        # 题解正文与原流程一致：保留模型原始输出（含代码块）
        assert "解题思路" in out.solution_md and "int main" in out.solution_md
        assert out.usage["input"] > 0 and out.cost > 0

    def test_step_budget(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ce_report()] * 10)
        out = agent.attempt(_problem(), model="m", max_steps=2)
        assert out.steps == 2
        # 全程本地失败时兜底提交最后一版（避免 OJ 上什么都没有）
        assert len(submitted) == 1

    def test_cost_cap_stops(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ce_report()] * 10)
        agent.cost_exceeded = lambda: True
        out = agent.attempt(_problem(), model="m")
        assert out.is_cost_capped and out.steps == 0

    def test_submit_budget(self, tmp_path):
        cfg = _cfg(tmp_path, agent_max_submissions=2, agent_max_steps=9)
        ai = _ScriptedAI(cfg, [GOOD])
        submitted = []
        agent = SolverAgent(
            ai, cfg, _FakeJudge([_ok_report()] * 9),
            submit_fn=lambda c: (submitted.append(c), f"rid{len(submitted)}")[1],
            judge_fn=lambda rid: _verdict(False, 0))
        out = agent.attempt(_problem(), model="m")
        assert out.submissions == 2 and len(submitted) == 2 and not out.is_ac

    def test_fallback_without_toolchain(self, tmp_path):
        cfg = _cfg(tmp_path)
        ai = _ScriptedAI(cfg, [GOOD])
        submitted = []
        agent = SolverAgent(ai, cfg, None,
                            submit_fn=lambda c: (submitted.append(c), "rid1")[1],
                            judge_fn=lambda rid: _verdict(True))
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac and out.submissions == 1 and out.steps == 1
        assert not ai.repair_prompts

    def test_judge_unavailable_degrades(self, tmp_path):
        class _Dead(_FakeJudge):
            available = False

        cfg = _cfg(tmp_path)
        agent = SolverAgent(_ScriptedAI(cfg, [GOOD]), cfg, _Dead(),
                            submit_fn=lambda c: "rid1",
                            judge_fn=lambda rid: _verdict(True))
        out = agent.attempt(_problem(), model="m")
        assert out.is_ac and out.steps == 1

    def test_stress_disabled_skips_reference_request(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ok_report()])
        out = agent.attempt(_problem(), model="m", stress_enable=False)
        assert out.is_ac and len(submitted) == 1
        assert not any("提供**暴力解**" in c for c in ai.calls)

    def test_plan_disabled(self, tmp_path):
        agent, ai, submitted = _agent(tmp_path, [_ok_report()])
        out = agent.attempt(_problem(), model="m", plan_enable=False)
        assert out.is_ac
        assert not any("先分析题目，给出解题方案" in c for c in ai.calls)

    # ── 多候选（换算法，而不是改上一版）──
    def test_multiple_candidates_tried(self, tmp_path):
        cfg = _cfg(tmp_path, solve_candidates=3, solve_repairs=0)
        ai = _ScriptedAI(cfg, [GOOD])
        submitted = []
        agent = SolverAgent(
            ai, cfg, _FakeJudge([_ce_report(), _ce_report(), _ok_report()]),
            submit_fn=lambda c: (submitted.append(c), f"rid{len(submitted)}")[1],
            judge_fn=lambda rid: _verdict(True))
        out = agent.attempt(_problem(), model="m")
        assert out.candidates_tried == 3 and out.is_ac
        # 第 2、3 条思路必须要求「换一个不同的算法角度」
        assert sum(1 for c in ai.calls if "换一个不同的算法角度" in c) == 2
        assert len(out.ideas) == 3

    def test_alternative_idea_sees_previous_failures(self, tmp_path):
        cfg = _cfg(tmp_path, solve_candidates=2, solve_repairs=0)
        ai = _ScriptedAI(cfg, [GOOD])
        agent = SolverAgent(ai, cfg,
                            _FakeJudge([_ce_report("boom: syntax error"), _ok_report()]),
                            submit_fn=lambda c: "rid1", judge_fn=lambda rid: _verdict(True))
        agent.attempt(_problem(), model="m")
        alt = [c for c in ai.calls if "换一个不同的算法角度" in c][-1]
        assert "boom: syntax error" in alt      # 上一轮的失败原因要带进新思路

    def test_keeps_best_scoring_candidate(self, tmp_path):
        cfg = _cfg(tmp_path, solve_candidates=2, solve_repairs=0)
        ai = _ScriptedAI(cfg, [GOOD, BAD])      # 第一条 30 分，第二条 0 分
        scores = [30, 0]
        agent = SolverAgent(
            ai, cfg, _FakeJudge([_ok_report(), _ok_report()]),
            submit_fn=lambda c: "rid1",
            judge_fn=lambda rid: _verdict(False, scores.pop(0)))
        out = agent.attempt(_problem(), model="m")
        assert not out.is_ac and out.best_score == 30
        assert out.code.strip() == GOOD_CODE    # 保留得分更高的那一版


class TestAgentConfig:
    def test_defaults(self, tmp_path):
        c = _cfg(tmp_path)
        assert c["agent_enabled"] is True
        assert c["agent_max_steps"] == 12
        assert c["agent_max_submissions"] == 8
        assert c["agent_stress_enable"] is True
        assert c["agent_stress_rounds"] == 30
        assert c["solve_candidates"] == 3
        assert c["solve_repairs"] == 2
        assert c["editorial_enable"] is True

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OJ_AGENT_ENABLE", "0")
        monkeypatch.setenv("OJ_AGENT_MAX_STEPS", "3")
        from config_manager import ConfigManager
        cm = ConfigManager(str(tmp_path / "none.json"))
        assert cm.cfg.agent_enabled is False
        assert cm.cfg.agent_max_steps == 3

    def test_cli_flags(self):
        args = types.SimpleNamespace(
            username=None, password=None, lang=None, api_key_env=None, base_url=None,
            model=None, base=None, cookie_jar=None, timeout=None, show_thinking=False,
            no_difficulty_detect=False, difficulty_skip_model=None,
            no_agent=True, agent_steps=2, no_stress=True)
        cli = build_cli_overrides(args)
        assert cli["agent_enabled"] is False
        assert cli["agent_max_steps"] == 2
        assert cli["agent_stress_enable"] is False

    def test_cli_defaults_untouched(self):
        args = types.SimpleNamespace(
            username=None, password=None, lang=None, api_key_env=None, base_url=None,
            model=None, base=None, cookie_jar=None, timeout=None, show_thinking=False,
            no_difficulty_detect=False, difficulty_skip_model=None,
            no_agent=False, agent_steps=None, no_stress=False)
        cli = build_cli_overrides(args)
        assert "agent_enabled" not in cli and "agent_max_steps" not in cli


@cpp_only
class TestAgentIntegration:
    """真实 g++ + 假 OJ/模型：验证主流程确实「本地通过后才提交」。"""

    class _OJ(OJClient):
        def __init__(self, cfg, verdicts):
            super().__init__(cfg)
            self._verdicts = list(verdicts)
            self.submitted = []
            self.solutions = []

        def login(self, max_retries=3):
            return True

        def get_problem(self, pid):
            return _problem()

        def submit_code(self, pid, code, contest_id=""):
            self.submitted.append(code)
            return f"rid{len(self.submitted)}"

        def verify_submission(self, rid):
            return self._verdicts.pop(0) if self._verdicts else _verdict(False, 0)

        def post_solution(self, pid, md):
            self.solutions.append(md)
            return "psid1"

    def _solve(self, tmp_path, monkeypatch, codes, agent_enabled=True):
        monkeypatch.chdir(tmp_path)
        cfg = _cfg(tmp_path, agent_enabled=agent_enabled, difficulty_detect_enable=False)
        ai = _ScriptedAI(cfg, codes)
        oj = self._OJ(cfg, [_verdict(True)])
        result = SolverOrchestrator(oj, ai, cfg).solve(
            "1", submit=True, post=True, accumulate=False)
        return result, oj, ai

    def test_bad_version_rejected_locally(self, tmp_path, monkeypatch):
        result, oj, ai = self._solve(tmp_path, monkeypatch, [BAD, GOOD_WITH_PROSE])
        assert result["is_ac"] is True
        assert len(oj.submitted) == 1, "样例不过的那一版不应该被提交"
        assert ai.repair_prompts, "应把样例差异回喂给模型"
        assert "int main" in oj.submitted[0]
        assert oj.solutions and "int main" in oj.solutions[0]   # 题解正文仍带代码

    def test_ce_rejected_locally(self, tmp_path, monkeypatch):
        result, oj, ai = self._solve(tmp_path, monkeypatch, [BROKEN, GOOD_WITH_PROSE])
        assert result["is_ac"] is True
        assert len(oj.submitted) == 1
        prompt = ai.repair_prompts[-1]
        # 编译器报错原文要出现在反馈里（不同 gcc 版本措辞不同，只断言 error 标记）
        assert "本地编译失败" in prompt and "error:" in prompt
        assert BROKEN_CODE.splitlines()[0] in prompt

    def test_pipeline_used_when_agent_disabled(self, tmp_path, monkeypatch):
        result, oj, ai = self._solve(tmp_path, monkeypatch, [GOOD_WITH_PROSE],
                                     agent_enabled=False)
        assert result["is_ac"] is True
        assert ai.repair_prompts == []
        assert not any("提供**暴力解**" in c for c in ai.calls)
        assert len(oj.submitted) == 1
