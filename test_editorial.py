"""题解产出管线测试：结构校验、代码一致性、定向修复与主流程集成。"""
from pathlib import Path

import pytest

from editorial import (EditorialWriter, enforce_code_block, find_code_block,
                       validate_editorial)
from local_judge import detect_toolchain
from oj_solver import AIClient, Config, OJClient, SolverOrchestrator
from test_agent import _problem, _verdict

HAS_CPP = detect_toolchain("cpp") is not None
cpp_only = pytest.mark.skipif(not HAS_CPP, reason="本机没有 C++ 编译器")

FENCE = "```"
AC_CODE = ('#include <cstdio>\nint main(){int a,b;'
           'if(scanf("%d %d",&a,&b)!=2)return 0;printf("%d\\n",a+b);}')
STALE_CODE = ('#include <iostream>\nint main(){int a,b;std::cin>>a>>b;'
              'std::cout<<a-b<<std::endl;}')


def _editorial(code=AC_CODE, *, sections=("题意", "思路", "正确性说明", "复杂度分析",
                                          "实现要点与易错点", "代码"), extra=""):
    body = []
    for sec in sections:
        if sec == "代码":
            body.append(f"## 代码\n```cpp\n{code}\n```")
        elif sec == "复杂度分析":
            body.append("## 复杂度分析\n时间与空间复杂度均为 $O(1)$。")
        elif sec == "题意":
            body.append("## 题意\n读入两个整数 $a,b$，输出它们的和。")
        else:
            body.append(f"## {sec}\n按定义推导即可得到结论，这里给出足够长的讲解文本以通过长度检查，"
                        "并说明每一步的依据，避免出现空话。")
    return "\n\n".join(body) + ("\n\n" + extra if extra else "") + "\n"


class _FakeAI(AIClient):
    """按顺序返回预设内容，并记录收到的提示词。"""

    def __init__(self, cfg, responses=()):
        super().__init__(cfg)
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, model="", use_stream=False, max_tokens=0, effort=""):
        self.calls.append(messages[-1]["content"])
        content = self.responses.pop(0) if self.responses else ""
        return {"content": content, "usage": {"input": 1, "output": 1, "total": 2,
                                              "cache_hit": 0},
                "cost": 0.0, "elapsed_s": 0.0, "model": model or self.config["ai_model"]}


def _cfg(tmp_path, **over):
    fixture = Path(__file__).with_name("config.json")
    return Config(config_path=str(fixture if fixture.exists() else tmp_path / "none.json"),
                  cli_overrides=over)


class TestValidate:
    def test_clean_editorial_passes(self):
        assert validate_editorial(_editorial(), AC_CODE, "cpp") == []

    def test_missing_code_block(self):
        md = _editorial(sections=("题意", "思路", "正确性说明", "复杂度分析"))
        assert any("代码块" in i for i in validate_editorial(md, AC_CODE, "cpp"))

    def test_code_mismatch(self):
        md = _editorial(code=STALE_CODE)
        assert any("不一致" in i for i in validate_editorial(md, AC_CODE, "cpp"))

    def test_missing_sections(self):
        md = _editorial(sections=("题意", "代码"))
        issues = validate_editorial(md, AC_CODE, "cpp")
        assert any("复杂度" in i for i in issues)
        assert any("正确性" in i for i in issues)

    def test_unbalanced_math(self):
        md = _editorial(extra="这里 $a+b 少了收尾的符号")
        assert any("配对" in i for i in validate_editorial(md, AC_CODE, "cpp"))

    def test_bare_latex_command(self):
        md = _editorial(extra="比较 a \\le b 的大小关系")
        assert any("公式命令" in i for i in validate_editorial(md, AC_CODE, "cpp"))

    def test_leftover_conversation(self):
        md = _editorial(extra="以下是题解，[标签: 模拟]")
        issues = validate_editorial(md, AC_CODE, "cpp")
        assert any("对话痕迹" in i for i in issues)

    def test_too_short(self):
        assert any("过短" in i for i in validate_editorial("太短了", AC_CODE, "cpp"))


