"""订阅额度与账户余额（参照 cc-switch 的 Quotas & Balances）。

三种额度：

* ``window`` — 本地用量窗口（5 小时 / 每日 / 每周 / 每月），按费用、token
  或请求数计算剩余额度，并给出重置倒计时。
* ``balance`` — 账户余额：手工、DeepSeek、StepFun、SiliconFlow（国内/国际）、
  OpenRouter、Novita、小米 MiMo、智谱 GLM、New API、Sub2API、generic。
* ``plan`` — 供应商套餐额度（Token Plan / Coding Plan）：Kimi、智谱 GLM、
  MiniMax、OpenCode Go、Ollama Cloud、小米 MiMo Token Plan。返回 5 小时 /
  每周 / 每月 / credits 多档剩余百分比与重置时间。

远端结果带短期缓存，失败时回退到最近一次有效快照并标注过期；查询不会再
跟随重定向，错误信息不会包含密钥。官方 OAuth 订阅（Claude/Codex/Gemini/
Grok）与需要 AK/SK 签名的火山方舟不在本模块覆盖范围内。
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from hydroai.config.io import env_reference
from hydroai.usage.store import get_store

log = logging.getLogger(__name__)

BALANCE_SOURCES = {
    "manual", "deepseek", "stepfun", "siliconflow", "openrouter", "novita",
    "mimo", "zhipu", "newapi", "sub2api", "generic",
}
PLAN_SOURCES = {"kimi", "zhipu_plan", "minimax", "opencode_go", "ollama", "mimo_plan"}
SOURCES = BALANCE_SOURCES | PLAN_SOURCES
WINDOW_UNITS = {"cost", "tokens", "requests"}
PLAN_UNITS = {"percent"}
PERIODS = {"5h", "daily", "weekly", "monthly", "rolling"}


def _num(value):
    if type(value) in (int, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _json_path(data, path):
    """按 ``a.b.0.c`` 读取嵌套 JSON；失败返回 None。"""
    if not path:
        return None
    value = data
    for part in str(path).split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return value


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    except (ValueError, TypeError):
        pass
    return ""


def _percent(value):
    """把任意数值/字符串转成 0–100 的百分比；无法解析返回 None。"""
    number = _num(value)
    if number is None:
        return None
    return max(0.0, min(100.0, number))


def _reset_epoch(value, *, default_utc=False):
    """统一重置时间：毫秒/秒时间戳、ISO8601、``YYYY-MM-DD HH:mm:ss``。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        number = _num(text)
        if number is not None:
            return None if number <= 0 else (number / 1000.0 if number > 1e12 else number)
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        if default_utc:
            naive = naive.replace(tzinfo=timezone.utc)
        return naive.timestamp()
    number = _num(value)
    if number is None or number <= 0:
        return None
    return number / 1000.0 if number > 1e12 else number


def _make_tier(name, *, remaining=None, used=None, reset_at=None):
    """构造一个额度档位；remaining/used 至少给一个（百分比）。"""
    if remaining is None and used is not None:
        remaining = 100.0 - used
    if used is None and remaining is not None:
        used = 100.0 - remaining
    if remaining is None:
        return None
    remaining = max(0.0, min(100.0, float(remaining)))
    used = max(0.0, min(100.0, float(used)))
    return {"name": name, "used_percent": round(used, 2),
            "remaining_percent": round(remaining, 2), "reset_at": reset_at}


