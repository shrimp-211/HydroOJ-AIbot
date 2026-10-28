"""动态配置管理器 — 类型安全、热重载、线程安全"""
import os
import copy
import json
import time
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from config_io import read_config, atomic_write_json, env_reference
from runtime_paths import data_file

log = logging.getLogger(__name__)


@dataclass
class AppConfig:
    # OJ
    oj_root: str = "https://oj.yuanyicode.com"
    oj_base: str = "https://oj.yuanyicode.com/d/system"
    username: str = ""
    password: str = ""
    # AI (全局默认值，可被 models.<name> 覆盖)
    ai_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-v4-pro"
    ai_api_key: str = ""
    ai_reasoning_effort: str = "high"
    ai_max_tokens: int = 384000
    ai_task_tokens: dict = field(default_factory=lambda: {
        "default": 16384, "solve": 16384, "plan": 4096, "reference": 8192,
        "difficulty": 1024, "editorial": 8192,
    })
    compact_history: bool = True
    # 多模态视觉模型（题面含图时优先使用，见 is_vision_model）
    ai_vision_model: str = "deepseek-v4-flash-vision-exp"
    max_problem_images: int = 4  # 每道题最多下载几张题面图片
    # 难度判断：关闭后跳过难度评估调用（省一次 AI 请求），改用 difficulty_skip_model
    difficulty_detect_enable: bool = True
    difficulty_skip_model: str = ""  # 关闭难度判断时使用的模型（空=沿用分层默认）
    # Agent 求解：先本地编译/跑样例/随机对拍，通过后再提交 OJ
    agent_enabled: bool = True
    agent_max_steps: int = 12          # 单题最多几轮「方案 → 实现 → 修正」
    agent_max_submissions: int = 8     # 单题最多提交几次（不为省提交而牺牲正确率）
    agent_max_seconds: int = 900       # 单题 agent 总时长上限（秒）
    agent_stress_enable: bool = True   # 是否用暴力解做随机对拍
    agent_stress_rounds: int = 30      # 对拍轮数
    agent_run_timeout: int = 10        # 单次本地运行超时（秒）
    # 求解策略：多条互相独立的思路（换算法，而不是改上一版）
    solve_candidates: int = 3          # 每个层级尝试几条独立思路
    solve_repairs: int = 2             # 每条思路最多修正几次
    # 题解产出：AC 之后单独写一篇结构化题解（而不是直接发布求解输出）
    editorial_enable: bool = True
    editorial_model: str = ""          # 写题解用的模型（空=沿用求解模型）
    editorial_fix_rounds: int = 2      # 校验不通过时的定向修复轮数
    # Code
    lang: str = "cc.cc14o2"
    # Runtime
    verify_timeout: int = 60
    cookie_jar: str = ""
    show_thinking: bool = True
    # Monitor
    monitor_domains: list[str] = field(default_factory=lambda: ["system"])
    monitor_interval: int = 120
    contest_start_page: int = 2
    monitor_state_file: str = "processed_contests.json"
    # Message
    msg_whitelist: list[int] = field(default_factory=lambda: [2])
    msg_push_list: list[int] = field(default_factory=lambda: [2])
    msg_superuser: list[int] = field(default_factory=lambda: [2])
    msg_interval: int = 5
    # Models (per-model independent config)
    models: dict[str, dict] = field(default_factory=dict)
    model_router: dict[str, dict] = field(default_factory=dict)
    # Misc
    msg_blocked_cmds: list[str] = field(default_factory=lambda: ["exit", "quit", "q"])
    benchmark_users: list[int] = field(default_factory=lambda: [2])
    auto_supplement_testdata: bool = False
    max_cost_per_problem: float = 5.0
    cost_accum_enable: bool = True  # 单题金额长期累计（跨调用），False 则仅按单次会话限额
    msg_push_events: dict[str, bool] = field(default_factory=dict)
    code_obfuscate: dict[str, bool] = field(default_factory=dict)

    def clone(self) -> "AppConfig":
        return AppConfig(**asdict(self))


# Every dataclass field is persisted automatically; only the legacy language key differs.
FIELD_TO_JSON = {name: ("code_lang" if name == "lang" else name)
                 for name in AppConfig.__dataclass_fields__}
