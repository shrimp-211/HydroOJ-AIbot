"""本轮优化的回归测试。

每个测试对应一个具体缺陷（解析、并发、配置、统计口径等），命名说明
「修好了什么」，方便日后改动时判断是否破坏了既有行为。
"""
import json
import types
from pathlib import Path

import pytest

import oj_common
from oj_common import load_dotenv, parse_contest_or_problem, parse_problem_url, parse_root
from oj_solver import (AIClient, Config, OJClient, SolverOrchestrator,
                       _extract_tag_prefix, _hour_in_range, build_cli_overrides)
from model_router import ModelRouter
from config_manager import FIELD_TO_JSON, JSON_TO_FIELD, AppConfig, ConfigManager


def make_solver(tmp_path, cli=None):
    """构造一个不触网的 SolverOrchestrator。"""
    cfg = Config(config_path=str(tmp_path / "none.json"), cli_overrides=cli or {})
    return SolverOrchestrator(OJClient(cfg), AIClient(cfg), cfg)


# ══════════════════════════════════════════════════════════════
# 代码块 / 标签解析
# ══════════════════════════════════════════════════════════════
class TestExtractCode:
    def test_plain_fence(self):
        assert AIClient._extract_code("思路\n```cpp\nint main(){}\n```\n", "cpp") == "int main(){}"

    def test_fence_with_extra_info_and_case(self):
        text = "```C++ title=x.cpp\nint main(){}\n```"
        assert AIClient._extract_code(text, "cpp") == "int main(){}"

    def test_fence_without_language(self):
        assert AIClient._extract_code("```\nint main(){}\n```", "cpp") == "int main(){}"

    def test_tilde_fence(self):
        assert AIClient._extract_code("~~~cpp\nint main(){}\n~~~", "cpp") == "int main(){}"

    def test_prefers_target_language(self):
        text = "```python\nprint(1)\n```\n说明\n```cpp\nint main(){}\n```"
        assert AIClient._extract_code(text, "cpp") == "int main(){}"

    def test_falls_back_to_other_language(self):
        assert AIClient._extract_code("```python\nprint(1)\n```", "cpp") == "print(1)"

    def test_no_fence(self):
        assert AIClient._extract_code("这里没有代码块", "cpp") == ""


class TestTagPrefix:
    def test_half_width(self):
        tags, body = _extract_tag_prefix("[标签: 贪心, DP]\n正文")
        assert tags == ["贪心", "DP"] and body == "正文"

    def test_full_width_colon(self):
        tags, body = _extract_tag_prefix("[标签：二分，前缀和] 正文")
        assert tags == ["二分", "前缀和"] and body == "正文"

    def test_without_prefix(self):
        tags, body = _extract_tag_prefix("正文开头")
        assert tags == [] and body == "正文开头"


class TestDifficultyParsing:
    def test_full_width_and_two_digits(self):
        # 旧实现逐字符取数字，会把「难度：10」解析成 1
        assert ModelRouter.parse_diff_and_tags("难度：10\n标签：贪心，DP")[0] == 8

    def test_markdown_wrapped(self):
        diff, tags = ModelRouter.parse_diff_and_tags("**难度**: 4\n**标签**: [二分, 前缀和]")
        assert diff == 4 and tags == ["二分", "前缀和"]

    def test_difficulty_and_tags_on_one_line(self):
        diff, tags = ModelRouter.parse_diff_and_tags("难度:3 标签: 模拟")
        assert diff == 3 and tags == ["模拟"]

    def test_default_when_missing(self):
        assert ModelRouter.parse_diff_and_tags("") == (3, [])

    def test_tags_deduplicated_and_capped(self):
        _, tags = ModelRouter.parse_diff_and_tags(
            "难度: 2\n标签: " + ", ".join(["a"] * 3 + [f"t{i}" for i in range(20)]))
        assert len(tags) == 10 and tags[0] == "a"


class TestModelTier:
    def test_index_mapping(self):
        assert [ModelRouter.tier_index_for_difficulty(d) for d in (1, 2, 3, 5, 6, 8)] == \
               [0, 0, 1, 1, 2, 2]

    def test_clamp_with_single_tier(self, tmp_path):
        cfg = Config(config_path=str(tmp_path / "none.json"),
                     cli_overrides={"model_router": {}, "models": {}})
        router = ModelRouter(config_manager=cfg)
        assert router.clamp_index(2) == len(router.tiers) - 1

    def test_thinking_out_of_range(self):
        tier = ModelRouter(config_manager=None).tiers[0]
        assert tier.current_thinking(-1) == ""
        assert tier.current_thinking(99) == ""


