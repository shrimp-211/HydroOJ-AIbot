"""Shared AI client used by solving, editorials and test-data generation."""
import copy
import os
import sys
import re
import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from ai_protocol import build_request, extract_usage
from ai_context import repair_messages
from config_manager import ConfigManager as Config

log = logging.getLogger(__name__)

# 当 prompts.json 缺失时使用的默认提示词（与 prompts.json 内容一致）
from prompt_defaults import DEFAULT_PROMPTS


# ═══════════════════════════════════════════════════════════════
# AI 调用限流（延迟模式）
# ═══════════════════════════════════════════════════════════════
_ai_last_call = 0.0
_ai_lock = threading.Lock()


# 语言扩展名 → 代码围栏里可能出现的别名（模型经常用 cpp / C++ / cc 混写）
_LANG_ALIASES = {
    "cpp": ["cpp", "c++", "cc", "cxx", "c"],
    "python": ["python", "py", "python3"],
    "java": ["java"],
    "go": ["go", "golang"],
    "rust": ["rust", "rs"],
    "javascript": ["javascript", "js"],
    "csharp": ["csharp", "cs", "c#"],
    "php": ["php"],
    "ruby": ["ruby", "rb"],
    "pascal": ["pascal", "pas"],
}


def _code_fence_pattern(ext: str | None) -> str:
    """构造匹配 Markdown 代码块的正则。

    ext=None 时不限制语言（任意围栏），否则只匹配该语言及其常见别名。
    兼容 ``` 与 ~~~ 围栏、语言标记后附带的额外说明、大小写差异。
    """
    if not ext:
        return r"(?:```|~~~)[^\n]*\n(.*?)(?:```|~~~)"
    names = list(_LANG_ALIASES.get(ext, []))
    names.append(ext)
    lang = "|".join(re.escape(n) for n in dict.fromkeys(names))
    return rf"(?:```|~~~)\s*(?:{lang})\b[^\n]*\n(.*?)(?:```|~~~)"


def _hour_in_range(hour: int, start: int, end: int) -> bool:
    """判断整点 hour 是否落在 [start, end) 区间内，支持跨零点（如 23→7）。"""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _extract_tag_prefix(text: str) -> tuple[list[str], str]:
    """摘掉题解开头的 `[标签: a, b]` 前缀，返回 (标签列表, 剩余正文)。

    同时接受半角/全角冒号与各种空白，避免 AI 写成「[标签：x]」时该行
    残留在题解正文里。
    """
    m = re.match(r"\s*\[标签\s*[:：]\s*([^\]]*)\]\s*", text)
    if not m:
        return [], text
    tags = [t.strip() for t in re.split(r"[,，、;；]", m.group(1)) if t.strip()]
    return tags, text[m.end():].lstrip()


def _ai_delay():
    global _ai_last_call
    with _ai_lock:
        now = time.monotonic()
        gap = 2.0 - (now - _ai_last_call)
        if gap > 0:
            time.sleep(gap)
        _ai_last_call = time.monotonic()


