"""AI 错误后的可用性判断与通知去重。

设计原则：
* 不在任务开始前主动消耗 token 探测；
* 只有真实 API 调用返回错误时，才检查“是否所有 AI 都不可用”；
* 优先查询已配置的额度/余额（QuotaManager），没有可用的额度信息时才
  回退到一次极小请求探测；
* 同一错误类型在配置未变更时只通知一次（通知账本存放在 usage.db meta）。
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from hydroai.ai.client import AIClient

log = logging.getLogger(__name__)

MAX_PROBE_MODELS = 12
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()


class AIUnavailableError(RuntimeError):
    """所有 AI 渠道均不可用；调用方应中止任务并发送一次通知。"""

    def __init__(self, message: str, health: dict | None = None):
        super().__init__(message)
        self.health = health or {}


def _candidate_models(config) -> list[str]:
    cfg = getattr(config, "cfg", config)
    names: list[str] = []

    def add(value) -> None:
        if isinstance(value, str):
            value = value.strip()
            if value and not value.startswith("_") and value not in names:
                names.append(value)

    add(getattr(cfg, "ai_model", ""))
    router = getattr(cfg, "model_router", {}) or {}
    tiers = router.get("tiers", {}) if isinstance(router, dict) else {}
    if isinstance(tiers, dict):
        for value in tiers.values():
            add(value)
    for attr in ("ai_vision_model", "editorial_model", "difficulty_skip_model"):
        add(getattr(cfg, attr, ""))
    models = getattr(cfg, "models", {}) or {}
    if isinstance(models, dict):
        for name in models:
            add(name)
    return names[:MAX_PROBE_MODELS]


def _cache_key(config, models: list[str]) -> tuple:
    path = str(getattr(config, "_path", ""))
    try:
        mtime = float(getattr(config, "_mtime", 0) or 0)
    except (TypeError, ValueError):
        mtime = 0.0
    return (path, round(mtime, 3), tuple(models))


def config_version(config) -> str:
    """配置版本：文件路径 + mtime；配置变更后通知账本自动失效。"""
    path = str(getattr(config, "_path", ""))
    try:
        mtime = float(getattr(config, "_mtime", 0) or 0)
    except (TypeError, ValueError):
        mtime = 0.0
    return f"{path}:{mtime:.6f}"


def _error_categories(text: str) -> set[str]:
    text = str(text or "")
    low = text.lower()
    categories: set[str] = set()
    if ("api key" in low or "401" in text or "403" in text
            or "unauthorized" in low or "forbidden" in low):
        categories.add("auth")
    if "429" in text or "rate limit" in low or "rate_limit" in low:
        categories.add("rate_limit")
    if ("timeout" in low or "connection" in low or "connect" in low
            or "network" in low):
        categories.add("network")
    if any(code in text for code in ("500", "502", "503", "504")) \
            or "internalserver" in low or "serviceunavailable" in low:
        categories.add("server")
    if ("额度" in text or "balance" in low or "credit" in low
            or "quota" in low):
        categories.add("quota")
    return categories or {"other"}


def _quota_health(config) -> tuple[bool, bool, str, set[str]]:
    """优先用额度/余额判断。返回 (是否有额度信息, 是否可用, 原因, 错误类型)。"""
    try:
        cfg = getattr(config, "cfg", config)
        if not (getattr(cfg, "quotas", None) or {}):
            return False, True, "", set()
        from hydroai.usage.quota import QuotaManager
        from hydroai.usage.store import get_store
        items = QuotaManager(config, store=get_store()).status(refresh=True)
    except Exception as exc:
        log.debug("[AI 预检] 额度查询失败: %s", type(exc).__name__)
        return False, True, "", set()

    # 本地 window 额度只反映自己的用量，不代表供应商是否可用。
    remote = [item for item in items if item.get("kind") in ("balance", "plan")]
    if not remote:
        return False, True, "", set()

    usable = []
    failed = []
    for item in remote:
        remaining = item.get("remaining")
        status = item.get("status")
        if remaining is not None and remaining > 0 and status in ("ok", "low", "stale"):
            usable.append(item)
        elif status == "ok" and remaining is None:
            # 接口成功但未给剩余值，按可用处理，避免误停任务。
            usable.append(item)
        else:
            failed.append(item)

    if usable:
        return True, True, "", set()

    parts = []
    categories: set[str] = set()
    for item in failed:
        error = str(item.get("error") or "")
        remaining = item.get("remaining")
        if remaining is not None and remaining <= 0:
            parts.append(f"{item.get('id')}: 额度用尽")
            categories.add("quota")
        elif error:
            parts.append(f"{item.get('id')}: {error}")
            categories |= _error_categories(error)
        else:
            parts.append(f"{item.get('id')}: 不可用")
            categories.add("other")
    reason = "；".join(parts[:3]) or "所有额度渠道不可用"
    return True, False, reason, categories


def _probe_model(ai: AIClient, model: str, timeout: float) -> dict:
    try:
        client = ai._get_client(model)
        if client is None:
            return {"model": model, "ok": False, "error": "API Key 未配置"}
        args = ai._build_args(
            [{"role": "user", "content": "Reply with OK."}],
            model=model,
            max_tokens=64,
        )
        # 只验证鉴权与连通性：去掉流式、推理强度、thinking，避免小预算被拒绝。
        args.pop("stream", None)
        args.pop("stream_options", None)
        args.pop("reasoning_effort", None)
        extra = args.get("extra_body")
        if isinstance(extra, dict):
            extra.pop("thinking", None)
            if not extra:
                args.pop("extra_body", None)
        client.with_options(timeout=timeout, max_retries=0).chat.completions.create(**args)
        return {"model": model, "ok": True, "error": ""}
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        detail = type(exc).__name__
        if status:
            detail += f" HTTP {status}"
        return {"model": model, "ok": False, "error": detail}


def check_ai_health(config, *, ai: AIClient | None = None, force: bool = False) -> dict:
    """返回可用性结果；优先额度查询，无额度信息时才做极小请求探测。"""
    cfg = getattr(config, "cfg", config)
    now = time.time()
    if not getattr(cfg, "ai_health_check_enable", True):
        return {"available": True, "checked": 0, "models": [], "reason": "健康检查已关闭",
                "checked_at": now, "cached": False, "skipped": True,
                "source": "disabled", "error_type": ""}

    models = _candidate_models(config)
    try:
        ttl = max(0, int(getattr(cfg, "ai_health_check_ttl", 300) or 0))
    except (TypeError, ValueError):
        ttl = 300
    try:
        timeout = min(60.0, max(3.0, float(getattr(cfg, "ai_health_check_timeout", 15) or 15)))
    except (TypeError, ValueError):
        timeout = 15.0

    key = _cache_key(config, models)
    if not force and ttl > 0:
        with _CACHE_LOCK:
            entry = _CACHE.get(key)
        if entry and now - entry[0] < ttl:
            result = dict(entry[1])
            result["cached"] = True
            return result

    handled, available, reason, categories = _quota_health(config)
    if handled:
        source = "quota"
        results = []
    else:
        source = "probe"
        if not models:
            return {"available": False, "checked": 0, "models": [],
                    "reason": "未配置任何 AI 模型", "checked_at": now,
                    "cached": False, "skipped": False, "source": source,
                    "error_type": "ai_unavailable:other"}
        ai = ai or AIClient(config)
        with ThreadPoolExecutor(max_workers=min(8, len(models)),
                                thread_name_prefix="ai-health") as pool:
            results = list(pool.map(lambda name: _probe_model(ai, name, timeout), models))
        available = any(item["ok"] for item in results)
        categories = set()
        if not available:
            errors = [f"{item['model']}: {item['error']}"
                      for item in results if item["error"]]
            reason = "；".join(errors[:3]) or "所有模型均返回失败"
            for item in results:
                if not item["ok"]:
                    categories |= _error_categories(item["error"])

    error_type = "ai_unavailable:" + "+".join(sorted(categories) or ["unknown"])
    if not available:
        log.warning("[AI 预检] 所有 AI 不可用（%s）: %s", source, reason)

    result = {
        "available": available,
        "checked": len(results),
        "models": results,
        "reason": reason,
        "checked_at": now,
        "cached": False,
        "skipped": False,
        "source": source,
        "error_type": error_type,
    }
    if ttl > 0:
        with _CACHE_LOCK:
            _CACHE[key] = (now, dict(result))
    return result


def failure_message(health: dict) -> str:
    reason = str(health.get("reason") or "未知原因")
    if len(reason) > 300:
        reason = reason[:297] + "..."
    return ("❌ 所有 AI 接口均不可用，已暂停任务。\n"
            "请检查 API Key、网络连接或账户额度后重试。\n"
            f"原因：{reason}")


def should_notify(config, health: dict, *, channel: str = "default") -> bool:
    """同一错误类型 + 配置版本 + 通道只允许通知一次。"""
    key = str(health.get("error_type") or "ai_unavailable:unknown")
    key = f"{key}:{channel}"
    try:
        from hydroai.usage.store import get_store
        return get_store().notification_allowed(key, config_version(config))
    except Exception:
        return True


def clear_health_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