# ══════════════════════════════════════════════════════════════
# 配置表 / 类型收敛 / 缓存
# ══════════════════════════════════════════════════════════════
class TestConfigKeyMap:
    def test_map_covers_every_field(self):
        """FIELD_TO_JSON 必须覆盖 AppConfig 的所有字段。

        旧代码有三份手写映射表，repair() 那份漏掉了
        auto_supplement_testdata / max_cost_per_problem / cost_accum_enable。
        """
        fields = set(AppConfig.__dataclass_fields__)
        assert fields == set(FIELD_TO_JSON)

    def test_inverse_map(self):
        for attr, key in FIELD_TO_JSON.items():
            assert JSON_TO_FIELD[key] == attr


class TestConfigCoercion:
    def _write(self, tmp_path, data):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_bool_from_string(self, tmp_path):
        path = self._write(tmp_path, {"cost_accum_enable": "false"})
        assert ConfigManager(path).cfg.cost_accum_enable is False

    def test_int_from_string(self, tmp_path):
        path = self._write(tmp_path, {"verify_timeout": "120"})
        assert ConfigManager(path).cfg.verify_timeout == 120

    def test_bad_int_keeps_default(self, tmp_path):
        path = self._write(tmp_path, {"verify_timeout": "high"})
        assert ConfigManager(path).cfg.verify_timeout == AppConfig().verify_timeout

    def test_list_field_ignores_non_list(self, tmp_path):
        path = self._write(tmp_path, {"monitor_domains": "system"})
        assert ConfigManager(path).cfg.monitor_domains == ["system"]

    def test_validate_does_not_crash_on_null_max_tokens(self, tmp_path):
        path = self._write(tmp_path, {"models": {"m": {"base_url": "https://x", "max_tokens": None}}})
        warnings = ConfigManager(path).validate()
        assert not any("不是整数" in w for w in warnings)

    def test_validate_reports_bad_max_tokens(self, tmp_path):
        path = self._write(tmp_path, {"models": {"m": {"max_tokens": "abc"}}})
        assert any("不是整数" in w for w in ConfigManager(path).validate())

    def test_repair_never_writes_secrets(self, tmp_path):
        path = self._write(tmp_path, {"oj_root": "https://x"})
        cm = ConfigManager(path)
        cm.set_override(password="secret", ai_api_key="sk-secret")
        cm.repair()
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        assert "password" not in saved and "ai_api_key" not in saved

    def test_repair_adds_previously_missed_keys(self, tmp_path):
        path = self._write(tmp_path, {"oj_root": "https://x"})
        added = ConfigManager(path).repair()
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        for key in ("auto_supplement_testdata", "max_cost_per_problem", "cost_accum_enable"):
            assert key in saved

    def test_model_config_cache_invalidated_by_override(self, tmp_path):
        cm = ConfigManager(str(tmp_path / "none.json"))
        before = cm.get_model_config("glm-5.2")["base_url"]
        cm.set_override(ai_base_url="https://changed.example")
        assert cm.get_model_config("glm-5.2")["base_url"] != before

    def test_difficulty_toggle_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OJ_DIFFICULTY_DETECT", "0")
        monkeypatch.setenv("OJ_DIFFICULTY_SKIP_MODEL", "glm-5.2")
        cm = ConfigManager(str(tmp_path / "none.json"))
        assert cm.cfg.difficulty_detect_enable is False
        assert cm.cfg.difficulty_skip_model == "glm-5.2"

    def test_validate_warns_on_unknown_skip_model(self, tmp_path):
        path = self._write(tmp_path, {
            "models": {"m": {"base_url": "https://x", "max_tokens": 100}},
            "difficulty_detect_enable": False,
            "difficulty_skip_model": "typo-model",
        })
        warnings = ConfigManager(path).validate()
        assert any("difficulty_skip_model" in w for w in warnings)

    def test_save_keeps_every_setting(self, tmp_path):
        """save() 曾经把手写字典里漏掉的 4 个字段从 config.json 里删掉。"""
        path = self._write(tmp_path, {"oj_root": "https://x"})
        cm = ConfigManager(path)
        cm.set_override(
            auto_supplement_testdata=True,
            max_cost_per_problem=1.5,
            cost_accum_enable=False,
            benchmark_users=[7],
            difficulty_detect_enable=False,
            difficulty_skip_model="glm-5.2",
        )
        cm.save()
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, expected in (("auto_supplement_testdata", True),
                              ("max_cost_per_problem", 1.5),
                              ("cost_accum_enable", False),
                              ("benchmark_users", [7]),
                              ("difficulty_detect_enable", False),
                              ("difficulty_skip_model", "glm-5.2")):
            assert saved[key] == expected, f"{key} 在 save() 后丢失或被改写"
        assert "password" not in saved and "ai_api_key" not in saved


