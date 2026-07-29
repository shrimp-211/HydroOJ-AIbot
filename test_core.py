"""核心逻辑单元测试"""
import os
import json
import pytest
from pathlib import Path
from oj_common import parse_problem_url, parse_contest_or_problem, parse_root


class TestParseProblemUrl:
    def test_plain_id(self):
        root, api_base, pid = parse_problem_url("1178")
        assert root is None and api_base is None and pid == "1178"

    def test_root_url(self):
        root, api_base, pid = parse_problem_url("https://oj.yuanyicode.com/p/1178")
        assert root == "https://oj.yuanyicode.com"
        assert api_base == "https://oj.yuanyicode.com/d/system"
        assert pid == "1178"

    def test_domain_url(self):
        root, api_base, pid = parse_problem_url(
            "https://oj.yuanyicode.com/d/yuanyi__contestForPrimary/p/1274")
        assert root == "https://oj.yuanyicode.com"
        assert api_base == "https://oj.yuanyicode.com/d/yuanyi__contestForPrimary"
        assert pid == "1274"

    def test_problem_path(self):
        root, api_base, pid = parse_problem_url("https://other-oj.com/problem/5678")
        assert root == "https://other-oj.com"
        assert pid == "5678"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_problem_url("not-a-valid-url")


class TestParseContestOrProblem:
    def test_contest_url(self):
        r = parse_contest_or_problem(
            "https://oj.yuanyicode.com/d/yuanyi__contestForPrimary/contest/6a258dcb")
        assert r["type"] == "contest"
        assert r["domain_id"] == "yuanyi__contestForPrimary"

    def test_problem_url(self):
        r = parse_contest_or_problem(
            "https://oj.yuanyicode.com/d/yuanyi__contestForPrimary/p/1274")
        assert r["type"] == "problem" and r["pids"] == ["1274"]

    def test_root_problem(self):
        r = parse_contest_or_problem("https://oj.yuanyicode.com/p/1178")
        assert r["domain_id"] == "system" and r["pids"] == ["1178"]

    def test_training_url(self):
        r = parse_contest_or_problem("https://oj.yuanyicode.com/training/68c3c71d")
        assert r["type"] == "training" and r["training_id"] == "68c3c71d"

    def test_domain_training_url(self):
        r = parse_contest_or_problem("https://oj.yuanyicode.com/d/domain/training/abc123")
        assert r["type"] == "training" and r["domain_id"] == "domain"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_contest_or_problem("not-a-url")


class TestConfig:
    def test_defaults(self, monkeypatch):
        for env in ["OJ_ROOT", "OJ_BASE", "OJ_USERNAME", "OJ_PASSWORD",
                     "OJ_AI_BASE_URL", "AI_BASE_URL", "OJ_AI_MODEL", "AI_MODEL",
                     "OJ_AI_API_KEY", "AI_API_KEY", "OJ_LANG", "OJ_VERIFY_TIMEOUT"]:
            monkeypatch.delenv(env, raising=False)
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json")
        assert c["oj_root"] == "https://oj.yuanyicode.com"
        assert c["ai_model"] == "deepseek-v4-pro"
        assert c["lang"] == "cc.cc14o2"
        assert c.lang_ext == "cpp"

    def test_cli_override(self):
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json",
                   cli_overrides={"ai_model": "gpt-4o", "lang": "py.py3"})
        assert c["ai_model"] == "gpt-4o"
        assert c.lang_ext == "python"

    def test_env_override(self, monkeypatch):
        monkeypatch.delenv("AI_MAX_TOKENS", raising=False)
        monkeypatch.delenv("VERIFY_TIMEOUT", raising=False)
        monkeypatch.setenv("AI_MAX_TOKENS", "64000")
        monkeypatch.setenv("VERIFY_TIMEOUT", "120")
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json")
        assert c["ai_max_tokens"] == 64000
        assert c["verify_timeout"] == 120

    def test_env_bad_int(self, monkeypatch):
        monkeypatch.delenv("AI_MAX_TOKENS", raising=False)
        monkeypatch.setenv("AI_MAX_TOKENS", "high")
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json")
        assert c["ai_max_tokens"] == 384000

    def test_api_key_literal(self):
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json",
                   cli_overrides={"ai_api_key": "cd10cc.xxxx.xxxx"})
        assert c.api_key == "cd10cc.xxxx.xxxx"

    def test_api_key_env_var(self, monkeypatch):
        monkeypatch.delenv("OJ_AI_API_KEY", raising=False)
        monkeypatch.delenv("AI_API_KEY", raising=False)
        monkeypatch.setenv("MY_KEY", "my-key-value")
        from oj_solver import Config
        c = Config(config_path="/nonexistent.json",
                   cli_overrides={"ai_api_key": "MY_KEY"})
        assert c.api_key == "my-key-value"

    def test_lang_ext_unknown(self):
        from oj_solver import Config
        c = Config(cli_overrides={"lang": "unknown_lang"})
        assert c.lang_ext == "cpp"  # fallback, with warning


