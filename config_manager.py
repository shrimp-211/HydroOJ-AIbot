"""动态配置管理器 — 类型安全、热重载、线程安全"""
import os
import json
import time
import logging
import threading
from pathlib import Path
from dataclasses import dataclass, field, asdict

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
    # Code
    lang: str = "cc.cc14o2"
    # Runtime
    verify_timeout: int = 60
    cookie_jar: str = ""
    show_thinking: bool = True
    # Monitor
    monitor_domains: list[str] = field(default_factory=lambda: ["system"])
    monitor_interval: int = 120
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
    msg_push_events: dict[str, bool] = field(default_factory=dict)
    code_obfuscate: dict[str, bool] = field(default_factory=dict)

    def clone(self) -> "AppConfig":
        return AppConfig(**asdict(self))


class ConfigManager:
    """配置管理器 — 支持 JSON 文件 + 环境变量 + 热重载"""

    def __init__(self, config_path: str = "config.json", auto_reload: bool = False,
                 cli_overrides: dict | None = None):
        self._path = Path(config_path)
        self._config = AppConfig()
        self._lock = threading.Lock()
        self._mtime: float = 0
        self._mtime_cache: float = 0
        self._callbacks: list = []
        self.reload()
        # CLI 覆盖（最高优先级）
        if cli_overrides:
            with self._lock:
                for k, v in cli_overrides.items():
                    if v is None: continue
                    if hasattr(self._config, k):
                        setattr(self._config, k, v)
                    else:
                        log.warning("[!] 未知配置项 '%s'，已忽略", k)
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
    def reload(self):
        with self._lock:
            c = AppConfig()
            # 1. JSON 文件
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                field_map = {
                    "oj_root": "oj_root", "oj_base": "oj_base",
                    "username": "username", "password": "password",
                    "ai_base_url": "ai_base_url", "ai_model": "ai_model",
                    "ai_api_key": "ai_api_key", "ai_reasoning_effort": "ai_reasoning_effort",
                    "ai_max_tokens": "ai_max_tokens", "lang": "code_lang",
                    "verify_timeout": "verify_timeout",
                    "cookie_jar": "cookie_jar", "show_thinking": "show_thinking",
                    "monitor_domains": "monitor_domains",
                    "monitor_interval": "monitor_interval",
                    "monitor_state_file": "monitor_state_file",
                    "msg_whitelist": "msg_whitelist",
                    "msg_push_list": "msg_push_list",
                    "msg_superuser": "msg_superuser",
                    "msg_interval": "msg_interval",
                    "models": "models",
                    "model_router": "model_router",
                    "msg_blocked_cmds": "msg_blocked_cmds",
                    "msg_push_events": "msg_push_events",
                    "code_obfuscate": "code_obfuscate",
                    "benchmark_users": "benchmark_users",
                    "auto_supplement_testdata": "auto_supplement_testdata",
                    "max_cost_per_problem": "max_cost_per_problem",
                }
                for field, key in field_map.items():
                    if key in j and hasattr(c, field):
                        val = j[key]
                        if isinstance(getattr(c, field), int) and isinstance(val, str):
                            try: val = int(val)
                            except ValueError: continue
                        if isinstance(getattr(c, field), list) and isinstance(val, list):
                            setattr(c, field, val)
                        elif not isinstance(val, list):
                            setattr(c, field, val)
            # 2. 环境变量 (OJ_ / AI_ 前缀)
            env_map = {
                "oj_root": ["OJ_ROOT"],
                "oj_base": ["OJ_BASE"],
                "username": ["OJ_USERNAME"],
                "password": ["OJ_PASSWORD"],
                "ai_base_url": ["OJ_AI_BASE_URL", "AI_BASE_URL"],
                "ai_model": ["OJ_AI_MODEL", "AI_MODEL"],
                "ai_api_key": ["OJ_AI_API_KEY", "AI_API_KEY"],
                "ai_reasoning_effort": ["OJ_AI_REASONING_EFFORT"],
                "ai_max_tokens": ["OJ_AI_MAX_TOKENS", "AI_MAX_TOKENS"],
                "lang": ["OJ_LANG"],
                "verify_timeout": ["OJ_VERIFY_TIMEOUT", "VERIFY_TIMEOUT"],
            }
            for field, keys in env_map.items():
                if isinstance(keys, str): keys = [keys]
                for key in keys:
                    if key in os.environ and hasattr(c, field):
                        val = os.environ[key]
                        if isinstance(getattr(c, field), int):
                            try: val = int(val)
                            except ValueError: continue
                        setattr(c, field, val)
                        break
            self._config = c
            self._mtime = self._path.stat().st_mtime if self._path.exists() else 0
            self._notify()

    def save(self):
        """保存当前配置到文件（保留 models/model_router 的注释键）"""
        with self._lock:
            # 尝试读取现有文件以保留注释键
            existing = {}
            if self._path.exists():
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception: pass
            j = {
                "oj_root": self._config.oj_root,
                "oj_base": self._config.oj_base,
                "ai_base_url": self._config.ai_base_url,
                "ai_model": self._config.ai_model,
                "ai_reasoning_effort": self._config.ai_reasoning_effort,
                "ai_max_tokens": self._config.ai_max_tokens,
                "code_lang": self._config.lang,
                "username": self._config.username,
                "password": self._config.password,
                "verify_timeout": self._config.verify_timeout,
                "cookie_jar": self._config.cookie_jar,
                "show_thinking": self._config.show_thinking,
                "monitor_domains": self._config.monitor_domains,
                "monitor_interval": self._config.monitor_interval,
                "monitor_state_file": self._config.monitor_state_file,
                "msg_whitelist": self._config.msg_whitelist,
                "msg_push_list": self._config.msg_push_list,
                "msg_superuser": self._config.msg_superuser,
                "msg_interval": self._config.msg_interval,
                "models": self._config.models,
                "model_router": self._config.model_router,
                "msg_blocked_cmds": self._config.msg_blocked_cmds,
                "msg_push_events": self._config.msg_push_events,
                "code_obfuscate": self._config.code_obfuscate,
            }
            # 不保存敏感字段（凭据由 .env 管理）
            for sensitive in ("password", "ai_api_key"):
                j.pop(sensitive, None)
            # 保留原有注释键
            for k in existing:
                if k.startswith("_") and k not in j:
                    j[k] = existing[k]
            # 保留 models 和 model_router 内的注释键
            for section in ("models", "model_router"):
                if section in existing and isinstance(existing[section], dict):
                    for k, v in existing[section].items():
                        if k.startswith("_") and section in j and isinstance(j[section], dict):
                            j[section][k] = v
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(j, f, indent=2, ensure_ascii=False)
            self._mtime = self._path.stat().st_mtime

    # ---- 热重载 ----
    def _check_reload(self) -> bool:
        if not self._path.exists(): return False
        now = time.time()
        if now - self._mtime_cache < 5:
            return False
        self._mtime_cache = now
        return self._path.stat().st_mtime > self._mtime

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
                    setattr(self._config, k, v)

    def repair(self) -> list[str]:
        """补齐 config.json 中缺失的字段（用 AppConfig 默认值填充），返回新增的字段列表。"""
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            return []

        # AppConfig → JSON key 映射
        key_map = {
            "oj_root": "oj_root", "oj_base": "oj_base",
            "username": "username", "password": "password",
            "ai_base_url": "ai_base_url", "ai_model": "ai_model",
            "ai_api_key": "ai_api_key", "ai_reasoning_effort": "ai_reasoning_effort",
            "ai_max_tokens": "ai_max_tokens", "code_lang": "lang",
            "verify_timeout": "verify_timeout",
            "cookie_jar": "cookie_jar", "show_thinking": "show_thinking",
            "monitor_domains": "monitor_domains",
            "monitor_interval": "monitor_interval",
            "monitor_state_file": "monitor_state_file",
            "msg_whitelist": "msg_whitelist",
            "msg_push_list": "msg_push_list",
            "msg_superuser": "msg_superuser",
            "msg_interval": "msg_interval",
            "msg_blocked_cmds": "msg_blocked_cmds",
            "msg_push_events": "msg_push_events",
            "code_obfuscate": "code_obfuscate",
            "benchmark_users": "benchmark_users",
            "models": "models", "model_router": "model_router",
        }

        added = []
        c = self.cfg
        for json_key, attr_name in key_map.items():
            if json_key not in j and hasattr(c, attr_name):
                val = getattr(c, attr_name)
                j[json_key] = val
                added.append(json_key)

        if added:
            try:
                tmp = str(self._path) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(j, f, indent=2, ensure_ascii=False)
                Path(tmp).replace(self._path)
                log.info("[配置] 补齐 %d 个缺失字段: %s", len(added), ", ".join(added))
            except Exception as e:
                log.warning("[配置] 补齐写入失败: %s", e)

        return added

    # ── 配置校验 ──

    def validate(self) -> list[str]:
        """启动时校验配置，返回警告列表（空=无问题）"""
        warnings = []
        c = self.cfg
        if not c.oj_root.startswith("http"):
            warnings.append("oj_root 不是有效 URL")
        if not c.ai_base_url.startswith("http"):
            warnings.append("ai_base_url 不是有效 URL")
        if c.verify_timeout <= 0:
            warnings.append("verify_timeout 应 > 0")
        if not c.models:
            warnings.append("models 段为空，请配置至少一个模型")
        else:
            for name, md in c.models.items():
                if name.startswith("_"):
                    continue
                if md.get("base_url") and not str(md["base_url"]).startswith("http"):
                    warnings.append(f"模型 {name} 的 base_url 无效")
                if md.get("max_tokens", 0) <= 0:
                    warnings.append(f"模型 {name} 的 max_tokens 无效")
        # 检查 model_router tiers 引用的模型是否存在
        router = c.model_router or {}
        tiers = router.get("tiers", {})
        model_names = {k for k in c.models if not k.startswith("_")}
        for tier, name in tiers.items():
            if name and name not in model_names:
                warnings.append(f"model_router tiers.{tier} 引用了不存在的模型 '{name}'")
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
        cfg = self.cfg
        md = cfg.models.get(model_name) if cfg.models else None
        if md is None:
            return dict(base_url=cfg.ai_base_url, api_key=self.api_key,
                        max_tokens=cfg.ai_max_tokens,
                        reasoning_effort=cfg.ai_reasoning_effort, pricing={})
        return dict(
            base_url=md.get("base_url") or cfg.ai_base_url,
            api_key=self._resolve_model_api_key(md.get("api_key")) or self.api_key,
            max_tokens=md.get("max_tokens") or cfg.ai_max_tokens,
            reasoning_effort=md.get("reasoning_effort")
                if md.get("reasoning_effort") is not None else cfg.ai_reasoning_effort,
            pricing=md.get("pricing") or {},
        )

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

    def get_tier_model(self, tier: str) -> str | None:
        """获取路由层对应的模型名。优先 config.model_router.tiers，fallback 环境变量 AI_MODEL_<TIER>"""
        cfg = self.cfg
        router = cfg.model_router or {}
        tiers = router.get("tiers", {})
        if tier in tiers and tiers[tier]:
            return tiers[tier]
        return os.environ.get(f"AI_MODEL_{tier.upper()}", None)

    def get_tier_thinking(self, tier: str) -> list[str]:
        """获取路由层对应的推理强度列表"""
        cfg = self.cfg
        router = cfg.model_router or {}
        thinking_map = router.get("thinking_levels", {})
        raw = thinking_map.get(tier)
        if raw is None:
            raw = os.environ.get(f"AI_THINKING_{tier.upper()}", "")
        if not raw:
            return []
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def api_key(self) -> str:
        return self._resolve_api_key(self.cfg.ai_api_key)

    @staticmethod
    def _resolve_api_key(val: str) -> str:
        if not val: return ""
        if "." in val or val != val.upper():
            return val
        return os.environ.get(val, "") or os.environ.get("OJ_AI_API_KEY") or os.environ.get("AI_API_KEY", "")

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