class TestEnforceCodeBlock:
    def test_replaces_stale_code(self):
        md = _editorial(code=STALE_CODE)
        fixed = enforce_code_block(md, AC_CODE, "cpp")
        assert AC_CODE in fixed and STALE_CODE not in fixed

    def test_appends_when_missing(self):
        md = _editorial(sections=("题意", "思路"))
        fixed = enforce_code_block(md, AC_CODE, "cpp")
        assert AC_CODE in fixed and "## 代码" in fixed

    def test_keeps_identical(self):
        md = _editorial()
        assert enforce_code_block(md, AC_CODE, "cpp") == md

    def test_find_code_block_returns_last(self):
        md = "```cpp\nold\n```\n中间说明\n```cpp\nnew\n```"
        assert find_code_block(md)[2] == "new"


class TestEditorialWriter:
    def _writer(self, tmp_path, responses, **over):
        cfg = _cfg(tmp_path, **over)
        ai = _FakeAI(cfg, responses)
        return EditorialWriter(ai, cfg), ai

    def test_writes_and_syncs_code(self, tmp_path):
        writer, ai = self._writer(tmp_path, [_editorial(code=STALE_CODE)])
        md = writer.write(_problem(), AC_CODE, verdict=_verdict(True))
        assert AC_CODE in md and STALE_CODE not in md   # 代码被确定性地同步
        assert "复杂度分析" in md

    def test_fix_round_on_validation_failure(self, tmp_path):
        draft = _editorial(sections=("题意", "思路", "正确性说明", "代码"))
        writer, ai = self._writer(tmp_path, [draft, _editorial()])
        md = writer.write(_problem(), AC_CODE, verdict=_verdict(True))
        assert len(ai.calls) == 2                       # 触发了一次定向修复
        assert "复杂度分析" in ai.calls[1]               # 修复提示里列出问题
        assert "复杂度分析" in md

    def test_fix_rounds_capped(self, tmp_path):
        writer, ai = self._writer(tmp_path, ["太短"] * 5, editorial_fix_rounds=1)
        writer.write(_problem(), AC_CODE, verdict=_verdict(True))
        assert len(ai.calls) == 2                       # 1 次生成 + 1 次修复

    def test_empty_response_falls_back(self, tmp_path):
        writer, ai = self._writer(tmp_path, [])
        assert writer.write(_problem(), AC_CODE, fallback="RAW") == "RAW"

    def test_disabled_returns_fallback(self, tmp_path):
        writer, ai = self._writer(tmp_path, [_editorial()], editorial_enable=False)
        assert writer.write(_problem(), AC_CODE, fallback="RAW") == "RAW"
        assert ai.calls == []

    def test_prompt_carries_solve_journal(self, tmp_path):
        writer, ai = self._writer(tmp_path, [_editorial()])
        writer.write(_problem(), AC_CODE, verdict=_verdict(True),
                     journal=["第1轮：样例 1 WA", "第2轮：对拍反例"])
        assert "样例 1 WA" in ai.calls[0] and "对拍反例" in ai.calls[0]


class TestEditorialConfig:
    def test_editorial_model_used(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = _cfg(tmp_path, agent_enabled=True, difficulty_detect_enable=False,
                   editorial_model="glm-5.2", editorial_fix_rounds=0)
        from test_agent import GOOD_WITH_PROSE, _ScriptedAI
        ai = _ScriptedAI(cfg, [GOOD_WITH_PROSE])
        ai.editorial = _editorial()            # 题解生成时的返回

        class _OJ(OJClient):
            def __init__(self, c):
                super().__init__(c)
                self.submitted = []
                self.solutions = []

            def login(self, max_retries=3):
                return True

            def get_problem(self, pid):
                return _problem()

            def submit_code(self, pid, code, contest_id=""):
                self.submitted.append(code)
                return "rid1"

            def verify_submission(self, rid):
                return _verdict(True)

            def post_solution(self, pid, md):
                self.solutions.append(md)
                return "psid1"

        oj = _OJ(cfg)
        result = SolverOrchestrator(oj, ai, cfg).solve("1", submit=True, post=True,
                                                       accumulate=False)
        assert result["is_ac"] is True
        editorial_models = [m for m, p in ai.chat_calls if "写作要求" in p]
        assert editorial_models == ["glm-5.2"], "题解应当用 editorial_model 指定的模型生成"
        assert oj.solutions and "复杂度分析" in oj.solutions[0]