class TestVerdictParsing:
    def test_all_ac(self):
        from oj_solver import OJClient
        from oj_solver import Config
        oc = OJClient(Config())
        rdoc = {
            "score": 100, "time": 15.5, "memory": 6400,
            "testCases": [
                {"id": 1, "status": 1, "time": 1.2, "memory": 4000},
                {"id": 2, "status": 1, "time": 2.3, "memory": 4100},
            ],
        }
        v = oc._parse_verdict(rdoc)
        assert v["is_ac"] is True
        assert v["ac_rate"] == "2/2"
        assert v["score"] == 100

    def test_partial_tle(self):
        from oj_solver import OJClient
        from oj_solver import Config
        oc = OJClient(Config())
        rdoc = {
            "score": 44, "time": 16000, "memory": 22000,
            "testCases": [
                {"id": 1, "status": 1, "time": 2.0, "memory": 6000, "subtaskId": 1},
                {"id": 12, "status": 3, "time": 1100.0, "memory": 20000, "subtaskId": 2},
            ],
            "subtasks": [{"id": 1, "title": "小数据"}, {"id": 2, "title": "大数据"}],
        }
        v = oc._parse_verdict(rdoc)
        assert v["is_ac"] is False
        assert "TLE" in v["failures_by_type"]
        assert "子任务汇总" in v["errors_text"]

    def test_none_time(self):
        from oj_solver import OJClient
        from oj_solver import Config
        oc = OJClient(Config())
        rdoc = {
            "score": 0, "time": 0, "memory": 0,
            "testCases": [
                {"id": 0, "status": 9, "time": None, "memory": None, "message": "canceled"},
            ],
        }
        v = oc._parse_verdict(rdoc)
        assert v["is_ac"] is False
        assert "CANCELED" in v["case_summary"]