JSON_TO_FIELD = {value: key for key, value in FIELD_TO_JSON.items()}


class ConfigManager:
    """配置管理器 — 支持 JSON 文件 + 环境变量 + 热重载"""

    def __init__(self, config_path: str | None = None, auto_reload: bool = False,
                 cli_overrides: dict | None = None):
        self._path = Path(config_path) if config_path is not None else data_file("config.json")
        self._config = AppConfig()
        self._lock = threading.RLock()
        self._mtime: float = 0
        self._mtime_cache: float = 0
        self._mtime_probe: float = 0.0   # 上次 stat 时间（避免每次属性访问都打盘）
        self._mtime_stat: float = -1.0   # 上次 stat 结果
        self._callbacks: list = []
        self._overrides = dict(cli_overrides or {})
        self._load_error = ""
        self.reload()
        if auto_reload:
            self._start_watcher()

    # ---- 属性访问 ----
    @property
    def cfg(self) -> AppConfig:
        if self._check_reload():
            self.reload()
        return self._config

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        # 只代理 AppConfig 的合法字段
        if name in self._config.__dataclass_fields__:
            return getattr(self.cfg, name)
        raise AttributeError(f"'{type(self).__name__}' 无 '{name}' 属性")

    # ---- 加载 ----
    @staticmethod
    def _coerce(current, val):
        """把 JSON/env 里的原始值转换成与 AppConfig 字段一致的类型。
        返回 (ok, value)；无法转换时返回 (False, None)，调用方跳过该字段。
        """
        if current is None or isinstance(current, list) or isinstance(current, dict):
            return True, val
        if isinstance(current, bool):
            if isinstance(val, str):
                low = val.strip().lower()
                if low in ("true", "1", "yes", "on"):
                    return True, True
                if low in ("false", "0", "no", "off", ""):
                    return True, False
                return False, None
            return True, bool(val)
        if isinstance(current, int):
            if isinstance(val, bool):
                return True, int(val)
            if isinstance(val, str):
                try:
                    return True, int(val.strip())
                except ValueError:
                    return False, None
            if isinstance(val, (int, float)):
                return True, int(val)
            return False, None
        if isinstance(current, float):
            try:
                return True, float(val)
            except (TypeError, ValueError):
                return False, None
        if isinstance(current, str):
            return (True, val) if isinstance(val, str) else (True, str(val))
        return True, val

    def reload(self):
        with self._lock:
            c = AppConfig()
            # 1. JSON 文件
            if self._path.exists():
                try:
                    j = read_config(self._path)
                except (ValueError, OSError) as e:
                    self._load_error = f"配置读取失败: {type(e).__name__}"
                    log.error("[配置] %s，保留上一次有效配置", self._load_error)
                    return False
                if isinstance(j, dict):
                    for field, key in FIELD_TO_JSON.items():
                        if key not in j or not hasattr(c, field):
                            continue
                        val = j[key]
                        # list/dict 字段只接受同类型；标量字段做类型收敛
                        current = getattr(c, field)
                        if isinstance(current, list) and not isinstance(val, list):
                            continue
                        if isinstance(current, dict) and not isinstance(val, dict):
                            continue
                        ok, converted = self._coerce(current, val)
                        if ok:
                            setattr(c, field, converted)
            # 2. 环境变量 (OJ_ / AI_ 前缀)
            env_map = {
                "oj_root": ["OJ_ROOT"],
                "oj_base": ["OJ_BASE"],
                "username": ["OJ_USERNAME"],
                "password": ["OJ_PASSWORD"],
                "ai_base_url": ["OJ_AI_BASE_URL", "AI_BASE_URL"],
                "ai_model": ["OJ_AI_MODEL", "AI_MODEL"],
                "ai_api_key": ["OJ_AI_API_KEY", "AI_API_KEY"],
                "ai_reasoning_effort": ["OJ_AI_REASONING_EFFORT", "AI_REASONING_EFFORT"],
                "ai_max_tokens": ["OJ_AI_MAX_TOKENS", "AI_MAX_TOKENS"],
                "ai_vision_model": ["OJ_AI_VISION_MODEL"],
                "cookie_jar": ["OJ_COOKIE_JAR"],
                "contest_start_page": ["OJ_CONTEST_START_PAGE"],
                "difficulty_detect_enable": ["OJ_DIFFICULTY_DETECT", "DIFFICULTY_DETECT"],
                "difficulty_skip_model": ["OJ_DIFFICULTY_SKIP_MODEL", "DIFFICULTY_SKIP_MODEL"],
                "agent_enabled": ["OJ_AGENT_ENABLE", "AGENT_ENABLE"],
                "agent_max_steps": ["OJ_AGENT_MAX_STEPS"],
                "agent_stress_enable": ["OJ_AGENT_STRESS", "AGENT_STRESS"],
                "solve_candidates": ["OJ_SOLVE_CANDIDATES"],
                "solve_repairs": ["OJ_SOLVE_REPAIRS"],
                "editorial_enable": ["OJ_EDITORIAL_ENABLE"],
                "editorial_model": ["OJ_EDITORIAL_MODEL"],
                "lang": ["OJ_LANG"],
                "verify_timeout": ["OJ_VERIFY_TIMEOUT", "VERIFY_TIMEOUT"],
            }
            for field, keys in env_map.items():
                if isinstance(keys, str): keys = [keys]
                for key in keys:
                    if key in os.environ and hasattr(c, field):
                        val = os.environ[key]
                        ok, converted = self._coerce(getattr(c, field), val)
                        if not ok:
                            continue
                        setattr(c, field, converted)
                        break
            for key, value in self._overrides.items():
                if value is not None and hasattr(c, key):
                    setattr(c, key, copy.deepcopy(value))
            if c.oj_root != AppConfig.oj_root and c.oj_base == AppConfig.oj_base:
                c.oj_base = c.oj_root.rstrip("/") + "/d/system"
            self._load_error = ""
            self._config = c
            self._mtime = self._stat_mtime()
            self._mtime_stat = self._mtime
            self._mtime_probe = time.monotonic()
        self._notify()
        return True

    def _stat_mtime(self) -> float:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return 0.0

    def save(self):
        """Persist settings while retaining unknown fields and stored credentials.

        Runtime/env credentials are never copied into the file.
        """
        with self._lock:
            existing = read_config(self._path)
            values = {key: copy.deepcopy(getattr(self._config, attr))
                      for attr, key in FIELD_TO_JSON.items()
                      if attr not in ("password", "ai_api_key")}
            existing.update(values)
            atomic_write_json(self._path, existing)
            self._mtime = self._stat_mtime()
            self._mtime_stat = self._mtime

    # ---- 热重载 ----
    def _check_reload(self) -> bool:
        """文件 mtime 变化检测。5s 内只 stat 一次，避免高频属性访问打盘。"""
        now = time.monotonic()
        if now - self._mtime_probe < 5:
            return False
        self._mtime_probe = now
        mtime = self._stat_mtime()
        self._mtime_stat = mtime
        return mtime != self._mtime

    def _start_watcher(self):
        def _watch():
            while True:
                time.sleep(5)
                if self._check_reload():
                    log.info("[*] 配置文件已变更，热重载")
                    self.reload()
        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def on_change(self, callback):
        self._callbacks.append(callback)

    def _notify(self):
        for cb in self._callbacks:
            try: cb(self._config)
            except Exception as e: log.debug("[!] 回调异常: %s", e)

    # ---- WebUI 兼容 ----
    def get(self, key, default=None):
        return getattr(self.cfg, key, default)

    def __getitem__(self, key):
        return getattr(self.cfg, key)

    def __contains__(self, key):
        return hasattr(self.cfg, key)

    def set_override(self, **kwargs):
        """临时覆盖配置（线程安全），用于 model_router 切换模型等场景"""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self._config, k):
                    setattr(self._config, k, copy.deepcopy(v))
                    self._overrides[k] = copy.deepcopy(v)

    def repair(self) -> list[str]:
        """补齐 config.json 中缺失的字段（用 AppConfig 默认值填充），返回新增的字段列表。"""
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            return []
        if not isinstance(j, dict):
            return []
        added = []
        c = self.cfg
        # 敏感字段不落盘（凭据统一由 .env 管理，避免明文写进 config.json）
        sensitive = {"password", "ai_api_key"}
        # 由统一的 FIELD_TO_JSON 派生，保证「补齐」与「读取」认为的键名完全一致
        for attr_name, json_key in FIELD_TO_JSON.items():
            if attr_name in sensitive:
                continue
            if json_key not in j and hasattr(c, attr_name):
                val = getattr(c, attr_name)
                j[json_key] = val
                added.append(json_key)

        if added:
            try:
                atomic_write_json(self._path, j)
                log.info("[配置] 补齐 %d 个缺失字段: %s", len(added), ", ".join(added))
            except Exception as e:
                log.warning("[配置] 补齐写入失败: %s", e)

        return added

    # ── 配置校验 ──

    def validate(self) -> list[str]:
        """启动时校验配置，返回警告列表（空=无问题）"""
        c = self.cfg
        warnings = [self._load_error] if self._load_error else []
        if not str(c.oj_root or "").startswith("http"):
            warnings.append("oj_root 不是有效 URL")
        if not str(c.ai_base_url or "").startswith("http"):
            warnings.append("ai_base_url 不是有效 URL")
        if not isinstance(c.verify_timeout, int) or c.verify_timeout <= 0:
            warnings.append("verify_timeout 应 > 0")
        if type(c.contest_start_page) is not int or c.contest_start_page < 1:
            warnings.append("contest_start_page 必须是正整数")
        if c.models:
            for name, md in c.models.items():
                if name.startswith("_") or not isinstance(md, dict):
                    continue
                if md.get("base_url") and not str(md["base_url"]).startswith("http"):
                    warnings.append(f"模型 {name} 的 base_url 无效")
                # max_tokens 允许缺省（继承全局），但显式写了非法值要报出来。
                # 注意配置里可能写成 null / "128000"，直接比较会抛 TypeError。
                mt = md.get("max_tokens", None)
                if mt is not None:
                    try:
                        mt_int = int(mt)
                    except (TypeError, ValueError):
                        warnings.append(f"模型 {name} 的 max_tokens 不是整数: {mt!r}")
                    else:
                        if mt_int <= 0:
                            warnings.append(f"模型 {name} 的 max_tokens 无效: {mt!r}")
        # 检查 model_router tiers 引用的模型是否存在
        router = c.model_router or {}
        tiers = router.get("tiers", {}) if isinstance(router, dict) else {}
        model_names = {k for k in c.models if not k.startswith("_")} | {c.ai_model}
        if not isinstance(tiers, dict):
            warnings.append("model_router.tiers 必须是对象")
            tiers = {}
        for tier, name in tiers.items():
            if name and name not in model_names:
                warnings.append(f"model_router tiers.{tier} 引用了不存在的模型 '{name}'")
        # 关闭难度判断时使用的模型同样要存在，否则会一路撞到「API Key 未配置」
        skip_model = (c.difficulty_skip_model or "").strip()
        if not c.difficulty_detect_enable and skip_model and skip_model not in model_names:
            warnings.append(f"difficulty_skip_model 引用了不存在的模型 '{skip_model}'")
        if warnings:
            log.warning("[配置校验] %d 个警告:", len(warnings))
            for w in warnings:
                log.warning("  - %s", w)
        else:
            log.info("[配置校验] 通过")
        return warnings

    # ── 模型配置查询 ──

    def get_model_config(self, model_name: str) -> dict:
        """获取指定模型的完整配置，合并模型自身字段与全局默认值。
        返回: {base_url, api_key, max_tokens, reasoning_effort, pricing}"""
        cfg = self.cfg  # Probe reload before consulting the cache.
        md = cfg.models.get(model_name) if cfg.models else None
        if md is None:
            result = dict(base_url=cfg.ai_base_url, api_key=self.api_key,
                          max_tokens=cfg.ai_max_tokens,
                          reasoning_effort=cfg.ai_reasoning_effort, pricing={})
        else:
            if not isinstance(md, dict):
                md = {}
            result = dict(
                base_url=md.get("base_url") or cfg.ai_base_url,
                api_key=(self._resolve_model_api_key(md["api_key"])
                         if md.get("api_key") else self.api_key),
                max_tokens=md.get("max_tokens") or cfg.ai_max_tokens,
                reasoning_effort=md.get("reasoning_effort")
                    if md.get("reasoning_effort") is not None else cfg.ai_reasoning_effort,
                pricing=md.get("pricing") or {},
            )
        if isinstance(md, dict):
            for key in ("provider", "token_parameter", "supports_reasoning_effort",
                        "thinking", "extra_body", "stream_usage", "timeout", "max_retries"):
                if key in md and md[key] is not None:
                    result[key] = copy.deepcopy(md[key])
        # Resolve environment references each time so credential rotation takes effect.
        return copy.deepcopy(result)

    def _resolve_model_api_key(self, val) -> str:
        """解析模型级 api_key：null → 返回空；全大写 → 环境变量引用；含. → 字面值"""
        if val is None or val == "":
            return ""
        return self._resolve_api_key(val)

    def get_model_base_url(self, model_name: str) -> str:
        return self.get_model_config(model_name)["base_url"]

    def get_model_api_key(self, model_name: str) -> str:
        return self.get_model_config(model_name)["api_key"]

    def get_model_max_tokens(self, model_name: str) -> int:
        return self.get_model_config(model_name)["max_tokens"]

    def get_model_reasoning_effort(self, model_name: str) -> str:
        return self.get_model_config(model_name)["reasoning_effort"]

    def get_model_pricing(self, model_name: str) -> dict:
        return self.get_model_config(model_name)["pricing"]

    def get_vision_model(self) -> str:
        """多模态视觉模型名。配置 ai_vision_model 或按 models 段中标记 vision 的模型"""
        name = self.cfg.ai_vision_model
        if name:
            return name
        for mname, md in (self.cfg.models or {}).items():
            if mname.startswith("_"):
                continue
            if isinstance(md, dict) and md.get("vision"):
                return mname
        return ""

    def is_vision_model(self, model_name: str) -> bool:
        """模型是否支持多模态图片输入"""
        name = model_name or self.cfg.ai_model
        if not name:
            return False
        # 配置 ai_vision_model 显式指定
        if name == self.cfg.ai_vision_model:
            return True
        # models 段显式标记 vision: true
        md = (self.cfg.models or {}).get(name)
        if isinstance(md, dict) and md.get("vision"):
            return True
        # 命名约定回退：含 vision/视觉 的模型名
        return "vision" in name.lower() or "glm-4.6v" in name.lower()

    def get_tier_model(self, tier: str) -> str | None:
        """获取路由层对应的模型名。优先 config.model_router.tiers，fallback 环境变量 AI_MODEL_<TIER>"""
        cfg = self.cfg
        router = cfg.model_router or {}
        tiers = router.get("tiers", {})
        if isinstance(tiers, dict) and isinstance(tiers.get(tier), str) and tiers[tier]:
            return tiers[tier]
        return os.environ.get(f"AI_MODEL_{tier.upper()}", None)

    def get_tier_thinking(self, tier: str) -> list[str]:
        """获取路由层对应的推理强度列表"""
        cfg = self.cfg
        router = cfg.model_router or {}
        thinking_map = router.get("thinking_levels", {})
        raw = thinking_map.get(tier) if isinstance(thinking_map, dict) else None
        if raw is None:
            raw = os.environ.get(f"AI_THINKING_{tier.upper()}", "")
        if not raw:
            return []
        return [t.strip() for t in str(raw).split(",") if t.strip()]

    @property
    def api_key(self) -> str:
        return self._resolve_api_key(self.cfg.ai_api_key)

    @staticmethod
    def _resolve_api_key(val: str) -> str:
        if not val:
            return ""
        reference = env_reference(val)
        return os.environ.get(reference, "") if reference is not None else str(val)

    def task_token_limit(self, task: str = "default") -> int:
        defaults = AppConfig().ai_task_tokens
        budgets = self.cfg.ai_task_tokens
        return int(budgets.get(task, budgets.get("default", defaults.get(task, 16384))))

    @property
    def lang_ext(self) -> str:
        lang = self.cfg.lang
        for prefix, ext in [("cpp", "cpp"), ("cc", "cpp"), ("c++", "cpp"),
                             ("py", "python"), ("java", "java"), ("go", "go"),
                             ("rb", "ruby"), ("rs", "rust"), ("js", "javascript"),
                             ("cs", "csharp"), ("php", "php"), ("pas", "pascal")]:
            if prefix in lang: return ext
        log.warning("未知语言 %s，回退为 cpp", lang)
        return "cpp"