def _normalize(qid: str, raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("额度配置必须是对象")
    kind = str(raw.get("kind") or raw.get("type") or "").lower()
    if kind in ("subscription", "coding_plan", "token_plan", "quota"):
        kind = "plan"
    if not kind:
        kind = "window" if (raw.get("limit") is not None or raw.get("window_hours")
                            or raw.get("period")) else "balance"
    if kind in ("currency", "account", "credit"):
        kind = "balance"
    if kind not in ("balance", "window", "plan"):
        raise ValueError(f"未知额度类型: {kind}")
    if kind == "balance":
        source = str(raw.get("source") or "manual").lower()
        if source not in BALANCE_SOURCES:
            raise ValueError(f"未知余额来源: {source}")
    elif kind == "plan":
        source = str(raw.get("source") or "").lower()
        if source not in PLAN_SOURCES:
            raise ValueError(f"套餐额度 source 必须是: {', '.join(sorted(PLAN_SOURCES))}")
    else:
        source = "local"
    unit = str(raw.get("unit") or ("cost" if kind == "window" else
                                   "percent" if kind == "plan" else "currency")).lower()
    if kind == "window" and unit not in WINDOW_UNITS:
        raise ValueError(f"未知额度单位: {unit}")
    if kind == "plan":
        unit = "percent"
    limit = _num(raw.get("limit"))
    window_hours = _num(raw.get("window_hours"))
    period = str(raw.get("period") or "").lower()
    if kind == "window":
        if period == "5h":
            window_hours = window_hours or 5.0
        if window_hours is None and not period:
            period = "5h"
            window_hours = 5.0
        if window_hours is not None and window_hours <= 0:
            raise ValueError("window_hours 必须大于 0")
        if period and period not in PERIODS:
            raise ValueError(f"未知统计周期: {period}")
        if limit is None or limit <= 0:
            raise ValueError("window 额度必须配置正的 limit")
    models = [m for m in (raw.get("models") or []) if isinstance(m, str) and m]
    threshold = _num(raw.get("low_threshold"))
    ratio = _num(raw.get("low_ratio"))
    if ratio is not None and ratio > 1:
        ratio = ratio / 100.0
    default_currency = ("USD" if source in ("openrouter", "novita", "newapi", "sub2api")
                        else "CNY")
    return {
        "id": qid,
        "kind": kind,
        "source": source,
        "unit": unit,
        "models": models,
        "currency": (None if kind == "plan" else
                     str(raw.get("currency") or default_currency)),
        "limit": limit,
        "balance": _num(raw.get("balance") if raw.get("balance") is not None else raw.get("remaining")),
        "low_threshold": threshold if threshold is not None else 0.0,
        "low_ratio": ratio if ratio is not None else 0.1,
        "window_hours": window_hours,
        "period": period,
        "base_url": str(raw.get("base_url") or ""),
        "api_key": raw.get("api_key"),
        "endpoint": str(raw.get("endpoint") or ""),
        "json_path": str(raw.get("json_path") or ""),
        "used_json_path": str(raw.get("used_json_path") or ""),
        "total_json_path": str(raw.get("total_json_path") or ""),
        "headers": raw.get("headers") if isinstance(raw.get("headers"), dict) else {},
        "cache_seconds": _num(raw.get("cache_seconds")) or 300.0,
        "timeout": _num(raw.get("timeout")) or 10.0,
        "updated": str(raw.get("updated") or ""),
        "note": str(raw.get("note") or ""),
    }


def _calendar_bounds(period: str, now: float):
    moment = datetime.fromtimestamp(now)
    if period == "daily":
        start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start.timestamp(), end.timestamp()
    if period == "weekly":
        start = (moment - timedelta(days=moment.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=7)).timestamp()
    if period == "monthly":
        start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        year, month = start.year, start.month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        end = start.replace(year=year, month=month)
        return start.timestamp(), end.timestamp()
    return None, None


class QuotaManager:
    """计算并缓存各额度状态。配置可以是 ConfigManager 或 AppConfig。"""

    def __init__(self, config, store=None):
        self.config = config
        self._cfg = getattr(config, "cfg", config)
        self.store = store or get_store()
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ── 配置访问 ──
    def _model_settings(self, name: str) -> dict:
        if hasattr(self.config, "get_model_config"):
            try:
                return self.config.get_model_config(name)
            except (KeyError, TypeError, AttributeError):
                pass
        models = getattr(self._cfg, "models", {}) or {}
        md = models.get(name) if isinstance(models, dict) else None
        if not isinstance(md, dict):
            md = {}
        return {
            "base_url": md.get("base_url") or getattr(self._cfg, "ai_base_url", ""),
            "api_key": md.get("api_key") if md.get("api_key") is not None
            else getattr(self._cfg, "ai_api_key", ""),
        }

    def _resolve_secret(self, value) -> str:
        if not value:
            return ""
        ref = env_reference(value)
        return os.environ.get(ref, "") if ref is not None else str(value)

    def _api_key(self, defn: dict) -> str:
        # 显式配置了 api_key 时只认它；引用缺失就报错，不挪用其他供应商的密钥。
        if defn.get("api_key"):
            return self._resolve_secret(defn["api_key"])
        for model in defn["models"]:
            key = self._model_settings(model).get("api_key") or ""
            key = self._resolve_secret(key)
            if key:
                return key
        # 只有未指定任何模型时才回退到全局密钥。
        return "" if defn["models"] else self._resolve_secret(
            getattr(self._cfg, "ai_api_key", ""))

    def _base_url(self, defn: dict) -> str:
        if defn["base_url"]:
            return defn["base_url"]
        for model in defn["models"]:
            base = self._model_settings(model).get("base_url") or ""
            if base:
                return base
        # 不借用全局 AI 地址：内置余额源会使用各自官方端点。
        return ""

    # ── 远端查询 ──
    @staticmethod
    def _request_json(url: str, *, headers: dict, timeout: float) -> dict:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise ValueError("额度接口返回重定向，已拒绝跟随")
        if response.status_code in (401, 403):
            raise ValueError(f"额度接口鉴权失败（HTTP {response.status_code}）")
        if response.status_code != 200:
            raise ValueError(f"额度接口返回 HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ValueError("额度接口返回非 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("额度接口返回格式不受支持")
        return data

    def _remote_balance(self, defn: dict) -> dict:
        source = defn["source"]
        if source == "manual":
            if defn["balance"] is None:
                raise ValueError("手工余额未配置 balance")
            return {"remaining": defn["balance"], "currency": defn["currency"],
                    "source": "manual", "updated_at": defn["updated"] or None}
        key = self._api_key(defn)
        if not key:
            raise ValueError("未配置用于查询余额的 API Key")
        base = self._base_url(defn)
        root = _origin(base)
        common = {"Accept": "application/json", **defn["headers"]}
        auth = {**common, "Authorization": f"Bearer {key}"}
        timeout = defn["timeout"]

        if source == "deepseek":
            root = root or "https://api.deepseek.com"
            data = self._request_json(root.rstrip("/") + "/user/balance",
                                      headers=auth, timeout=timeout)
            infos = data.get("balance_infos") or []
            if not isinstance(infos, list):
                raise ValueError("DeepSeek 余额响应缺少 balance_infos")
            info = next((i for i in infos if isinstance(i, dict)
                         and i.get("currency") == defn["currency"]),
                        infos[0] if infos else {})
            remaining = _num(info.get("total_balance")) if isinstance(info, dict) else None
            if remaining is None:
                raise ValueError("DeepSeek 余额响应缺少 total_balance")
            return {"remaining": remaining,
                    "currency": info.get("currency") or defn["currency"],
                    "source": "deepseek",
                    "is_available": bool(data.get("is_available", True)),
                    "note": "官方余额接口"}
        if source == "stepfun":
            url = defn["endpoint"] or "https://api.stepfun.com/v1/accounts"
            data = self._request_json(url, headers=auth, timeout=timeout)
            remaining = _num(data.get("balance"))
            if remaining is None:
                raise ValueError("StepFun 余额响应缺少 balance")
            return {"remaining": remaining, "currency": "CNY",
                    "source": "stepfun", "note": "StepFun 账户余额"}
        if source == "openrouter":
            url = defn["endpoint"] or "https://openrouter.ai/api/v1/credits"
            data = self._request_json(url, headers=auth, timeout=timeout)
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            total = _num(body.get("total_credits"))
            used = _num(body.get("total_usage"))
            if total is None:
                raise ValueError("OpenRouter 余额响应缺少 total_credits")
            remaining = total - (used or 0.0)
            return {"remaining": remaining, "limit": total, "used": used,
                    "currency": "USD", "source": "openrouter", "note": "credits 余额"}
        if source == "siliconflow":
            root = root or "https://api.siliconflow.cn"
            unit = "USD" if "siliconflow.com" in root else "CNY"
            data = self._request_json(root.rstrip("/") + "/v1/user/info",
                                      headers=auth, timeout=timeout)
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            remaining = _num(body.get("totalBalance"))
            if remaining is None:
                remaining = _num(body.get("balance"))
            if remaining is None:
                remaining = _num(body.get("chargeBalance"))
            if remaining is None:
                raise ValueError("SiliconFlow 余额响应缺少余额字段")
            return {"remaining": remaining, "currency": unit, "source": "siliconflow",
                    "note": "账户余额"}
        if source == "novita":
            url = defn["endpoint"] or "https://api.novita.ai/v3/user/balance"
            data = self._request_json(url, headers=auth, timeout=timeout)
            raw = _num(data.get("availableBalance"))
            if raw is None:
                raise ValueError("Novita 余额响应缺少 availableBalance")
            return {"remaining": raw / 10000.0, "currency": "USD",
                    "source": "novita", "note": "Novita AI 余额（0.0001 USD）"}
        if source == "mimo":
            host = root or "https://api.xiaomimimo.com"
            data = self._request_json(host.rstrip("/") + "/v1/platform/balance",
                                      headers=auth, timeout=timeout)
            infos = data.get("balance_infos")
            if not isinstance(infos, list) or not infos:
                raise ValueError("MiMo 余额响应缺少 balance_infos")
            info = next((i for i in infos if isinstance(i, dict)
                         and i.get("currency") == defn["currency"]),
                        infos[0] if isinstance(infos[0], dict) else {})
            total = _num(info.get("total_available"))
            if total is None:
                raise ValueError("MiMo 余额响应缺少 total_available")
            frozen = _num(info.get("frozen_balance")) or 0.0
            return {"remaining": max(0.0, total - frozen),
                    "currency": info.get("currency") or defn["currency"],
                    "source": "mimo", "note": "小米 MiMo 可用余额"}
        if source == "zhipu":
            host = root or "https://open.bigmodel.cn"
            headers = {**common, "Authorization": key, "Content-Type": "application/json"}
            data = self._request_json(
                host.rstrip("/") + "/api/biz/account/query-customer-account-report",
                headers=headers, timeout=timeout)
            body = data.get("data") if isinstance(data.get("data"), dict) else data
            remaining = _num(body.get("availableBalance"))
            if remaining is None:
                remaining = _num(body.get("balance"))
            if remaining is None:
                raise ValueError("智谱余额响应缺少 availableBalance/balance")
            return {"remaining": remaining,
                    "currency": "USD" if "z.ai" in host else "CNY",
                    "source": "zhipu", "note": "智谱账户余额"}
        if source == "newapi":
            if not root:
                raise ValueError("New API 余额查询需要 base_url（站点根地址）")
            url = defn["endpoint"] or root.rstrip("/") + "/api/user/self"
            data = self._request_json(url, headers=auth, timeout=timeout)
            if data.get("success") is not True and not isinstance(data.get("data"), dict):
                raise ValueError("New API 返回未登录或格式不受支持")
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            quota = _num(body.get("quota"))
            used = _num(body.get("used_quota"))
            if quota is None and used is None:
                raise ValueError("New API 响应缺少 quota/used_quota")
            remaining = (quota or 0.0) / 500000.0
            used_usd = (used or 0.0) / 500000.0
            return {"remaining": remaining, "used": used_usd,
                    "limit": remaining + used_usd, "currency": "USD",
                    "source": "newapi",
                    "note": str(body.get("group") or body.get("username") or "New API")}
        if source == "sub2api":
            if not root:
                raise ValueError("Sub2API 余额查询需要 base_url（站点地址）")
            site = root.rstrip("/")
            if site.endswith("/v1"):
                site = site[:-3]
            url = defn["endpoint"] or site + "/v1/usage"
            data = self._request_json(url, headers=auth, timeout=timeout)
            if data.get("isValid") is False:
                raise ValueError(f"Sub2API 返回无效：{str(data.get('status') or '')[:60]}")
            quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
            remaining = _num(data.get("remaining"))
            used = _num(quota.get("used"))
            total = _num(quota.get("limit"))
            if remaining is None and total is not None and used is not None:
                remaining = total - used
            if remaining is None:
                raise ValueError("Sub2API 响应缺少 remaining/quota.limit")
            return {"remaining": remaining, "used": used, "limit": total,
                    "currency": str(data.get("unit") or quota.get("unit") or "USD"),
                    "source": "sub2api",
                    "note": str(data.get("planName") or "Sub2API")}
        # generic
        if not defn["endpoint"] or not defn["json_path"]:
            raise ValueError("generic 余额查询需要 endpoint 与 json_path")
        data = self._request_json(defn["endpoint"], headers=auth, timeout=timeout)
        remaining = _num(_json_path(data, defn["json_path"]))
        used = _num(_json_path(data, defn["used_json_path"])) if defn["used_json_path"] else None
        total = _num(_json_path(data, defn["total_json_path"])) if defn["total_json_path"] else None
        if remaining is None and total is not None and used is not None:
            remaining = total - used
        if remaining is None:
            raise ValueError("generic 余额路径没有取到数值")
        return {"remaining": remaining, "used": used, "limit": total,
                "currency": defn["currency"], "source": "generic", "note": "自定义接口"}

    def _remote_plan(self, defn: dict) -> dict:
        """查询 Token Plan / Coding Plan 多档额度。"""
        source = defn["source"]
        key = self._api_key(defn)
        if not key:
            raise ValueError("未配置用于查询套餐额度的 API Key")
        base = defn["base_url"] or self._base_url(defn)
        common = {"Accept": "application/json", **defn["headers"]}
        auth = {**common, "Authorization": f"Bearer {key}"}
        timeout = defn["timeout"]

        if source == "kimi":
            root = (base or "https://api.kimi.com/coding/v1").rstrip("/")
            data = self._request_json(root + "/usages", headers=auth, timeout=timeout)
            tiers = []
            best = None
            for item in data.get("limits") or []:
                detail = item.get("detail") if isinstance(item, dict) else None
                if not isinstance(detail, dict):
                    continue
                limit = _num(detail.get("limit"))
                remaining = _num(detail.get("remaining"))
                if limit is None or remaining is None or limit <= 0:
                    continue
                tier = _make_tier("five_hour", remaining=remaining / limit * 100.0,
                                  reset_at=_reset_epoch(detail.get("resetTime")))
                if tier and (best is None or tier["remaining_percent"] < best["remaining_percent"]):
                    best = tier
            if best:
                tiers.append(best)
            usage = data.get("usage")
            if isinstance(usage, dict):
                limit = _num(usage.get("limit"))
                remaining = _num(usage.get("remaining"))
                if limit and remaining is not None and limit > 0:
                    tier = _make_tier("weekly", remaining=remaining / limit * 100.0,
                                      reset_at=_reset_epoch(usage.get("resetTime")))
                    if tier:
                        tiers.append(tier)
            if not tiers:
                raise ValueError("Kimi 用量响应未返回可识别的额度")
            return {"tiers": tiers, "plan_name": "Kimi Code",
                    "note": "Kimi Code 用量接口"}
        if source == "zhipu_plan":
            host = "https://api.z.ai" if "z.ai" in (base or "") else "https://open.bigmodel.cn"
            headers = {**common, "Authorization": key, "Content-Type": "application/json",
                       "Accept-Language": "en-US,en"}
            data = self._request_json(host + "/api/monitor/usage/quota/limit",
                                      headers=headers, timeout=timeout)
            if data.get("success") is False:
                message = str(data.get("msg") or "")
                lowered = message.lower().replace("_", " ").replace("-", " ")
                if ("coding plan" in lowered
                        and any(word in lowered for word in
                                ("不存在", "没有", "未开通", "not exist", "not found",
                                 "not have", "no coding plan", "not subscribed"))):
                    raise ValueError("该账号没有 Coding Plan，请改用 kind=balance 查询余额")
                raise ValueError(f"智谱额度接口错误：{message[:80] or '未知错误'}")
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            slots = {"five_hour": None, "weekly": None}
            unclassified = []
            for item in body.get("limits") or []:
                if not isinstance(item, dict):
                    continue
                limit_type = str(item.get("type") or "").upper()
                if limit_type not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
                    continue
                used = _percent(item.get("percentage"))
                if used is None:
                    continue
                tier = _make_tier("", used=used,
                                  reset_at=_reset_epoch(item.get("nextResetTime")))
                if not tier:
                    continue
                unit = _num(item.get("unit"))
                name = "five_hour" if unit == 3 else "weekly" if unit == 6 else None
                if name and slots[name] is None:
                    tier["name"] = name
                    slots[name] = tier
                else:
                    unclassified.append(tier)
            for tier in unclassified:
                for name in ("five_hour", "weekly"):
                    if slots[name] is None:
                        tier["name"] = name
                        slots[name] = tier
                        break
            tiers = [slots[name] for name in ("five_hour", "weekly") if slots[name]]
            if not tiers:
                raise ValueError("智谱额度响应未返回可识别的额度")
            return {"tiers": tiers,
                    "plan_name": str(body.get("level") or "GLM Coding Plan"),
                    "note": "智谱 Token Plan"}
        if source == "minimax":
            host = ("https://api.minimax.io" if "minimax.io" in (base or "")
                    else "https://api.minimaxi.com")
            data = self._request_json(
                host + "/v1/api/openplatform/coding_plan/remains",
                headers=auth, timeout=timeout)
            base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else {}
            code = base_resp.get("status_code", 0)
            if code not in (0, None):
                raise ValueError(f"MiniMax 额度接口错误（{code}）："
                                 f"{base_resp.get('status_msg') or ''}")
            items = data.get("model_remains")
            item = next((i for i in (items or [])
                         if isinstance(i, dict) and i.get("model_name") == "general"), None)
            if not item:
                raise ValueError("MiniMax 额度响应缺少 general 套餐")
            tiers = []
            remaining = _percent(item.get("current_interval_remaining_percent"))
            if remaining is not None:
                tiers.append(_make_tier("five_hour", remaining=remaining,
                                        reset_at=_reset_epoch(item.get("end_time"))))
            weekly_status = _num(item.get("current_weekly_status"))
            if weekly_status in (1, 2):
                remaining = _percent(item.get("current_weekly_remaining_percent"))
                if remaining is not None:
                    tiers.append(_make_tier("weekly", remaining=remaining,
                                            reset_at=_reset_epoch(item.get("weekly_end_time"))))
            if not tiers:
                raise ValueError("MiniMax 额度响应未返回可识别的额度")
            return {"tiers": tiers, "plan_name": "MiniMax Coding Plan",
                    "note": "MiniMax 额度接口"}
        if source == "opencode_go":
            data = self._request_json("https://opencode.ai/zen/go/v1/usage",
                                      headers=auth, timeout=timeout)
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            tiers = []
            for key_name, name in (("rolling", "five_hour"), ("weekly", "weekly"),
                                   ("monthly", "monthly")):
                window = usage.get(key_name)
                if not isinstance(window, dict):
                    continue
                used = _percent(window.get("percent"))
                if used is None:
                    continue
                reset_at = _reset_epoch(window.get("resetsAt")) if used > 0 else None
                tiers.append(_make_tier(name, used=used, reset_at=reset_at))
            if not tiers:
                raise ValueError("OpenCode Go 用量响应形态不受支持")
            return {"tiers": tiers, "plan_name": "OpenCode Go",
                    "note": "OpenCode Go 用量接口"}
        if source == "ollama":
            data = self._request_json("https://ollama.com/api/balance",
                                      headers=auth, timeout=timeout)
            included = data.get("included") if isinstance(data.get("included"), dict) else {}
            balance = _num(included.get("balance_usd"))
            allowance = _num(included.get("allowance_usd"))
            tiers = []
            if balance is not None and allowance is not None and allowance > 0:
                used = max(0.0, min(100.0, (allowance - balance) / allowance * 100.0))
                period = included.get("period") if isinstance(included.get("period"), dict) else {}
                tiers.append(_make_tier("monthly", used=used,
                                        reset_at=_reset_epoch(period.get("until"))))
            else:
                for key_name, name in (("session", "five_hour"), ("weekly", "weekly")):
                    window = included.get(key_name)
                    if not isinstance(window, dict):
                        continue
                    remaining = _percent(window.get("remaining_percent"))
                    if remaining is None:
                        continue
                    tiers.append(_make_tier(name, remaining=remaining,
                                            reset_at=_reset_epoch(window.get("resets_at"))))
            if not tiers:
                raise ValueError("Ollama 账号没有可显示的套餐额度（可能为按量付费）")
            return {"tiers": tiers, "plan_name": "Ollama Cloud",
                    "note": "Ollama Cloud 额度"}
        if source == "mimo_plan":
            data = self._request_json(
                "https://token-plan-cn.xiaomimimo.com/v1/platform/token_plan",
                headers=auth, timeout=timeout)
            quota = data.get("quota") if isinstance(data.get("quota"), dict) else {}
            total = _num(quota.get("total_credits"))
            if total is None or total <= 0:
                raise ValueError("MiMo Token Plan 响应缺少 total_credits")
            used = _num(quota.get("used_credits"))
            if used is None:
                remaining = _num(quota.get("remaining_credits"))
                used = (total - remaining) if remaining is not None else None
            if used is None:
                raise ValueError("MiMo Token Plan 响应缺少已用额度")
            period = data.get("period") if isinstance(data.get("period"), dict) else {}
            tier = _make_tier("credits", used=used / total * 100.0,
                              reset_at=_reset_epoch(period.get("end_at"), default_utc=True))
            return {"tiers": [tier], "plan_name": "小米 MiMo Token Plan",
                    "note": "MiMo Token Plan"}
        raise ValueError(f"未知套餐来源: {source}")

    # ── 状态计算 ──
    def _window_status(self, defn: dict, now: float) -> dict:
        period = defn["period"]
        hours = defn["window_hours"]
        if hours:
            start, end, reset_hint = now - hours * 3600, now + 1, "rolling"
        else:
            start, end = _calendar_bounds(period, now)
            if start is None:
                raise ValueError("窗口额度缺少 window_hours 或 period")
            reset_hint = "calendar"
        summary = self.store.summary(since=start, until=end, models=defn["models"])
        unit = defn["unit"]
        if unit == "cost":
            used = summary["cost"]
        elif unit == "tokens":
            used = summary["total_tokens"]
        else:
            used = summary["requests"]
        limit = defn["limit"]
        remaining = max(0.0, limit - used)
        ratio = remaining / limit if limit else 0.0
        if reset_hint == "rolling":
            first = summary.get("first_ts") or 0
            reset_at = (first + hours * 3600) if first else None
            window_label = f"{hours:g}h"
        else:
            reset_at = end
            window_label = period
        low = remaining <= defn["low_threshold"] or ratio <= defn["low_ratio"]
        return {
            "used": round(used, 6) if unit == "cost" else int(used),
            "limit": limit,
            "remaining": round(remaining, 6) if unit == "cost" else int(remaining),
            "ratio": round(max(0.0, min(1.0, ratio)), 4),
            "unit": unit,
            "window": window_label,
            "reset_at": reset_at,
            "source": "local",
            "status": "low" if low else "ok",
            "note": defn["note"] or "按本地用量统计",
        }

    def _balance_status(self, defn: dict, now: float, refresh: bool) -> dict:
        with self._lock:
            cached = self._cache.get(defn["id"])
            if cached and not refresh and cached["expires"] > now:
                return dict(cached["payload"])
        error = ""
        try:
            data = self._remote_balance(defn)
            payload = {
                "used": data.get("used"),
                "limit": data.get("limit"),
                "remaining": round(data["remaining"], 6),
                "ratio": None,
                "unit": "currency",
                "currency": data.get("currency") or defn["currency"],
                "window": None,
                "reset_at": None,
                "source": data.get("source", defn["source"]),
                "updated_at": now,
                "stale": False,
                "note": data.get("note") or defn["note"],
            }
            threshold = defn["low_threshold"]
            payload["status"] = "low" if payload["remaining"] <= threshold else "ok"
            if data.get("is_available") is False:
                payload["status"] = "low"
                payload["note"] = (payload["note"] + "；账户当前不可用").strip("；")
            if defn["source"] != "manual":
                self.store.save_quota_snapshot(defn["id"], payload, ts=now)
            with self._lock:
                self._cache[defn["id"]] = {
                    "expires": now + (defn["cache_seconds"] if defn["source"] != "manual" else 5),
                    "payload": dict(payload),
                }
            return payload
        except (ValueError, requests.RequestException, OSError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else f"余额查询失败（{type(exc).__name__}）"
            log.warning("[额度] %s 查询失败: %s", defn["id"], error)
        # 回退到最近一次有效快照
        snapshot = self.store.last_quota_snapshot(defn["id"]) if defn["source"] != "manual" else None
        if snapshot and isinstance(snapshot.get("payload"), dict):
            payload = dict(snapshot["payload"])
            payload.update({"status": "stale", "stale": True, "error": error,
                            "updated_at": snapshot.get("ts")})
            return payload
        return {"used": None, "limit": None, "remaining": None, "ratio": None,
                "unit": "currency", "currency": defn["currency"], "window": None,
                "reset_at": None, "source": defn["source"], "updated_at": now,
                "stale": False, "note": defn["note"], "status": "error", "error": error}

    def _plan_status(self, defn: dict, now: float, refresh: bool) -> dict:
        with self._lock:
            cached = self._cache.get(defn["id"])
            if cached and not refresh and cached["expires"] > now:
                return dict(cached["payload"])
        error = ""
        try:
            data = self._remote_plan(defn)
            tiers = []
            for tier in data["tiers"]:
                tier = dict(tier)
                reset_at = tier.get("reset_at")
                tier["reset_in_s"] = max(0, int(reset_at - now)) if reset_at else None
                tier["status"] = ("low" if tier["remaining_percent"] <= defn["low_ratio"] * 100
                                  else "ok")
                tiers.append(tier)
            if not tiers:
                raise ValueError("套餐额度接口未返回档位")
            worst = min(tiers, key=lambda item: item["remaining_percent"])
            resets = [item["reset_at"] for item in tiers if item.get("reset_at")]
            payload = {
                "used": worst["used_percent"],
                "limit": 100,
                "remaining": worst["remaining_percent"],
                "ratio": round(worst["remaining_percent"] / 100.0, 4),
                "unit": "percent",
                "currency": None,
                "window": None,
                "reset_at": worst.get("reset_at") or (min(resets) if resets else None),
                "source": defn["source"],
                "plan_name": str(data.get("plan_name") or ""),
                "tiers": tiers,
                "updated_at": now,
                "stale": False,
                "note": data.get("note") or defn["note"],
                "status": "low" if any(t["status"] == "low" for t in tiers) else "ok",
            }
            self.store.save_quota_snapshot(defn["id"], payload, ts=now)
            with self._lock:
                self._cache[defn["id"]] = {
                    "expires": now + defn["cache_seconds"],
                    "payload": dict(payload),
                }
            return payload
        except (ValueError, requests.RequestException, OSError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else f"套餐额度查询失败（{type(exc).__name__}）"
            log.warning("[额度] %s 查询失败: %s", defn["id"], error)
        snapshot = self.store.last_quota_snapshot(defn["id"])
        if snapshot and isinstance(snapshot.get("payload"), dict):
            payload = dict(snapshot["payload"])
            tiers = []
            for tier in payload.get("tiers", []):
                tier = dict(tier)
                reset_at = tier.get("reset_at")
                tier["reset_in_s"] = max(0, int(reset_at - now)) if reset_at else None
                tiers.append(tier)
            payload["tiers"] = tiers
            payload.update({"status": "stale", "stale": True, "error": error,
                            "updated_at": snapshot.get("ts")})
            return payload
        return {"used": None, "limit": 100, "remaining": None, "ratio": None,
                "unit": "percent", "currency": None, "window": None,
                "reset_at": None, "tiers": [], "plan_name": "",
                "source": defn["source"], "updated_at": now, "stale": False,
                "note": defn["note"], "status": "error", "error": error}

    def _status_one(self, defn: dict, now: float, refresh: bool) -> dict:
        unit = ("percent" if defn["kind"] == "plan" else
                "currency" if defn["kind"] == "balance" else defn["unit"])
        base = {
            "id": defn["id"], "kind": defn["kind"], "models": defn["models"],
            "unit": unit, "currency": defn["currency"],
            "low_threshold": defn["low_threshold"],
        }
        try:
            if defn["kind"] == "window":
                payload = self._window_status(defn, now)
            elif defn["kind"] == "plan":
                payload = self._plan_status(defn, now, refresh)
            else:
                payload = self._balance_status(defn, now, refresh)
        except ValueError as exc:
            payload = {"status": "error", "error": str(exc), "remaining": None,
                       "used": None, "limit": None, "ratio": None,
                       "unit": unit, "currency": defn["currency"],
                       "window": None, "reset_at": None, "source": defn["source"],
                       "updated_at": now, "stale": False, "note": defn["note"]}
        base.update(payload)
        reset_at = base.get("reset_at")
        base["reset_in_s"] = max(0, int(reset_at - now)) if reset_at else None
        return base

    def definitions(self) -> list[dict]:
        quotas = getattr(self._cfg, "quotas", {}) or {}
        result = []
        for qid, raw in quotas.items():
            if str(qid).startswith("_") or not isinstance(raw, dict):
                continue
            if raw.get("enabled") is False or str(raw.get("kind", "")).lower() == "off":
                continue
            try:
                result.append(_normalize(str(qid), raw))
            except (ValueError, TypeError) as exc:
                log.warning("[额度] 忽略 %s: %s", qid, exc)
        return result

    def status(self, *, refresh=False) -> list[dict]:
        now = time.time()
        return [self._status_one(defn, now, refresh) for defn in self.definitions()]

    def summary(self, *, refresh=False) -> dict:
        items = self.status(refresh=refresh)
        return {
            "items": items,
            "total": len(items),
            "low": sum(1 for item in items if item.get("status") in ("low", "error")),
            "errors": sum(1 for item in items if item.get("status") == "error"),
            "updated_at": time.time(),
        }


def quota_status(config, *, refresh=False, store=None) -> dict:
    """便捷入口：返回 ``{items, total, low, errors, updated_at}``。"""
    return QuotaManager(config, store=store).summary(refresh=refresh)