class TestModelConfig:
    """每模型独立配置测试"""

    def test_inherits_globals(self, monkeypatch):
        """null 字段继承全局默认值"""
        for env in ["OJ_ROOT", "OJ_USERNAME", "OJ_PASSWORD", "OJ_AI_BASE_URL",
                     "AI_BASE_URL", "OJ_AI_MODEL", "AI_MODEL", "AI_API_KEY"]:
            monkeypatch.delenv(env, raising=False)
        from config_manager import ConfigManager
        c = ConfigManager()
        cfg = c.get_model_config("glm-5.2")
        assert cfg["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert cfg["max_tokens"] == 128000
        assert cfg["reasoning_effort"] == "high,max"  # 显式设置，非继承

    def test_overrides_globals(self, monkeypatch):
        """显式字段覆盖全局默认值"""
        for env in ["OJ_ROOT", "OJ_USERNAME", "OJ_PASSWORD", "DEEPSEEK_API_KEY"]:
            monkeypatch.delenv(env, raising=False)
        from config_manager import ConfigManager
        c = ConfigManager()
        cfg = c.get_model_config("deepseek-v4-pro")
        assert cfg["base_url"] == "https://api.deepseek.com"
        assert cfg["max_tokens"] == 384000
        assert cfg["reasoning_effort"] == "high,max"

    def test_pricing_peaks(self):
        """多峰值 pricing 读取"""
        from config_manager import ConfigManager
        c = ConfigManager()
        p = c.get_model_pricing("deepseek-v4-pro")
        assert p["input"] == 3.0
        assert p["output"] == 6.0
        assert p["cache_hit"] == 0.025
        assert len(p["peaks"]) == 2
        assert p["peaks"][0]["hours"] == [9, 12]

    def test_pricing_no_peaks(self):
        """无峰值模型 pricing"""
        from config_manager import ConfigManager
        c = ConfigManager()
        p = c.get_model_pricing("glm-5.2")
        assert p["input"] == 8.0
        assert p["output"] == 28.0
        assert p["cache_hit"] == 2.0
        assert "peaks" not in p

    def test_nonexistent_model_fallback(self):
        """不存在的模型降级到全局默认"""
        from config_manager import ConfigManager
        c = ConfigManager()
        cfg = c.get_model_config("no-such-model")
        assert cfg["base_url"] == c["ai_base_url"]
        assert cfg["max_tokens"] == c["ai_max_tokens"]
        assert cfg["pricing"] == {}

    def test_router_tiers(self):
        """model_router 层级映射"""
        from config_manager import ConfigManager
        c = ConfigManager()
        assert c.get_tier_model("flash") == "deepseek-v4-flash"
        assert c.get_tier_model("pro") == "deepseek-v4-pro"
        assert c.get_tier_model("max") == "glm-5.2"
        assert c.get_tier_thinking("flash") == []

    def test_router_from_config(self):
        """ModelRouter 从 ConfigManager 加载"""
        from config_manager import ConfigManager
        from model_router import ModelRouter
        c = ConfigManager()
        r = ModelRouter(config_manager=c)
        assert len(r.tiers) == 3
        assert r.flash.model == "deepseek-v4-flash"
        assert r.default.model == "deepseek-v4-flash"
        strat = r.solve_strategy(4, 0)
        assert strat[0] == "deepseek-v4-pro"  # diff 4 → pro tier

    def test_get_model_base_url(self):
        from config_manager import ConfigManager
        c = ConfigManager()
        assert "deepseek.com" in c.get_model_base_url("deepseek-v4-pro")
        assert "bigmodel.cn" in c.get_model_base_url("glm-5.2")


class TestSampleExtraction:
    """extract_samples 单元测试"""

    def test_structured_numbered(self):
        from testdata_supplement import TestDataSupplement
        td = TestDataSupplement("https://oj.yuanyicode.com/p/1")
        content = "```input1\n1 2\n3 4\n```\n```output1\n5\n```\n```input2\n2 3\n```\n```output2\n10\n```"
        samples = td.extract_samples(content)
        assert len(samples) == 2
        assert samples[0] == {"in": "1 2\n3 4", "out": "5", "n": 1}
        assert samples[1] == {"in": "2 3", "out": "10", "n": 2}

    def test_interleaved_blocks(self):
        from testdata_supplement import TestDataSupplement
        td = TestDataSupplement("https://oj.yuanyicode.com/p/1")
        content = "```input\n1 2\n```\n```output\n3\n```"
        samples = td.extract_samples(content)
        assert len(samples) == 1
        assert samples[0]["in"] == "1 2"
        assert samples[0]["out"] == "3"

    def test_no_samples(self):
        from testdata_supplement import TestDataSupplement
        td = TestDataSupplement("https://oj.yuanyicode.com/p/1")
        samples = td.extract_samples("没有测试数据")
        assert len(samples) == 0

    def test_mismatched_blocks(self):
        from testdata_supplement import TestDataSupplement
        td = TestDataSupplement("https://oj.yuanyicode.com/p/1")
        # 两个连续的 input 后面跟一个 output → 应只配对第二个 pair
        content = "```input\n1\n```\n```input\n2\n```\n```output\n3\n```"
        samples = td.extract_samples(content)
        assert len(samples) == 1  # input(2)+output(3), input(1) 无匹配跳过
        assert samples[0]["in"] == "2"
        assert samples[0]["out"] == "3"