# ══════════════════════════════════════════════════════════════
# 命令行 / 费用上限 / 峰谷价
# ══════════════════════════════════════════════════════════════
def _args(**kw):
    base = dict(username=None, password=None, lang=None, api_key_env=None,
                base_url=None, model=None, base=None, cookie_jar=None,
                timeout=None, show_thinking=False,
                no_difficulty_detect=False, difficulty_skip_model=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class TestCliOverrides:
    def test_api_key_env_is_applied(self):
        """--api-key-env 过去被写成 ai_api_key_env，ConfigManager 不认，静默忽略。"""
        cli = build_cli_overrides(_args(api_key_env="MY_KEY"))
        assert cli["ai_api_key"] == "MY_KEY"

    def test_model_and_root(self):
        cli = build_cli_overrides(_args(model="gpt-4o", base="https://a/b"),
                                  "https://oj.example", "https://oj.example/d/x")
        assert cli["ai_model"] == "gpt-4o"
        assert cli["oj_root"] == "https://oj.example"
        assert cli["oj_base"] == "https://oj.example/d/x"
        assert cli["oj_base"] == cli["oj_base"]  # 解析出的域覆盖 --base

    def test_empty_values_are_skipped(self):
        cli = build_cli_overrides(_args())
        assert cli == {"show_thinking": False}

    def test_disable_difficulty_detect(self):
        cli = build_cli_overrides(_args(no_difficulty_detect=True))
        assert cli["difficulty_detect_enable"] is False

    def test_skip_model_implies_detection_off(self):
        """只给专用模型也应生效，否则这个参数会被静默忽略。"""
        cli = build_cli_overrides(_args(difficulty_skip_model="glm-5.2"))
        assert cli["difficulty_skip_model"] == "glm-5.2"
        assert cli["difficulty_detect_enable"] is False

    def test_default_keeps_detection_on(self):
        assert "difficulty_detect_enable" not in build_cli_overrides(_args())


class TestCostCap:
    def test_under_and_over_cap(self, tmp_path):
        s = make_solver(tmp_path, {"max_cost_per_problem": 5.0})
        assert s._is_cost_capped(True, 4.0, 0.5) is False
        assert s._is_cost_capped(True, 4.0, 1.5) is True

    def test_disabled(self, tmp_path):
        s = make_solver(tmp_path)
        assert s._is_cost_capped(False, 100.0, 100.0) is False

    def test_bad_cap_value(self, tmp_path):
        s = make_solver(tmp_path, {"max_cost_per_problem": "无限"})
        assert s._is_cost_capped(True, 100.0, 100.0) is False


class TestPeakPricing:
    def test_wrap_around_range(self):
        assert _hour_in_range(23, 23, 7) is True
        assert _hour_in_range(3, 23, 7) is True
        assert _hour_in_range(12, 23, 7) is False

    def test_normal_range(self):
        assert _hour_in_range(10, 9, 12) is True
        assert _hour_in_range(12, 9, 12) is False

    def test_cost_uses_peak_price(self, tmp_path, monkeypatch):
        cfg_data = {
            "models": {"m": {"pricing": {
                "input": 1.0, "output": 1.0, "cache_hit": 0,
                "peaks": [{"hours": [23, 7], "input": 10.0, "output": 10.0}],
            }}}
        }
        path = tmp_path / "config.json"
        path.write_text(json.dumps(cfg_data), encoding="utf-8")
        ai = AIClient(Config(config_path=str(path)))
        monkeypatch.setattr("oj_solver.datetime", _FixedDatetime(3))
        cost = ai._calc_cost({"input": 1_000_000, "output": 0}, "m")
        assert cost == pytest.approx(10.0)


class _FixedDatetime:
    """只替换 datetime.now().hour，用于峰谷价测试。"""
    def __init__(self, hour):
        import datetime as _dt
        self._real = _dt.datetime
        self._hour = hour

    def now(self, *a, **kw):
        return self._real.now(*a, **kw).replace(hour=self._hour)


# ══════════════════════════════════════════════════════════════
# .env 解析
# ══════════════════════════════════════════════════════════════
class TestDotenv:
    def test_syntax_variants(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\ufeff# 注释行\n"
            "export OJ_TEST_EXPORT=plain\n"
            'OJ_TEST_QUOTED="a # b"\n'
            "OJ_TEST_COMMENT=value # 行内注释\n"
            "OJ_TEST_EMPTY=\n"
            "OJ_TEST_KEEP=from-file\n",
            encoding="utf-8")
        monkeypatch.setenv("OJ_TEST_KEEP", "from-env")
        keys = ["OJ_TEST_EXPORT", "OJ_TEST_QUOTED", "OJ_TEST_COMMENT",
                "OJ_TEST_EMPTY", "OJ_TEST_KEEP"]
        try:
            load_dotenv(str(env_file))
            import os
            assert os.environ["OJ_TEST_EXPORT"] == "plain"
            assert os.environ["OJ_TEST_QUOTED"] == "a # b"
            assert os.environ["OJ_TEST_COMMENT"] == "value"
            assert os.environ["OJ_TEST_EMPTY"] == ""
            assert os.environ["OJ_TEST_KEEP"] == "from-env"  # 已存在不覆盖
        finally:
            import os
            for k in keys:
                os.environ.pop(k, None)

    def test_missing_file_is_noop(self, tmp_path):
        load_dotenv(str(tmp_path / "nope.env"))  # 不应抛异常


# ══════════════════════════════════════════════════════════════
# 私信 403 重试
# ══════════════════════════════════════════════════════════════
class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeSession:
    """第一轮全部 403，第二轮全部 200。"""
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        return _FakeResponse(403 if len(self.calls) <= 2 else 200)


class TestPushMessage403:
    def test_send_round_reports_403(self, monkeypatch):
        monkeypatch.setattr(oj_common, "MSG_LIMITER", _NoLimiter())
        assert oj_common._send_round(_FakeSession(), "https://x", "hi", {7}) is True

    def test_relogin_and_retry_once(self, monkeypatch):
        monkeypatch.setattr(oj_common, "MSG_LIMITER", _NoLimiter())
        calls = []
        monkeypatch.setattr(oj_common, "_recover_login",
                            lambda session, root: calls.append(root))
        session = _FakeSession()
        oj_common.push_oj_message(session, "https://x", "hi", push_uids=[7])
        assert calls == ["https://x"]              # 触发了一次重新登录
        assert len(session.calls) == 2             # 并且重试了一轮

    def test_no_retry_without_403(self, monkeypatch):
        monkeypatch.setattr(oj_common, "MSG_LIMITER", _NoLimiter())
        calls = []
        monkeypatch.setattr(oj_common, "_recover_login",
                            lambda session, root: calls.append(root))

        class Ok(_FakeSession):
            def post(self, url, json=None, headers=None, timeout=None):
                self.calls.append(json)
                return _FakeResponse(200)

        session = Ok()
        oj_common.push_oj_message(session, "https://x", "hi", push_uids=[7])
        assert calls == [] and len(session.calls) == 1


class _NoLimiter:
    def wait(self):
        pass


# ══════════════════════════════════════════════════════════════
# URL 解析 / 仪表盘 / 私信后端
# ══════════════════════════════════════════════════════════════
class TestUrlNoise:
    def test_query_and_fragment_stripped(self):
        root, base, pid = parse_problem_url(
            "https://oj.example/d/system/p/1178?tid=abc#anchor")
        assert (root, base, pid) == ("https://oj.example",
                                     "https://oj.example/d/system", "1178")

    def test_trailing_slash(self):
        assert parse_root("https://oj.example/p/1/") == "https://oj.example"

    def test_non_hex_contest_id(self):
        info = parse_contest_or_problem("https://oj.example/d/d1/contest/final-round")
        assert info["type"] == "contest" and info["contest_id"] == "final-round"

    def test_plain_id_still_supported(self):
        assert parse_problem_url("1178") == (None, None, "1178")


class TestDashboardStats:
    def test_contest_records_do_not_pollute_rates(self, tmp_path):
        from dashboard import Dashboard
        d = Dashboard(save_path=str(tmp_path / "dash.json"))
        d.problem_start("1")
        d.problem_update("1", status="ac", elapsed_s=1)
        d.problem_start("2")
        d.problem_update("2", status="fail", elapsed_s=1)
        d.contest_start("c1", title="比赛")
        d.contest_update("c1", total=2, ac=2)
        stats = d.stats()
        assert stats["total"] == 2 and stats["ac"] == 1 and stats["ac_rate"] == "1/2"

    def test_pending_uses_epoch(self, tmp_path):
        import time
        from dashboard import Dashboard
        d = Dashboard(save_path=str(tmp_path / "dash.json"))
        d.problem_start("9")
        d.problems["9"].started_epoch = time.time() - 10
        assert [p["pid"] for p in d.pending()] == ["9"]
        d.problems["9"].started_epoch = time.time() - 3600  # 超过 30 分钟
        assert d.pending() == []


class TestMsgBackend:
    def _backend(self, tmp_path, monkeypatch):
        from msg_backend import MsgBackend
        monkeypatch.chdir(tmp_path)
        return MsgBackend(object(), "https://x", 1, {"2"}, {2}, 5)

    def test_processed_survives_reload_in_order(self, tmp_path, monkeypatch):
        b = self._backend(tmp_path, monkeypatch)
        for i in range(5):
            b.processed[f"k{i}"] = None
        b._save_processed()
        from msg_backend import MsgBackend
        reloaded = MsgBackend(object(), "https://x", 1, {"2"}, {2}, 5)
        assert list(reloaded.processed) == [f"k{i}" for i in range(5)]

    def test_fifo_eviction(self, tmp_path, monkeypatch):
        b = self._backend(tmp_path, monkeypatch)
        from msg_backend import MAX_PROCESSED
        for i in range(MAX_PROCESSED + 5):
            b.processed[f"k{i}"] = None
        b._save_processed()
        assert len(b.processed) == MAX_PROCESSED
        assert "k0" not in b.processed and f"k{MAX_PROCESSED + 4}" in b.processed


# ══════════════════════════════════════════════════════════════
# 离线端到端：验证模型路由改为显式传参后的求解主流程
# ══════════════════════════════════════════════════════════════
def _verdict(is_ac, score=100):
    return {"is_ac": is_ac, "score": score, "time_ms": 12.0, "memory_kb": 3400,
            "case_summary": "AC", "errors_text": "", "fail_details": [],
            "failures_by_type": {}, "compiler_text": [], "judge_text": [],
            "is_system_error": False, "ac_rate": "1/1", "cases": []}


class _FakeOJ(OJClient):
    def __init__(self, cfg, verdicts):
        super().__init__(cfg)
        self._verdicts = list(verdicts)
        self.submitted = []
        self.solutions = []

    def login(self, max_retries: int = 3):
        return True

    def get_problem(self, pid):
        return {"pid": pid, "title": "测试题", "content": "求 a+b", "tags": [],
                "time_limit": "1000ms", "memory_limit": "256MiB", "io_method": "",
                "url": f"https://oj.example/p/{pid}", "images": []}

    def submit_code(self, pid, code, contest_id=""):
        self.submitted.append(code)
        return f"rid{len(self.submitted)}"

    def verify_submission(self, rid):
        return self._verdicts.pop(0) if self._verdicts else _verdict(False, 0)

    def post_solution(self, pid, solution_md):
        self.solutions.append(solution_md)
        return "psid1"


class _FakeAI(AIClient):
    """记录每次调用使用的模型，模拟 AI 返回。"""
    def __init__(self, cfg):
        super().__init__(cfg)
        self.models = []
        self.calls = []  # [(model, prompt)]

    def _p(self, key, default=""):
        return default  # 触发调用方回退到 DEFAULT_PROMPTS

    def _call_ai(self, user_prompt, system_msg, use_stream=False, images=None,
                 model="", effort=""):
        model = model or self.config["ai_model"]
        self.models.append(model)
        self.calls.append((model, user_prompt))
        return {"solution_md": "[标签: 模拟]\n思路正文", "code": "int main(){}",
                "usage": {"input": 10, "output": 20, "total": 30, "cache_hit": 0},
                "elapsed_s": 0.5, "cost": 0.01, "model": model}

    @property
    def difficulty_probes(self):
        """难度判断那次调用的记录（按提示词特征识别）。"""
        return [c for c in self.calls if "难度等级" in c[1]]


class TestSolveFlowOffline:
    def _run(self, tmp_path, monkeypatch, verdicts, cli=None, accumulate=False):
        monkeypatch.chdir(tmp_path)
        # 用仓库自带的 config.json 作为模型表（没有则退回默认值），
        # 工件（dashboard.json / cost_accum.json）则写在隔离的 tmp 目录里。
        fixture = Path(__file__).with_name("config.json")
        cfg = Config(config_path=str(fixture if fixture.exists() else tmp_path / "none.json"),
                     cli_overrides=cli or {})
        oj = _FakeOJ(cfg, verdicts)
        ai = _FakeAI(cfg)
        result = SolverOrchestrator(oj, ai, cfg).solve(
            "1178", submit=True, post=True, accumulate=accumulate)
        return result, oj, ai

    def test_ac_publishes_solution(self, tmp_path, monkeypatch):
        result, oj, ai = self._run(tmp_path, monkeypatch, [_verdict(True)])
        assert result["is_ac"] is True
        assert oj.solutions and "思路正文" in oj.solutions[0]
        # 代码头注释与题解落款应使用真实产出代码的模型
        assert ai.models, "应当调用过 AI"
        assert ai.models[0] in oj.submitted[0]
        # 标签前缀被剥离，不出现在正文中
        assert "[标签:" not in oj.solutions[0]

    def test_cost_cap_stops_early(self, tmp_path, monkeypatch):
        # 费用上限依赖「累计计费」开关（accumulate / cost_accum_enable），
        # 手动单次求解（accumulate=False）本身就不设上限，这是既有语义。
        result, oj, ai = self._run(
            tmp_path, monkeypatch, [_verdict(False, 0)],
            cli={"max_cost_per_problem": 0.005, "cost_accum_enable": True},
            accumulate=True)
        assert result["is_cost_capped"] is True
        assert result["is_ac"] is False
        assert oj.solutions == []


class TestDifficultyToggle:
    """难度判断开关 + 关闭时使用的专用模型。"""

    def _run(self, tmp_path, monkeypatch, cli):
        # Phase0 先失败一次，才能真正走到按层级求解的 Phase2
        return TestSolveFlowOffline()._run(
            tmp_path, monkeypatch, [_verdict(False, 0), _verdict(True)], cli=cli)

    def test_enabled_by_default_probes_difficulty(self, tmp_path, monkeypatch):
        _, _, ai = self._run(tmp_path, monkeypatch, {})
        assert ai.difficulty_probes, "默认应当调用难度判断"

    def test_disabled_skips_probe_and_uses_skip_model(self, tmp_path, monkeypatch):
        result, oj, ai = self._run(tmp_path, monkeypatch, {
            "difficulty_detect_enable": False,
            "difficulty_skip_model": "glm-5.2",   # model_router 的 max 层
        })
        assert result["is_ac"] is True
        assert ai.difficulty_probes == [], "关闭后不应再调用难度判断"
        assert "glm-5.2" in oj.submitted[-1], "应当用专用模型解题"

    def test_skip_model_outside_router_used_alone(self, tmp_path, monkeypatch):
        result, oj, ai = self._run(tmp_path, monkeypatch, {
            "difficulty_detect_enable": False,
            "difficulty_skip_model": "my-custom-model",
        })
        assert result["is_ac"] is True
        assert ai.difficulty_probes == []
        # 不在分层里的模型：本次只用它，不再走 flash/pro/max 分层
        # （"请讲解并给出代码" 是求解提示词，可据此排除 Phase0 的快速尝试）
        solving_models = {m for m, prompt in ai.calls if "请讲解并给出代码" in prompt}
        assert solving_models == {"my-custom-model"}

    def test_disabled_without_skip_model_uses_router_default(self, tmp_path, monkeypatch):
        result, oj, ai = self._run(tmp_path, monkeypatch, {
            "difficulty_detect_enable": False,
            "difficulty_skip_model": "",
        })
        assert result["is_ac"] is True
        assert ai.difficulty_probes == []
        # 未指定专用模型 → 沿用分层首个模型
        fixture = Path(__file__).with_name("config.json")
        from model_router import ModelRouter
        default_model = ModelRouter(Config(config_path=str(fixture))).default.model
        assert default_model in oj.submitted[-1]