# ═══════════════════════════════════════════════════════════════
# Config — 集中配置管理（命令行 > 环境变量 > config.json > 默认值）
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# AIClient — 封装 AI API 调用
# ═══════════════════════════════════════════════════════════════
class AIClient:
    def __init__(self, config: Config):
        self.config = config
        self._clients: dict[tuple, object] = {}  # lazy cache by (base_url, api_key)
        self._client_lock = threading.Lock()
        self._show_thinking = config.get("show_thinking", False)
        self._unknown_price_warned = set()

    def _get_client(self, model_name: str = ""):
        """获取或创建 OpenAI 客户端，按 (base_url, api_key) 缓存"""
        name = model_name or self.config["ai_model"]
        settings = self.config.get_model_config(name)
        base_url, api_key = settings["base_url"], settings["api_key"]
        timeout = float(settings.get("timeout", 180))
        retries = int(settings.get("max_retries", 2))
        cache_key = (base_url, api_key, timeout, retries)
        with self._client_lock:  # 多线程求解时避免重复创建/竞态写入
            if cache_key not in self._clients:
                self._clients[cache_key] = OpenAI(api_key=api_key, base_url=base_url,
                                                  timeout=timeout, max_retries=retries) if api_key else None
            return self._clients[cache_key]

    @staticmethod
    def _load_prompts() -> dict:
        try:
            with open(Path(__file__).with_name("prompts.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    _cached_prompts: dict | None = None
    _prompts_lock = threading.Lock()

    @staticmethod
    def reload_class_prompts():
        """热重载提示词（供 WebUI / CLI 调用）"""
        AIClient._cached_prompts = None  # 下次访问时重新加载
        import logging as _log
        _log.getLogger(__name__).info("[+] 提示词已标记重载，下次调用生效")

    def _p(self, key: str, default: str = "") -> str:
        with AIClient._prompts_lock:
            if AIClient._cached_prompts is None:
                AIClient._cached_prompts = self._load_prompts()
        return AIClient._cached_prompts.get(key) or DEFAULT_PROMPTS.get(key, default)

    @property
    def SYS_SOLVE(self): return self._p("system_solve")
    @property
    def SYS_SOLVE_EASY(self): return self._p("system_solve_easy")
    @property
    def SYS_SOLVE_HARD(self): return self._p("system_solve_hard")
    @property
    def SYS_FIX(self): return self._p("system_fix")

    def _tag_prompt(self, tags: list[str]) -> str:
        if not tags: return ""
        return (
            f"\n\n## 题目标签\n候选标签: {', '.join(tags)}\n"
            "请从候选标签中选出最符合本题的标签。\n"
            "在解题思路开头，先以 `[标签: xxx, xxx, xxx]` 格式输出最终标签，再写解题思路。"
        )

    def generate(self, problem: dict, use_stream: bool = False,
                 difficulty: int = 0, candidate_tags: list[str] = None,
                 model: str = "", effort: str = "") -> dict | None:
        """
        difficulty: 0=未判断/默认, 1-8 (8级制)
        candidate_tags: 难度评定时筛选的候选标签，解题模型终选
        """
        log.info("[*] 调用 AI 生成题解和代码 (题面 %d 字符, 难度%d) ...",
                 len(problem.get('content', '')), difficulty)
        log.debug("[AI] generate: difficulty=%d candidate_tags=%s",
                  difficulty, candidate_tags)
        ext = self.config.lang_ext
        info_parts = []
        if problem.get("time_limit"):
            info_parts.append(f"时限 {problem['time_limit']}")
        if problem.get("memory_limit"):
            info_parts.append(f"内存 {problem['memory_limit']}")
        if problem.get("io_method"):
            info_parts.append(f"IO: {problem['io_method']}")
        info_block = " | ".join(info_parts) if info_parts else ""

        io_hint = problem.get('io_method') and f"IO方式: {problem['io_method']}，注意文件读写" or '使用标准输入输出'

        # 按 8 级难度选择提示词: 1-2→easy, 3-5→generate, 6-8→hard
        if difficulty >= 6:
            tpl_key, sys_msg = "generate_hard", self.SYS_SOLVE_HARD
        elif difficulty >= 3:
            tpl_key, sys_msg = "generate", self.SYS_SOLVE
        elif difficulty >= 1:
            tpl_key, sys_msg = "generate_easy", self.SYS_SOLVE_EASY
        else:
            tpl_key, sys_msg = "generate", self.SYS_SOLVE
        template = self._p(tpl_key, "") or DEFAULT_PROMPTS.get(tpl_key, "")
        if not template:
            template = self._p("generate", "") or DEFAULT_PROMPTS.get("generate", "")
            sys_msg = self.SYS_SOLVE

        prompt = template.format(title=problem['title'], info=info_block,
            content=problem['content'], time_limit=problem.get('time_limit','?'),
            memory_limit=problem.get('memory_limit','?'), io_hint=io_hint, ext=ext)
        if candidate_tags:
            prompt += self._tag_prompt(candidate_tags)
        # 题面图片：多模态模型优先携带（_call_ai 内部按模型能力过滤）
        images = problem.get("images") or []
        return self._call_ai(prompt, sys_msg, use_stream, images=images,
                             model=model, effort=effort)

    def fix(self, problem: dict, code: str, solution_md: str,
            verdict: dict, retry_num: int = 1, use_stream: bool = False,
            history: list | None = None, difficulty: int = 0,
            model: str = "", effort: str = "") -> dict | None:
        """修正代码。无上下文时直接重新 generate 而非修正。"""
        log.info("[*] 第%d次修正：反馈错误给 AI ...", retry_num)
        ext = self.config.lang_ext

        # 无上下文 → 直接重新生成，不修正
        if not history:
            log.info("[*] 无上下文，重新调用 generate ...")
            return self.generate(problem, use_stream=use_stream, difficulty=difficulty,
                                 model=model, effort=effort)

        # 连续对话修正
        ce_info = verdict.get("compiler_text", "")
        ce_section = ""
        if ce_info:
            ct = ce_info if isinstance(ce_info, str) else (ce_info[0] if ce_info else "")
            ce_section = f"\n### 编译错误\n```\n{str(ct)[:1500]}\n```\n"

        error_type_hint = ""
        score = verdict.get("score", 0)
        if ce_info:
            error_type_hint = "\n> 失败类型: **编译错误** — 检查语法、头文件、类型定义"
        elif score == 0:
            error_type_hint = "\n> 失败类型: 可能是**运行时错误**或**逻辑完全错误** — 检查数组越界、空指针、算法正确性"
        elif score < 100:
            error_type_hint = "\n> 失败类型: **部分正确(WA/TLE)** — 检查边界条件、特殊case、算法复杂度"

        fmt = dict(score=score, ce_section=ce_section,
                   errors=verdict.get("errors_text", verdict.get("case_summary", "")),
                   error_hint=error_type_hint, ext=ext)

        tpl = self._p("fix_with_history", DEFAULT_PROMPTS.get("fix_with_history", ""))
        fix_prompt = tpl.format(**fmt)
        messages = (repair_messages(history, code, ext, fix_prompt)
                    if self.config.get("compact_history", True) else
                    history + [{"role": "user", "content": fix_prompt}])
        return self._call_ai_with_messages(
            messages, use_stream,
            images=problem.get("images") or [], model=model, effort=effort)

    def obfuscate(self, code: str, model: str = "", effort: str = "") -> dict | None:
        """混淆代码：仅将代码发给 AI 要求强力混淆"""
        log.info("[*] 调用 AI 混淆代码 ...")
        ext = self.config.lang_ext
        tpl = self._p("obfuscate", DEFAULT_PROMPTS.get("obfuscate", ""))
        prompt = tpl.format(ext=ext, code=code)
        sys_msg = self._p("system_obfuscate", DEFAULT_PROMPTS.get("system_obfuscate", ""))
        result = self._call_ai(prompt, sys_msg, use_stream=False,
                               model=model, effort=effort)
        if result:
            result["code"] = result.get("code", "") or code
        return result

    def _build_args(self, messages: list, model: str = "", effort: str = "",
                    max_tokens: int = 0) -> dict:
        model = model or self.config["ai_model"]
        return build_request(model, messages, self.config.get_model_config(model),
                             max_tokens=max_tokens or self.config.task_token_limit(), effort=effort)

    _extract_usage = staticmethod(extract_usage)

    def chat(self, messages: list, model: str = "", use_stream: bool = False,
             max_tokens: int = 0, effort: str = "") -> dict | None:
        """One request path for every caller. Failed responses never contain usable code."""
        model = model or self.config["ai_model"]
        t_start = time.monotonic()
        try:
            if self._get_client(model) is None:
                log.error("[-] API Key 未配置: %s", model)
                return None
            args = self._build_args(messages, model, effort, max_tokens)
            if os.environ.get("OJ_DELAY_MODE") == "1":
                _ai_delay()
            content, reasoning, usage_obj = (self._stream_call(args, model=model) if use_stream
                                             else self._block_call(args, model=model))
        except Exception as exc:
            # Never log exception bodies: some gateways echo credentials/request content.
            status = getattr(exc, "status_code", None)
            log.error("[-] AI 请求失败: model=%s type=%s status=%s",
                      model, type(exc).__name__, status)
            if status == 429:
                return {"_rate_limited": True, "content": "", "usage": {}, "cost": 0,
                        "elapsed_s": time.monotonic() - t_start, "model": model}
            return None
        if reasoning and self._show_thinking and not use_stream:
            log.info("[AI 思考] %s", reasoning[:3000])
        usage = self._extract_usage(usage_obj)
        cost, cost_known = self._calc_cost(usage, model, with_status=True)
        cost_known = cost_known and bool(usage)
        elapsed = time.monotonic() - t_start
        if not content:
            log.warning("[-] AI 未返回完整正文: %s（已记录用量）", model)
        cost_text = f"¥{cost:.4f}" if cost_known else "未知（定价或用量不完整）"
        log.info("[+] %s 耗时%.1fs | Token %di/%do | 费用%s",
                 model, elapsed, usage.get("input", 0), usage.get("output", 0), cost_text)
        return {"content": content, "usage": usage, "cost": cost, "cost_known": cost_known,
                "elapsed_s": elapsed, "model": model, "_incomplete": not bool(content)}

    def _calc_cost(self, usage: dict, model: str = "", *, with_status=False):
        """根据模型定价计算费用（元），支持多峰值峰谷价"""
        model = model or self.config["ai_model"]
        pricing = self.config.get_model_pricing(model)
        if not pricing:
            if model not in self._unknown_price_warned:
                log.warning("[费用] %s 定价未知，费用与预算可能低估；请运行 config_cli.py prices 检查", model)
                self._unknown_price_warned.add(model)
            return (0.0, False) if with_status else 0.0
        now = datetime.now().hour
        inp_price = pricing.get("input", 0)
        out_price = pricing.get("output", 0)
        cache_price = pricing.get("cache_hit")
        peaks = pricing.get("peaks", [])
        if peaks:
            for peak in peaks:
                hours = peak.get("hours", [])
                if len(hours) >= 2 and _hour_in_range(now, hours[0], hours[1]):
                    inp_price = peak.get("input", inp_price)
                    out_price = peak.get("output", out_price)
                    if "cache_hit" in peak:
                        cache_price = peak["cache_hit"]
                    break
        else:
            ph = pricing.get("peak_hours", [])
            if ph and len(ph) >= 2 and _hour_in_range(now, ph[0], ph[1]):
                inp_price = pricing.get("peak_input", inp_price)
                out_price = pricing.get("peak_output", out_price)
                if "peak_cache_hit" in pricing:
                    cache_price = pricing["peak_cache_hit"]
        cached = min(usage.get("cache_hit", 0), usage.get("input", 0))
        if cache_price is None:
            cache_price = inp_price
        cost = ((usage.get("input", 0) - cached) * inp_price +
                usage.get("output", 0) * out_price + cached * cache_price) / 1_000_000
        cost = round(cost, 6)
        return (cost, True) if with_status else cost

    @staticmethod
    def _extract_code(content: str, ext: str) -> str:
        """从 AI 响应中提取最后一个代码块。

        宽松匹配围栏：``` 或 ~~~、语言标记后可能跟额外说明（```cpp title=x）、
        大小写混用（```C++）。真实模型输出经常带这些变体。
        优先取与目标语言匹配的块；没有匹配时才回退到任意语言的块。
        """
        for pattern in (_code_fence_pattern(ext), _code_fence_pattern(None)):
            blocks = list(re.finditer(pattern, content, re.DOTALL | re.IGNORECASE))
            if blocks:
                return blocks[-1].group(1).strip()
        return ""

    def _call_ai_with_messages(self, messages: list, use_stream: bool = False,
                               images: list | None = None, model: str = "",
                               effort: str = "", task: str = "solve") -> dict | None:
        model = model or self.config["ai_model"]
        messages = copy.deepcopy(messages)
        if images and self.config.is_vision_model(model):
            # 将图片追加到最近的 user 消息（图片仅允许出现在 user 消息）
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    cur = messages[i]["content"]
                    if isinstance(cur, str):
                        content = [{"type": "text", "text": cur}]
                    elif isinstance(cur, list):
                        content = list(cur)
                    else:
                        break
                    existing = {part.get("image_url", {}).get("url") for msg in messages
                                for part in (msg.get("content") if isinstance(msg.get("content"), list) else [])
                                if part.get("type") == "image_url"}
                    for img in images:
                        if img and img not in existing:
                            content.append({"type": "image_url", "image_url": {"url": img}})
                            existing.add(img)
                    messages[i]["content"] = content
                    break
        r = self.chat(messages, model=model, use_stream=use_stream, effort=effort,
                      max_tokens=self.config.task_token_limit(task))
        if not r or r.get("_rate_limited"):
            return r
        code = self._extract_code(r["content"], self.config.lang_ext)
        return {"solution_md": r["content"], "code": code, "raw": r["content"],
                "usage": r["usage"], "elapsed_s": r["elapsed_s"], "cost": r["cost"],
                "cost_known": r.get("cost_known", False),
                "model": r.get("model", model or self.config["ai_model"])}

    # ---- 核心调用 ----
    def _call_ai(self, user_prompt: str, system_msg: str,
                 use_stream: bool = False, images: list | None = None,
                 model: str = "", effort: str = "", task: str = "solve") -> dict | None:
        return self._call_ai_with_messages(
            [{"role": "system", "content": system_msg},
             {"role": "user", "content": user_prompt}],
            use_stream, images=images, model=model, effort=effort, task=task)

    def _stream_call(self, args: dict, *, model: str = "") -> tuple:
        name = model or args["model"]
        client = self._get_client(name)
        options = {"stream": True}
        if self.config.get_model_config(name).get("stream_usage", True):
            options["stream_options"] = {"include_usage": True}
        stream = client.chat.completions.create(**args, **options)
        content, reasoning, usage, finish = [], [], None, None
        try:
            for chunk in stream:
                # The final usage-only chunk has no choices.
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish = getattr(choice, "finish_reason", None) or finish
                delta = choice.delta
                if delta:
                    c = getattr(delta, "content", None) or ""
                    r = getattr(delta, "reasoning_content", None) or ""
                    content.append(c)
                    reasoning.append(r)
                    if r and self._show_thinking:
                        self._safe_write(r)
                    if c:
                        self._safe_write(c)
                        sys.stdout.flush()
        finally:
            stream.close()
        if finish != "stop":
            log.warning("[AI] 流式回复未正常结束: %s", finish)
            return "", "", usage
        return "".join(content), "".join(reasoning), usage

    def _block_call(self, args: dict, *, model: str = "") -> tuple:
        resp = self._get_client(model or args["model"]).chat.completions.create(**args)
        if not resp.choices:
            return "", "", resp.usage
        choice = resp.choices[0]
        if choice.finish_reason != "stop":
            log.warning("[AI] 回复未正常结束: %s", choice.finish_reason)
            return "", "", resp.usage
        return (choice.message.content or "",
                getattr(choice.message, "reasoning_content", "") or "", resp.usage)

    def close(self):
        """Release HTTP pools when a long-lived worker shuts down."""
        with self._client_lock:
            for client in self._clients.values():
                if client is not None:
                    client.close()
            self._clients.clear()

    @staticmethod
    def _safe_write(s: str):
        try: sys.stdout.write(s)
        except UnicodeEncodeError: sys.stdout.write(s.encode("ascii", errors="replace").decode("ascii"))

    @staticmethod
    def _flush_lines(tag: str, text: str) -> str:
        lines = text.split("\n")
        for line in lines[:-1]:
            if line.strip():
                AIClient._safe_write(f"{tag}{line.strip()}\n"); sys.stdout.flush()
        remainder = lines[-1]
        if len(remainder) >= 100:
            AIClient._safe_write(f"{tag}{remainder}\n"); sys.stdout.flush()
            remainder = ""
        return remainder

    @staticmethod
    def _flush_remainder(text: str):
        if text.strip():
            AIClient._safe_write(text.rstrip() + "\n"); sys.stdout.flush()


