#!/usr/bin/env python3
"""OJ 自动解题 & 题解发布脚本 — Hydro OJ 平台"""

import os
import sys
import re
import json
import time
import logging
import threading
import argparse
import requests
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from oj_common import load_dotenv, create_session, save_cookies, load_cookies, parse_problem_url, oj_login
from config_manager import ConfigManager

log = logging.getLogger(__name__)
Config = ConfigManager

# 当 prompts.json 缺失时使用的默认提示词（与 prompts.json 内容一致）
DEFAULT_PROMPTS = {
    "system_solve": "你是算法竞赛讲师，擅长用通俗语言讲解算法。\n\n## 讲解风格\n- 面向学习者：解释「为什么这样想」而非只给答案\n- 从朴素思路出发，逐步优化到最优解\n- 关键步骤用具体例子演示，而非抽象描述\n- 数学推导配文字解释，公式用 $...$ 或 $$...$$ 包裹\n\n## 工作流程\n1. 仔细阅读题目，提取关键信息（数据范围、时限、内存）\n2. 推演至少 2 种解法，讲解为什么选择最优的一种\n3. 用题目样例手动模拟算法过程\n4. 枚举边界和极端情况，确认处理正确\n5. 写出完整代码\n\n## 代码铁律\n- 使用 long long 处理所有可能超过 2e9 的整数\n- 数组大小 = 最大数据量 + 5 的余量\n- 多组测试用例时，所有全局/静态变量必须重置\n- 循环内避免 endl，用 '\\n' 防止 TLE\n- 大输入（>10^5）使用 ios::sync_with_stdio(false); cin.tie(nullptr)\n- 浮点数比较用 fabs(a-b) < 1e-9\n- 取模运算每一步都取模，避免溢出\n\n## 输出要求\n- 代码前必须有「解题思路」段（讲解式，说清来龙去脉）\n- 代码块用 ```{ext} 包裹，包含所有必要的 #include 和 main 函数",

    "system_solve_easy": "你是算法讲师，擅长简洁清晰地讲解题目。\n\n## 讲解风格\n- 简单题抓核心：一句话点出关键思路\n- 说明数据范围决定了哪些做法可行、哪些不可行\n- 简要提及易错边界\n- 代码简洁清晰，包含必要的 #include 和 main 函数\n- 数学公式用 $...$ 包裹",

    "system_solve_hard": "你是顶尖算法竞赛选手兼讲师，擅长深入浅出地讲解难题。\n\n## 讲解风格\n- 先给出直观理解（问题本质是什么），再进入严谨分析\n- 从暴力到优化：展示思维升级过程\n- 关键推导用具体例子辅助说明\n- 复杂度分析要说清「为什么是这个复杂度」而非只给出结果\n- 数学公式用 $...$ 或 $$...$$ 包裹\n\n## 工作流程\n1. 深入分析问题的数学本质，寻找模型转化\n2. 列举所有可行方案，比较优劣，讲解选择理由\n3. 严格证明时间/空间复杂度\n4. 逐一列举边界和极端情况（至少 5 种）并给出处理方式\n5. 用样例手动模拟验证\n6. 写出完整代码\n\n## 代码铁律\n- 使用 long long 处理所有可能超过 2e9 的整数\n- 数组大小 = 最大数据量 + 5 的余量\n- 多组测试用例时，所有全局/静态变量必须重置\n- 循环内避免 endl，用 '\\n' 防止 TLE\n- 大输入（>10^5）使用 ios::sync_with_stdio(false); cin.tie(nullptr)\n- 递归深度 >10^5 时改写为迭代或开栈\n- 浮点数比较用 fabs(a-b) < 1e-9\n- 取模运算每一步都取模，避免溢出\n- 考虑使用离散化、坐标压缩、离线处理等技巧\n\n## 输出要求\n- 代码前必须有「解题思路」段（讲解式，含直观理解 + 算法对比 + 推导证明）\n- 代码块用 ```{ext} 包裹，包含所有必要的 #include 和 main 函数",
    "system_fix": "你是算法讲师，根据评测反馈修正解法并给出更好的讲解。\n\n## 修正流程\n1. 对照评测反馈，精准定位失败原因（不是猜测）\n2. 编译错误 → 检查语法/头文件；运行时错误 → 检查越界/溢出/空指针；WA → 检查逻辑/边界/特殊 case；TLE → 分析复杂度瓶颈\n3. 修正后用题目样例在脑中模拟验证\n4. 在题解中讲解「之前的错误是什么、为什么错、正确做法是什么」\n\n## 铁律\n- 不猜测，根据评测证据推导\n- 如果原算法正确但实现有 bug → 修正 bug，讲解 bug 成因\n- 如果算法本身有缺陷 → 换用正确算法，讲解为什么新算法更优\n- 输出必须有「解题思路」（讲解式）+「代码」",
    "system_obfuscate": "你是一个代码混淆专家。仅输出混淆后的代码，不添加任何解释、注释或额外文本。",
    "fix_with_history": "## 评测反馈\n得分: {score}\n{error_hint}\n{ce_section}### 测试详情\n{errors}\n\n## 修正任务\n1. 根据错误信息精确定位失败原因\n2. 如果原算法正确但实现有 bug → 修正代码，讲解 bug 成因\n3. 如果原算法有根本缺陷 → 换用正确算法，讲解为什么新算法更好\n\n## 输出格式\n## 解题思路\n（讲解正确解法：从问题分析到算法选择的完整思维过程，说清来龙去脉）\n\n## 代码\n```{ext}\n（完整修正后的代码）\n```",
    "obfuscate": "请对以下代码进行强力混淆，使其难以阅读和理解，但功能完全不变。\n\n要求：\n- 重命名所有变量、函数、类名为无意义的短名称（如 a1, b2, c3 或 _0x 前缀）\n- 将连续语句合并为逗号表达式\n- 删除所有注释和多余空白\n- 展开简单函数为内联代码（宏或直接嵌入）\n- 将常量替换为晦涩的等价表达式\n- 保持代码能通过相同的编译和评测\n\n原始代码：\n```{ext}\n{code}\n```\n\n仅输出混淆后的代码，不要任何解释：\n```{ext}\n（混淆后的代码）\n```",
    "generate": "## 题目\n【{title}】{info}\n\n{content}\n\n## 求解要求\n\n### 讲解要点\n- 面向学习者：从读题到解题的完整思维过程\n- 分析数据范围 → 确定可用复杂度 → 选择算法（说清为什么）\n- 用题目样例演示算法过程\n- 列举至少 3 种边界/特殊情况的处理方式\n- 时间/空间复杂度带推导\n\n### 代码要求\n- 时限 {time_limit}，内存 {memory_limit}，{io_hint}\n- 使用 long long、显式处理边界、多组测试重置状态\n- 代码完整可编译运行，包含所有 #include 和 main 函数\n\n## 输出格式\n## 解题思路\n（问题本质 → 思维过程 → 算法选择理由 → 关键推导 → 边界处理 → 复杂度）\n\n## 代码\n```{ext}\n（完整代码）\n```",

    "generate_easy": "## 题目\n【{title}】{info}\n\n{content}\n\n## 求解要求\n这是一道简单题，请简洁讲解。\n\n### 要点\n- 一句话说清核心思路\n- 时限 {time_limit}，内存 {memory_limit}，{io_hint}\n- 注意数据范围选择正确类型（int 不够用 long long）\n- 检查边界：n=0、n=1、最大/最小值\n- 代码简洁清晰\n\n## 输出格式\n## 解题思路\n（核心思路 + 关键注意点，3-5 句话）\n\n## 代码\n```{ext}\n（完整可运行的代码，含 #include 和 main）\n```",

    "generate_flash": "## 题目\n【{title}】{info}\n\n{content}\n\n## 要求\n认真分析题目，采用均衡策略：既不简陋也不过度复杂。\n\n### 分析要点\n- 仔细审题，理解输入输出格式和约束条件\n- 分析数据范围，确定合适的算法复杂度级别\n- 时限 {time_limit}，内存 {memory_limit}，{io_hint}\n- 列举至少 3 种边界/特殊情况\n\n### 代码要求\n- 选择正确且高效的算法，兼顾简洁与鲁棒\n- 使用 long long、显式处理边界\n- 多组测试时重置全部状态变量\n- 代码完整可编译运行\n\n## 输出格式\n## 解题思路\n（问题分析 → 算法选择 → 边界处理 → 复杂度）\n\n## 代码\n```{ext}\n（完整代码，含 #include 和 main）\n```",

    "generate_hard": "## 题目\n【{title}】{info}\n\n{content}\n\n## 求解要求\n这是一道困难题，请深入讲解。\n\n### 第一步：理解问题\n- 分析问题的数学本质，寻找模型转化\n- 数据范围的算法含义：n≤10→O(n!), n≤20→O(2ⁿ), n≤500→O(n³), n≤10⁵→O(n log n), n≤10⁶→O(n)\n- 时限 {time_limit}，内存 {memory_limit}，{io_hint}\n\n### 第二步：从暴力到优化（讲解核心）\n- 先给出朴素解法，分析其不足\n- 逐步优化，每一步说清「为什么这样优化」「省掉了什么」\n- 提出所有可行方案，比较优劣，选择最优并给出理由\n- 涉及数学时给出公式推导，配文字解释\n\n### 第三步：严谨分析\n- 完整的时间/空间复杂度推导和证明\n- 列举至少 5 种边界/极端情况并说明处理方式\n\n### 第四步：实现要点\n- 最优数据结构选择理由\n- 常数优化技巧（避免拷贝、引用传递、快速 IO）\n- 递归改迭代、多组测试重置等注意事项\n\n## 输出格式\n## 解题思路\n（直观理解 → 朴素思路 → 逐步优化 → 最终方案 → 复杂度证明 → 边界清单 → 实现要点）\n\n## 代码\n```{ext}\n（完整代码，关键处加注释说明）\n```",
}


# ═══════════════════════════════════════════════════════════════
# AI 调用限流（延迟模式）
# ═══════════════════════════════════════════════════════════════
_ai_last_call = 0.0
_ai_lock = threading.Lock()

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
        self._show_thinking = config.get("show_thinking", False)

    def _get_client(self, model_name: str = ""):
        """获取或创建 OpenAI 客户端，按 (base_url, api_key) 缓存"""
        name = model_name or self.config["ai_model"]
        base_url = self.config.get_model_base_url(name)
        api_key = self.config.get_model_api_key(name)
        cache_key = (base_url, api_key)
        if cache_key not in self._clients:
            self._clients[cache_key] = OpenAI(api_key=api_key, base_url=base_url,
                                              timeout=180.0, max_retries=2) if api_key else None
        return self._clients[cache_key]

    @staticmethod
    def _load_prompts() -> dict:
        try:
            with open("prompts.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    _cached_prompts: dict | None = None

    @staticmethod
    def reload_class_prompts():
        """热重载提示词（供 WebUI / CLI 调用）"""
        AIClient._cached_prompts = None  # 下次访问时重新加载
        import logging as _log
        _log.getLogger(__name__).info("[+] 提示词已标记重载，下次调用生效")

    def _p(self, key: str, default: str = "") -> str:
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

    def generate(self, problem: dict, use_stream: bool = False,
                 difficulty: int = 0) -> dict | None:
        """
        difficulty: 0=未判断/默认, 1=简单(flash), 2=中等(pro), 3=困难(max)
        根据难度选择不同的 user 提示词和 system 提示词。
        """
        log.info("[*] 调用 AI 生成题解和代码 (题面 %d 字符, 难度%d) ...",
                 len(problem.get('content', '')), difficulty)
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
        return self._call_ai(prompt, sys_msg, use_stream)

    def fix(self, problem: dict, code: str, solution_md: str,
            verdict: dict, retry_num: int = 1, use_stream: bool = False,
            history: list | None = None, difficulty: int = 0) -> dict | None:
        """修正代码。无上下文时直接重新 generate 而非修正。"""
        log.info("[*] 第%d次修正：反馈错误给 AI ...", retry_num)
        ext = self.config.lang_ext

        # 无上下文 → 直接重新生成，不修正
        if not history:
            log.info("[*] 无上下文，重新调用 generate ...")
            return self.generate(problem, use_stream=use_stream, difficulty=difficulty)

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
        return self._call_ai_with_messages(
            history + [{"role": "user", "content": fix_prompt}], use_stream)

    def obfuscate(self, code: str) -> dict | None:
        """混淆代码：仅将代码发给 AI 要求强力混淆"""
        log.info("[*] 调用 AI 混淆代码 ...")
        ext = self.config.lang_ext
        tpl = self._p("obfuscate", DEFAULT_PROMPTS.get("obfuscate", ""))
        prompt = tpl.format(ext=ext, code=code)
        sys_msg = self._p("system_obfuscate", DEFAULT_PROMPTS.get("system_obfuscate", ""))
        result = self._call_ai(prompt, sys_msg, use_stream=False)
        if result:
            result["code"] = result.get("code", "") or code
        return result

    def _build_args(self, messages: list) -> dict:
        """构建 API 请求参数（provider 自适应，每模型独立配置）"""
        model = self.config["ai_model"]
        base_url = self.config.get_model_base_url(model)
        max_tok = self.config.get_model_max_tokens(model)
        args = dict(model=model, messages=messages, max_tokens=max_tok)
        re_val = self.config.get_model_reasoning_effort(model)
        if re_val and "deepseek" in base_url:
            # 校验：只取逗号分隔的第一个值，确保是有效值
            valid_efforts = {"low", "medium", "high", "max", "xhigh"}
            first = re_val.split(",")[0].strip()
            if first in valid_efforts:
                args["reasoning_effort"] = first
        if "deepseek" in base_url or "bigmodel" in base_url:
            args["extra_body"] = {"thinking": {"type": "enabled"}}
        return args

    def _calc_cost(self, usage: dict) -> float:
        """根据模型定价计算费用（元），支持多峰值峰谷价"""
        model = self.config["ai_model"]
        pricing = self.config.get_model_pricing(model)
        if not pricing: return 0.0
        now = datetime.now().hour
        inp_price = pricing.get("input", 0)
        out_price = pricing.get("output", 0)
        cache_price = pricing.get("cache_hit", 0)
        peaks = pricing.get("peaks", [])
        if peaks:
            for peak in peaks:
                hours = peak.get("hours", [])
                if len(hours) >= 2 and hours[0] <= now < hours[1]:
                    inp_price = peak.get("input", inp_price)
                    out_price = peak.get("output", out_price)
                    if "cache_hit" in peak:
                        cache_price = peak["cache_hit"]
                    break
        else:
            ph = pricing.get("peak_hours", [])
            if ph and len(ph) >= 2 and ph[0] <= now < ph[1]:
                inp_price = pricing.get("peak_input", inp_price)
                out_price = pricing.get("peak_output", out_price)
                if "peak_cache_hit" in pricing:
                    cache_price = pricing["peak_cache_hit"]
        cost = (usage.get("input", 0) * inp_price +
                usage.get("output", 0) * out_price +
                usage.get("cache_hit", 0) * cache_price) / 1_000_000
        return round(cost, 6)

    def _parse_response(self, content: str, reasoning: str, usage_obj,
                        t_start: float) -> dict:
        """统一解析 AI 响应：提取代码、Token、耗时、费用"""
        if not content:
            log.error("[-] AI 返回内容为空"); return None
        if reasoning and self._show_thinking:
            log.info("    --- 思考过程 (%d 字符) ---", len(reasoning))
            log.info(reasoning[:3000])
        ext = self.config.lang_ext
        code = ""
        blocks = list(re.finditer(rf"```(?:{ext}|c\+\+|c)\s*\n(.+?)```", content, re.DOTALL))
        if blocks:
            code = blocks[-1].group(1).strip()
        usage = {}
        if usage_obj:
            # 兼容不同 SDK / API 的 usage 字段名
            def _get(u, *names):
                for n in names:
                    # 支持点号分隔的递归属性访问 (如 prompt_tokens_details.cached_tokens)
                    obj = u
                    for part in n.split("."):
                        obj = getattr(obj, part, None)
                        if obj is None:
                            break
                    if obj is not None and obj > 0:
                        return obj
                return 0
            usage = {
                "input": _get(usage_obj, "prompt_tokens", "input_tokens"),
                "output": _get(usage_obj, "completion_tokens", "output_tokens"),
                "total": _get(usage_obj, "total_tokens"),
                "cache_hit": _get(usage_obj, "cache_creation_input_tokens",
                                  "prompt_tokens_details.cached_tokens"),
            }
        cost = self._calc_cost(usage)
        elapsed = time.monotonic() - t_start
        log.info("[+] 耗时%.1fs | Token %di/%do | 费用¥%.4f | 内容%d字符 代码%d字符",
                 elapsed, usage.get("input", 0), usage.get("output", 0),
                 cost, len(content), len(code))
        return {"solution_md": content, "code": code, "raw": content,
                "usage": usage, "elapsed_s": elapsed, "cost": cost}

    # ---- 连续对话调用 ----
    def _call_ai_with_messages(self, messages: list, use_stream: bool = False) -> dict | None:
        if self._get_client() is None: log.error("[-] API Key 未配置"); return None
        try:
            t_start = time.monotonic()
            content, reasoning, usage_obj = (self._stream_call(self._build_args(messages))
                                             if use_stream else self._block_call(self._build_args(messages)))
            return self._parse_response(content, reasoning, usage_obj, t_start)
        except requests.RequestException as e:
            log.error("[-] AI 网络异常: %s", e); return None
        except Exception as e:
            msg = str(e)
            if "timed out" in msg.lower():
                log.error("[-] AI 请求超时 — %s", self.config.get_model_base_url(self.config["ai_model"]))
            else: log.error("[-] AI 调用异常: %s", e)
            return None

    # ---- 核心调用 ----
    def _call_ai(self, user_prompt: str, system_msg: str,
                 use_stream: bool = False) -> dict | None:
        if self._get_client() is None: log.error("[-] API Key 未配置"); return None
        # 延迟模式：同 AI 服务请求至少间隔 2s
        if os.environ.get("OJ_DELAY_MODE") == "1":
            _ai_delay()
        try:
            log.info("[*] 提交AI (%d字符)", len(user_prompt))
            t_start = time.monotonic()
            args = self._build_args([{"role": "system", "content": system_msg},
                                     {"role": "user", "content": user_prompt}])
            content, reasoning, usage_obj = (self._stream_call(args) if use_stream
                                             else self._block_call(args))
            return self._parse_response(content, reasoning, usage_obj, t_start)
        except requests.RequestException as e:
            log.error("[-] AI 网络异常: %s", e); return None
        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "rate" in msg:
                log.warning("[!] API 限流(429)，建议切换模型")
                return {"_rate_limited": True}
            log.error("[-] AI 调用异常: %s", e); return None

    def _stream_call(self, args: dict) -> tuple:
        content = reasoning = ""
        client = self._get_client(args.get("model", ""))
        stream = client.chat.completions.create(**args, stream=True)
        self._safe_write("    "); sys.stdout.flush()
        acc = reasoning_acc = ""
        _last = None
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta: continue
            if hasattr(chunk, "usage") and chunk.usage:
                _last = chunk.usage
            r = getattr(delta, "reasoning_content", "") or ""
            c = delta.content or ""
            if r:
                reasoning += r
                if self._show_thinking:
                    reasoning_acc += r
                    reasoning_acc = self._flush_lines("[思考] ", reasoning_acc)
            if c:
                content += c
                if self._show_thinking and reasoning_acc:
                    self._flush_remainder(reasoning_acc); reasoning_acc = ""
                acc += c
                acc = self._flush_lines("", acc)
        if self._show_thinking:
            self._flush_remainder(reasoning_acc)
        self._flush_remainder(acc)
        # 多途径获取 usage（兼容不同 SDK 版本）
        usage_obj = _last
        if usage_obj is None:
            try:
                if hasattr(stream, "response") and stream.response:
                    usage_obj = getattr(stream.response, "usage", None)
            except Exception: pass
        if usage_obj is None:
            try:
                usage_obj = stream.last_response.usage if hasattr(stream, "last_response") else None
            except Exception: pass
        return content, reasoning, usage_obj

    def _block_call(self, args: dict) -> tuple:
        client = self._get_client(args.get("model", ""))
        resp = client.chat.completions.create(**args)
        content = resp.choices[0].message.content or ""
        reasoning = getattr(resp.choices[0].message, "reasoning_content", "") or ""
        return content, reasoning, resp.usage

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


# ═══════════════════════════════════════════════════════════════
# OJClient — OJ API 交互（含 HTTP 重试）
# ═══════════════════════════════════════════════════════════════
class OJClient:
    CASE_STATUS = {0: "PENDING", 1: "AC", 2: "WA", 3: "TLE", 4: "MLE",
                   5: "RE", 6: "CE", 7: "SE", 8: "OLE", 9: "CANCELED"}

    def __init__(self, config: Config):
        self.root = config["oj_root"].rstrip("/")
        self.api_base = config["oj_base"].rstrip("/")
        self.config = config
        self.session = create_session(verify_ssl=False)
        self.logged_in = False
        jar_path = config.get("cookie_jar", "")
        if jar_path and load_cookies(self.session, jar_path):
            self.logged_in = True
        self.verify_timeout = config["verify_timeout"]

    # ---- 登录 ----
    def login(self, max_retries: int = 3) -> bool:
        if self.logged_in:
            log.info("[*] 已登录（cookie 复用），跳过登录"); return True
        log.info("[*] 登录 %s ...", self.root)
        if not self.config["username"] or not self.config["password"]:
            log.error("[-] 未配置 OJ 凭据（检查 OJ_USERNAME/OJ_PASSWORD 环境变量或 config.json）")
            return False
        if oj_login(self.session, self.root, self.config["username"],
                     self.config["password"], max_retries):
            jar_path = self.config.get("cookie_jar", "")
            if jar_path: save_cookies(self.session, jar_path)
            log.info("[+] 登录成功"); self.logged_in = True; return True
        log.error("[-] 登录失败"); return False

    # ---- 获取题目 ----
    def get_problem(self, pid: str) -> dict | None:
        log.info("[*] 获取题目 #%s ...", pid)
        try:
            resp = self.session.get(f"{self.api_base}/p/{pid}",
                                    headers={"Accept": "application/json"}, timeout=15)
        except requests.RequestException as e:
            log.error("[-] 获取题目网络异常: %s", e); return None
        if resp.status_code != 200:
            log.error("[-] 获取失败，状态码 %d", resp.status_code); return None
        data = resp.json(); pdoc = data.get("pdoc", {})
        content_raw = pdoc.get("content", ""); zh = ""
        if isinstance(content_raw, str):
            if content_raw.startswith("{"):
                try: zh = json.loads(content_raw).get("zh", content_raw)
                except json.JSONDecodeError: zh = content_raw
            elif content_raw.startswith("<"):
                zh = re.sub(r'<[^>]+>', ' ', content_raw)
                zh = re.sub(r'&[a-z]+;', ' ', zh)
                zh = re.sub(r'\s+', ' ', zh).strip()
            else:
                zh = content_raw
        elif isinstance(content_raw, dict): zh = content_raw.get("zh", str(content_raw))
        title = pdoc.get("title", f"P{pid}")
        # 从 JSON pdoc 提取 tag/config 信息（优先），回退到 HTML
        config = pdoc.get("config", {}) or {}
        time_limit = f"{config.get('timeMax', 0)}ms" if config.get("timeMax") else ""
        memory_limit = f"{config.get('memoryMax', 0)}MiB" if config.get("memoryMax") else ""
        tags = []
        if "tag" in pdoc and pdoc["tag"]:
            tags = pdoc["tag"] if isinstance(pdoc["tag"], list) else [pdoc["tag"]]
        # HTML 补充（仅在 JSON 数据缺失时回退）
        html_tags = self._fetch_tags(pid) if not time_limit or not memory_limit or not tags else {}
        log.info("[+] 题目: #%s %s, tags=%s", pid, title, tags or html_tags.get("tags", []))
        return {"pid": pid, "title": title, "content": zh,
                "tags": tags or html_tags.get("tags", []),
                "time_limit": time_limit or html_tags.get("time_limit", ""),
                "memory_limit": memory_limit or html_tags.get("memory_limit", ""),
                "io_method": html_tags.get("io_method", ""),
                "url": f"{self.api_base}/p/{pid}"}

    def _fetch_tags(self, pid: str) -> dict:
        """从网页 HTML 提取 problem__tags 中的有用信息。
        返回 {tags, time_limit, memory_limit, io_method}"""
        result = {"tags": [], "time_limit": "", "memory_limit": "", "io_method": ""}
        try:
            resp = self.session.get(f"{self.api_base}/p/{pid}", timeout=10)
            html = resp.text
            # 提取所有 tag-item
            items = re.findall(r'problem__tag-item[^>]*>([^<]+)', html)
            for item in items:
                item = item.strip()
                if not item: continue
                if item.startswith("ID:"): continue
                if re.match(r'^\d+ms$', item):
                    result["time_limit"] = item
                elif re.match(r'^\d+MiB$', item) or re.match(r'^\d+MB$', item):
                    result["memory_limit"] = item
                elif item.startswith("文件IO") or item.startswith("文件 IO"):
                    result["io_method"] = item
                else:
                    result["tags"].append(item)
        except Exception:
            pass
        return result

    # ---- 提交代码 ----
    def submit_code(self, pid: str, code: str, contest_id: str = "") -> str | None:
        log.info("[*] 提交代码到 #%s ...", pid)
        data = {"lang": self.config["lang"], "code": code}
        if contest_id:
            data["tid"] = contest_id
            log.info("    (关联比赛 %s)", contest_id[:12])
        try:
            resp = self.session.post(f"{self.api_base}/p/{pid}/submit",
                data=data, allow_redirects=False, timeout=15)
        except requests.RequestException as e:
            log.error("[-] 提交异常: %s", e); return None
        if resp.status_code in (302, 303):
            m = re.search(r"/record/([a-f0-9]+)", resp.headers.get("Location", ""))
            if m:
                log.info("[+] 提交成功，记录: %s/record/%s", self.root, m.group(1))
                return m.group(1)
        if resp.status_code == 429:
            log.warning("[-] 提交限流(429)，%ds 后重试", 3)
            time.sleep(3)
            try:
                resp = self.session.post(f"{self.api_base}/p/{pid}/submit",
                    data=data, allow_redirects=False, timeout=15)
                if resp.status_code in (302, 303):
                    m = re.search(r"/record/([a-f0-9]+)", resp.headers.get("Location", ""))
                    if m: return m.group(1)
            except requests.RequestException: pass
        log.error("[-] 提交失败，status=%d", resp.status_code); return None

    # ---- 验证评测 ----
    def verify_submission(self, rid: str) -> dict | None:
        log.info("[*] 等待评测完成 (rid=%s) ...", rid)
        url = f"{self.api_base}/record/{rid}"
        waited, delay = 0, 2
        rdoc = None
        while waited < self.verify_timeout:
            # 1) 网络请求
            try:
                resp = self.session.get(url, headers={"Accept": "application/json"}, timeout=10)
            except requests.RequestException as e:
                log.warning("    网络异常: %s，%ds 后重试", str(e)[:40], delay)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256)
                continue

            # 2) HTTP 状态码
            if resp.status_code == 429:
                time.sleep(3); waited += 3; continue
            if resp.status_code in (404, 500, 502, 503, 504):
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue
            if resp.status_code != 200:
                log.error("    未知状态码 %d", resp.status_code)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 3) 解析响应
            data = resp.json()
            rdoc = data.get("rdoc", {})
            st = rdoc.get("status", -1)
            cases = rdoc.get("testCases", [])

            # 4) 状态判断
            if st == -1:
                log.warning("    rdoc 中无 status 字段")
                time.sleep(delay); waited += delay; continue

            if not cases:
                # 无测试点数据 → 评测未完成或异常，继续等待
                if st in (0, 1):
                    log.info("    %s ... (已等 %ds)",
                             "排队中" if st == 0 else "评测中", waited)
                else:
                    log.info("    status=%d 无测试点，继续等待 ... (已等 %ds)", st, waited)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 有测试点数据 → 检查是否为终态
            # 终态判断：score>0 或 有时间/内存数据 或 有编译错误
            score = rdoc.get("score", 0)
            has_timing = rdoc.get("time", 0) > 0 or rdoc.get("memory", 0) > 0
            has_ce = bool(rdoc.get("compilerTexts", []))
            is_terminal = score > 0 or has_timing or has_ce

            if not is_terminal:
                log.info("    评测中，等待最终结果 ... (已等 %ds, score=%d)", waited, score)
                time.sleep(delay); waited += delay
                delay = min(delay * 2, 256); continue

            # 终态
            break

        if waited >= self.verify_timeout or rdoc is None:
            log.error("[-] 评测超时（%ds 未完成）", self.verify_timeout); return None

        # 5) 校验数据完整性
        cases = rdoc.get("testCases", [])
        if not cases:
            log.warning("    无测试点数据！rdoc keys=%s score=%s status=%s",
                        list(rdoc.keys())[:10], rdoc.get("score"), rdoc.get("status"))

        return self._parse_verdict(rdoc)

    def _parse_verdict(self, rdoc: dict) -> dict:
        score, time_ms, memory_kb = rdoc.get("score", 0), rdoc.get("time", 0), rdoc.get("memory", 0)
        cases = rdoc.get("testCases", [])
        compiler_text = rdoc.get("compilerTexts", [])
        judge_text = rdoc.get("judgeTexts", [])

        # 构建 subtask 索引
        subtasks = rdoc.get("subtasks", [])
        subtask_map = {}
        if isinstance(subtasks, list):
            for st in subtasks:
                if isinstance(st, dict):
                    subtask_map[st.get("id", st.get("_id"))] = st

        ac_count, failures_by_type, fail_details = 0, {}, []
        subtask_stats = {}
        for c in cases:
            s, cid = c.get("status", 0), c.get("id", "?")
            t, mem = c.get("time", 0), c.get("memory", 0)
            label = self.CASE_STATUS.get(s, f"ERR({s})")
            st_id = c.get("subtaskId")
            if st_id is not None:
                st = subtask_stats.setdefault(st_id, {"ac": 0, "fail": 0, "score": 0, "label": ""})
                st["label"] = subtask_map.get(st_id, {}).get("title", f"子任务{st_id}")
                if s == 1: st["ac"] += 1
                else: st["fail"] += 1
                st["score"] += c.get("score", 0)
            if s == 1: ac_count += 1
            else:
                failures_by_type.setdefault(label, []).append(cid)
                msg = c.get("message", "")
                t_safe = float(t or 0); mem_safe = float(mem or 0)
                fail_details.append(f"  #{cid}: {label} | 耗时 {t_safe:.0f}ms | 内存 {mem_safe:.0f}KB"
                                    + (f" — {msg}" if msg else ""))

        case_summary = " ".join(self.CASE_STATUS.get(c.get("status", 0), "?") for c in cases)
        ac_rate = f"{ac_count}/{len(cases)}" if cases else "0/0"
        log.info("[+] 评测完成 — 得分: %d, AC: %s", score, ac_rate)
        log.info("    总耗时: %.0fms, 内存: %.0fKB", time_ms, memory_kb)
        log.info("    各测试点: %s", case_summary)
        for d in fail_details: log.info(d)

        errors_lines = []
        if compiler_text:
            ct = compiler_text if isinstance(compiler_text, str) else (compiler_text[0] if compiler_text else "")
            errors_lines.append(f"编译错误: {str(ct)[:600]}")
            log.info("    编译信息: %s", str(ct)[:300])
        if fail_details:
            errors_lines.append(f"通过率: {ac_rate} ({ac_count}AC / {len(cases)}总)")
            # 子任务汇总
            if len(subtask_stats) > 1:
                st_lines = []
                for st_id in sorted(subtask_stats):
                    st = subtask_stats[st_id]
                    st_lines.append(f"  {st['label']}: AC {st['ac']}/{(st['ac']+st['fail'])}, 得分 {st['score']}")
                if st_lines:
                    errors_lines.append("子任务汇总:"); errors_lines.extend(st_lines)
            for label, case_ids in failures_by_type.items():
                errors_lines.append(f"{label}: 共{len(case_ids)}个 — 用例 {', '.join(str(x) for x in case_ids)}")
            errors_lines.append("详细:"); errors_lines.extend(fail_details)
            hints = []
            if "TLE" in failures_by_type: hints.append("时间超限 — 需优化算法复杂度或剪枝")
            if "WA" in failures_by_type: hints.append("答案错误 — 检查边界条件、特殊情况和输出格式")
            if "RE" in failures_by_type: hints.append("运行错误 — 检查数组越界、空指针、除零等")
            if "MLE" in failures_by_type: hints.append("内存超限 — 需优化内存使用")
            if "CE" in failures_by_type: hints.append("编译错误 — 检查语法和头文件")
            if hints: errors_lines.append("分析提示: " + "; ".join(hints))
        if judge_text:
            jt = judge_text
            if isinstance(jt, list):
                jt = "; ".join(j if isinstance(j, str) else j.get("text", str(j)) for j in jt)
            errors_lines.append(f"评测机信息: {str(jt)[:500]}")

        all_ac = cases and all(c.get("status", 0) == 1 for c in cases)
        # 检测评测机故障：
        # - SE(7)/CANCELED(9): 时间内存全0 → 故障
        # - TLE(3): time=0 → 故障（超时应有时长）
        # - MLE(4): memory=0 → 故障（超内存应有内存占用）
        is_sys_err = False
        if cases and not all_ac:
            failed = [c for c in cases if c.get("status", 0) not in (0, 1)]
            if failed and all(c.get("status", 0) in {7, 9} and c.get("time", 0) == 0 and c.get("memory", 0) == 0 for c in failed):
                is_sys_err = True
            elif any(c.get("status", 0) == 3 and c.get("time", 0) == 0 for c in failed):
                is_sys_err = True  # TLE 但无耗时
            elif any(c.get("status", 0) == 4 and c.get("memory", 0) == 0 for c in failed):
                is_sys_err = True  # MLE 但无内存占用
        if is_sys_err:
            log.warning("    ⚠️ 疑似评测机故障：失败测试点数据异常")
            errors_lines.insert(0, "[系统疑似故障] 测试点数据异常(SE/CANCELED/TLE无耗时/MLE无内存)，可能为评测机错误")

        return {
            "score": score, "time_ms": time_ms, "memory_kb": memory_kb,
            "cases": cases, "case_summary": case_summary, "ac_rate": ac_rate,
            "failures_by_type": failures_by_type, "fail_details": fail_details,
            "errors_text": "\n".join(errors_lines),
            "is_ac": all_ac,
            "is_system_error": is_sys_err,
            "compiler_text": compiler_text, "judge_text": judge_text,
        }

    # ---- 发布题解 ----
    def post_solution(self, pid: str, solution_md: str) -> str | None:
        log.info("[*] 发布题解到 P%s 题解区 ...", pid)
        try:
            resp = self.session.post(f"{self.api_base}/p/{pid}/solution",
                json={"operation": "submit", "content": solution_md},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15)
        except requests.RequestException as e:
            log.error("[-] 发布题解网络异常: %s", e); return None
        if resp.status_code == 200:
            data = resp.json(); psid = data.get("psid", "")
            if psid:
                log.info("[+] 题解发布成功，psid=%s", psid)
                log.info("    题解页面: %s/p/%s/solution", self.api_base, pid)
                return psid
        log.error("[-] 题解发布失败，status=%d, body=%s", resp.status_code, resp.text[:200]); return None


# ═══════════════════════════════════════════════════════════════
# SolverOrchestrator — 编排完整流程
# ═══════════════════════════════════════════════════════════════
class SolverOrchestrator:
    def __init__(self, oj: OJClient, ai: AIClient, config: Config):
        self.oj = oj; self.ai = ai; self.config = config

    def solve(self, pid: str, *, submit: bool = True, post: bool = True,
              max_retries: int = 3, use_stream: bool = False, contest_id: str = "",
              outer_retries: int = 3):
        """一键解题。
        Phase1: flash 快速尝试(不修正) → Phase2: 难度判断
        → Phase3: 三层循环 (外层升级模型 × 中层换思路2次 × 内层修正2次)
        """
        log.info("\n" + "=" * 50 + f"\n  OJ Auto Solver — #{pid}\n" + "=" * 50)

        if not self.oj.login(): return
        problem = self.oj.get_problem(pid)
        if not problem: return

        total_usage, total_elapsed, total_cost = {}, 0.0, 0.0
        all_rids, all_verdicts = [], []
        final_verdict = None
        code, solution_md = "", ""
        is_ac = False
        difficulty = 0  # 0=未判断, 1-8 (8级制)
        MID_RETRIES = 2   # 中层默认：重试解题次数（flash 层仅 1 次）
        INNER_RETRIES = 2  # 内层：按错误修正次数

        # 多模型路由
        from model_router import ModelRouter
        router = ModelRouter(config_manager=self.config)
        route_model = router.default.model
        route_thinking = ""

        def _apply_route():
            self.config.set_override(ai_model=route_model)
            if route_thinking:
                self.config.set_override(ai_reasoning_effort=route_thinking)

        # ═══ Phase 0: 免费模型快速尝试（不修正） ═══
        free_model = "glm-4.6v-flash"
        fallback_model = "deepseek-v4-flash"  # free 限流时切换到 flash
        has_free = free_model in (self.config.cfg.models if self.config.cfg.models else {})
        if has_free:
            route_model = free_model
            route_thinking = ""
            _apply_route()
            log.info("[路由] Phase0 free: %s", route_model)

            # flash 专属提示词：快速、直击核心
            tpl = self.ai._p("generate_flash", "") or DEFAULT_PROMPTS.get("generate_flash", "")
            if not tpl:
                tpl = self.ai._p("generate_easy", "") or DEFAULT_PROMPTS.get("generate_easy", "")
            ext = self.ai.config.lang_ext
            prompt = tpl.format(title=problem['title'],
                info=f"时限 {problem.get('time_limit','?')} | 内存 {problem.get('memory_limit','?')}",
                content=problem['content'], time_limit=problem.get('time_limit','?'),
                memory_limit=problem.get('memory_limit','?'),
                io_hint=problem.get('io_method') and f"IO方式: {problem['io_method']}" or '使用标准输入输出',
                ext=ext)
            result = self.ai._call_ai(prompt, self.ai.SYS_SOLVE_EASY, use_stream=use_stream)
            # free 限流 → 自动切换 flash
            if result and result.get("_rate_limited"):
                log.info("[*] free 模型限流，切换 %s 重试", fallback_model)
                self.config.set_override(ai_model=fallback_model, ai_reasoning_effort="high")
                result = self.ai._call_ai(prompt, self.ai.SYS_SOLVE_EASY, use_stream=use_stream)
            if result and result.get("code"):
                code = result["code"]; solution_md = result["solution_md"]
                fu = result.get("usage", {})
                for k in ("input", "output", "total", "cache_hit"):
                    total_usage[k] = total_usage.get(k, 0) + fu.get(k, 0)
                total_elapsed += result.get("elapsed_s", 0)
                total_cost += result.get("cost", 0)
                banner = self._code_banner(usage=fu, cost=result.get("cost", 0))
                if submit:
                    rid = self.oj.submit_code(pid, banner + code)
                    all_rids.append(rid)
                    if rid:
                        verdict = self.oj.verify_submission(rid)
                        all_verdicts.append(verdict)
                        final_verdict = verdict
                        if verdict and verdict.get("is_ac"):
                            is_ac = True
                            log.info("[+] Phase0 AC! 用时 %.0fms, 内存 %.0fKB",
                                     verdict["time_ms"], verdict["memory_kb"])

        # ═══ Phase 1: 难度判断 ═══
        if not is_ac:
            if has_free:
                self.config.set_override(ai_model=free_model, ai_reasoning_effort="")
            log.info("[路由] Phase1 难度判断 (模型=%s) ...",
                     free_model if has_free else self.config["ai_model"])
            diff_prompt = router.DIFFICULTY_PROMPT.format(content=problem.get("content","")[:3000])
            diff_result = self.ai._call_ai(diff_prompt, "你是一个题目难度评估专家。仅回复数字。", use_stream=False)
            if diff_result and diff_result.get("_rate_limited") and has_free:
                log.info("[*] free 模型限流，切换 %s 判断难度", fallback_model)
                self.config.set_override(ai_model=fallback_model, ai_reasoning_effort="high")
                diff_result = self.ai._call_ai(diff_prompt, "你是一个题目难度评估专家。仅回复数字。", use_stream=False)
            if diff_result:
                try:
                    difficulty = int(diff_result.get("raw","").strip()[:1])
                    difficulty = max(1, min(8, difficulty))
                except ValueError:
                    difficulty = 2
            log.info("[路由] 难度判定: %d 级", difficulty)

        # ═══ Phase 2: 三层循环 ═══
        # 外层：升级模型 | 中层：重新解题(换思路) | 内层：按错误修正
        # 起始层级根据难度：difficulty 1→flash, 2→pro, 3→max
        # 8级难度 → tier: 1-2→0(flash), 3-5→1(pro), 6-8→2(max)
        if difficulty <= 2:    tier_start = 0
        elif difficulty <= 5:  tier_start = 1
        else:                  tier_start = 2
        tier_start = min(tier_start, len(router.tiers) - 1)
        for tier_idx in range(tier_start, len(router.tiers)):
            if is_ac: break
            tier = router.tiers[tier_idx]
            route_model = tier.model
            # flash 层中层仅 1 次，其他层 2 次
            mid_retries = 1 if tier_idx == 0 else MID_RETRIES

            for mid in range(mid_retries):
                if is_ac: break
                route_thinking = tier.current_thinking(mid)
                _apply_route()
                log.info("[三层] 模型=%s 中层=%d/%d 难度=%d",
                         route_model, mid + 1, mid_retries, difficulty)

                result = self.ai.generate(problem, use_stream=use_stream, difficulty=difficulty)
                if result and result.get("_rate_limited"):
                    # 降级模型: max→pro, pro→flash
                    prev_tier = tier_idx - 1
                    if prev_tier >= 0:
                        fallback = router.tiers[prev_tier]
                        log.warning("[!] %s 限流，降级到 %s 重试", tier.name, fallback.model)
                        route_model = fallback.model
                        route_thinking = fallback.current_thinking(mid)
                        _apply_route()
                        banner = self._code_banner()
                        result = self.ai.generate(problem, use_stream=use_stream, difficulty=difficulty)
                    else:
                        log.warning("[!] 已经是底层模型，无备选")
                if not result or not result.get("code"):
                    log.warning("[-] AI 未生成代码"); break
                code = result["code"]; solution_md = result["solution_md"]
                total_usage = dict(result.get("usage", {}))
                total_elapsed = result.get("elapsed_s", 0)
                total_cost = result.get("cost", 0)
                banner = self._code_banner(usage=total_usage, cost=total_cost)

                history = [
                    {"role": "system", "content": self.ai.SYS_SOLVE},
                    {"role": "user", "content": f"请解决这道题：\n{problem['content']}"},
                    {"role": "assistant", "content": solution_md},
                ]

                for fix_i in range(INNER_RETRIES + 1):
                    if not submit or not code: break
                    rid = self.oj.submit_code(pid, banner + code)
                    all_rids.append(rid)
                    if not rid: break
                    verdict = self.oj.verify_submission(rid)
                    all_verdicts.append(verdict)
                    if not verdict: break
                    final_verdict = verdict
                    if verdict.get("is_ac"):
                        is_ac = True
                        log.info("[+] AC! 用时 %.0fms, 内存 %.0fKB",
                                 verdict["time_ms"], verdict["memory_kb"]); break
                    if fix_i >= INNER_RETRIES:
                        log.warning("[-] 内层修正 %d 次未 AC，得分 %d",
                                    INNER_RETRIES, verdict["score"]); break
                    if verdict.get("is_system_error"):
                        log.warning("[!] 疑似评测机故障，直接重试提交")
                        time.sleep(3); continue
                    log.info("\n[*] 得分 %d，第 %d 次修正 (中层%d/%d 内层%d/%d) ...",
                             verdict["score"], fix_i + 1,
                             mid + 1, MID_RETRIES, fix_i + 1, INNER_RETRIES)
                    fix = self.ai.fix(problem, code, solution_md, verdict, fix_i + 1,
                                      use_stream=use_stream, history=history, difficulty=difficulty)
                    if not fix: log.warning("[-] 修正失败"); break
                    code = fix["code"]; solution_md = fix["solution_md"]
                    history.append({"role": "user", "content": f"评测: 得分{verdict['score']}, {verdict.get('case_summary','')}"})
                    history.append({"role": "assistant", "content": solution_md})
                    fu = fix.get("usage", {})
                    for k in ("input", "output", "total", "cache_hit"):
                        total_usage[k] = total_usage.get(k, 0) + fu.get(k, 0)
                    total_elapsed += fix.get("elapsed_s", 0)
                    total_cost += fix.get("cost", 0)

        retry_count = max(0, len(all_verdicts) - 1); outer_count = retry_count; psid = None

        if not is_ac:
            log.warning("[-] 未 AC，跳过发布题解（仅满分才发布）"); post = False

        # 代码混淆（仅 AC 后、发布题解前）
        obf_code = None
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                obf_config = json.load(f).get("code_obfuscate", {})
        except Exception:
            obf_config = {}
        if is_ac and code and obf_config.get("enabled"):
            # 切换到免费模型进行混淆
            saved_model = self.config["ai_model"]
            saved_effort = self.config["ai_reasoning_effort"]
            self.config.set_override(ai_model=free_model, ai_reasoning_effort="")
            log.info("[*] 调用 AI 混淆代码 (免费模型: %s) ...", free_model)
            obf = self.ai.obfuscate(code)
            if obf and obf.get("_rate_limited") and has_free:
                log.info("[*] free 模型限流，切换 %s 混淆", fallback_model)
                self.config.set_override(ai_model=fallback_model, ai_reasoning_effort="high")
                obf = self.ai.obfuscate(code)
            self.config.set_override(ai_model=saved_model, ai_reasoning_effort=saved_effort)
            if obf and obf.get("code"):
                obf_code = obf["code"]
                log.info("[+] 混淆完成，%d 字符 → %d 字符", len(code), len(obf_code))
                # 替换题解中的代码块为混淆版本，解题内容不变
                ext = self.config.lang_ext
                solution_md = re.sub(
                    rf"```(?:{ext}|c\+\+|c)\s*\n.+?```",
                    f"```{ext}\n{obf_code}\n```",
                    solution_md, count=1, flags=re.DOTALL)
            else:
                log.warning("[-] 混淆失败，使用原始代码")

        if post and solution_md:
            # 插入难度标签
            if difficulty > 0:
                from model_router import ModelRouter
                tag = ModelRouter.difficulty_tag(difficulty)
                solution_md = tag + "\n\n" + solution_md
            footer = self._build_footer(total_elapsed, total_usage, retry_count, total_cost)
            psid = self.oj.post_solution(pid, solution_md + footer)

        # 混淆后重新提交到原题/比赛
        if obf_code:
            if obf_config.get("resubmit_problem"):
                log.info("[*] 提交混淆代码到原题 ...")
                rid2 = self.oj.submit_code(pid, banner + obf_code)
                if rid2: log.info("[+] 混淆提交成功: %s/record/%s", self.oj.root, rid2)
            if obf_config.get("resubmit_contest") and contest_id:
                log.info("[*] 提交混淆代码到比赛 ...")
                self.oj.submit_code(pid, banner + obf_code, contest_id=contest_id)

        # 比赛同步递交（未混淆但 AC 时）
        if contest_id and is_ac and code and not obf_code:
            log.info("[*] 向比赛 %s 同步递交 ...", contest_id[:12])
            crid = self.oj.submit_code(pid, banner + code, contest_id=contest_id)
            if crid:
                log.info("[+] 比赛递交成功: %s/record/%s", self.oj.root, crid)

        log.info("\n" + "=" * 50 + f"\n  完成!\n  题目: {problem['title']}\n  链接: {problem['url']}")
        if all_rids: log.info("  评测: %s/record/%s", self.oj.root, all_rids[-1])
        if final_verdict:
            ac = "✓ AC" if is_ac else f"得分 {final_verdict['score']}"
            log.info("  结果: %s | 耗时: %.0fms | 内存: %.0fKB", ac, final_verdict["time_ms"], final_verdict["memory_kb"])
        if psid: log.info("  题解: %s/p/%s/solution", self.oj.api_base, pid)
        log.info("=" * 50)

        # 写入 dashboard 记录
        self._record_result(problem, is_ac, final_verdict, total_usage, total_elapsed,
                            total_cost, retry_count)

        return {"final_verdict": final_verdict, "psid": psid,
                "retry_count": retry_count, "outer_count": retry_count,
                "total_usage": total_usage, "total_cost": total_cost,
                "total_elapsed": total_elapsed, "is_ac": is_ac, "pid": pid,
                "model": self.config["ai_model"]}

    def _record_result(self, problem: dict, is_ac: bool, verdict: dict | None,
                       usage: dict, elapsed: float, cost: float, retries: int):
        """追加解题记录到 dashboard.json（使用共享工具函数）"""
        from oj_common import append_dashboard_record
        record = {
            "pid": str(problem.get("pid", "?")),
            "title": problem.get("title", "")[:30],
            "status": "ac" if is_ac else "fail",
            "score": verdict.get("score", 0) if verdict else 0,
            "time_ms": verdict.get("time_ms", 0) if verdict else 0,
            "memory_kb": verdict.get("memory_kb", 0) if verdict else 0,
            "tokens_in": usage.get("input", 0),
            "tokens_out": usage.get("output", 0),
            "cache_hit": usage.get("cache_hit", 0),
            "cost": cost,
            "retries": retries,
            "elapsed_s": elapsed,
            "finished_at": datetime.now().strftime("%m-%d %H:%M:%S"),
        }
        append_dashboard_record(record)

    def _comment_prefix(self) -> str:
        """根据语言返回注释前缀"""
        ext = self.config.lang_ext
        return "#" if ext == "python" else "//"

    def _code_banner(self, usage: dict | None = None, cost: float = 0) -> str:
        """生成代码头部注释，使用当前活跃模型的配置"""
        c = self._comment_prefix()
        model = self.config["ai_model"]
        re_val = self.config.get_model_reasoning_effort(model)
        banner = (
            f"{c} 模型: {model}  "
            f"| 语言: {self.config['lang']}  "
            f"| 推理强度: {re_val}\n"
            f"{c} 由 OJ Auto Solver 自动生成\n"
        )
        if usage:
            banner += f"{c} Token: {usage['input']}i/{usage['output']}o/{usage['total']}t  "
            if usage.get("cache_hit"):
                banner += f"| 缓存命中: {usage['cache_hit']} "
            if cost > 0:
                banner += f"| 费用: ¥{cost:.4f}"
            banner += "\n"
        return banner + "\n"

    def _build_footer(self, elapsed: float, usage: dict, retry_count: int, cost: float = 0) -> str:
        model = self.config["ai_model"]
        re_val = self.config.get_model_reasoning_effort(model)
        lines = [
            f"\n\n---\n",
            f"> 本题解由 **{model}** 生成 "
            f"| 代码语言: **{self.config['lang']}**",
            f"> 推理强度: **{re_val}** "
            f"| 总耗时: {elapsed:.1f}s",
        ]
        if usage:
            lines.append(f"> Token 用量: {usage['input']} in / {usage['output']} out / {usage['total']} total"
                        + (f" (缓存命中 {usage['cache_hit']})" if usage.get("cache_hit") else ""))
        if cost > 0:
            lines.append(f"> 预估费用: ¥{cost:.4f}")
        if retry_count > 0:
            lines.append(f"> 修正次数: {retry_count}")
        return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════
def main():
    load_dotenv()
    from oj_common import setup_logging
    parser = argparse.ArgumentParser(description="OJ Auto Solver — 自动解题并发布 Markdown 题解")
    parser.add_argument("pid", help="题目 ID 或完整链接")
    parser.add_argument("--base", help="OJ API 地址")
    parser.add_argument("--username", help="用户名")
    parser.add_argument("--password", help="密码")
    parser.add_argument("--api-key-env", help="AI API Key")
    parser.add_argument("--base-url", help="AI API 地址")
    parser.add_argument("--model", help="AI 模型名")
    parser.add_argument("--lang", help="提交语言 ID")
    parser.add_argument("--timeout", type=int, help="评测超时（秒）")
    parser.add_argument("--ai-only", action="store_true", help="仅 AI 生成解答")
    parser.add_argument("--no-submit", action="store_true", help="不提交代码")
    parser.add_argument("--no-post", action="store_true", help="不发布题解")
    parser.add_argument("--dry-run", action="store_true", help="仅抓取题目内容")
    parser.add_argument("--stream", action="store_true", help="AI 流式输出")
    parser.add_argument("--show-thinking", action="store_true", help="显示 AI 思考过程（默认关闭）")
    parser.add_argument("--no-show-thinking", action="store_true", help=argparse.SUPPRESS)  # 兼容子进程调用
    parser.add_argument("--quiet", action="store_true", help="减少输出")
    parser.add_argument("--verbose", action="store_true", help="详细输出")
    parser.add_argument("--log-file", help="日志文件路径（支持 {date} 占位符，如 logs/oj_{date}.log）")
    parser.add_argument("--cookie-jar", help="Cookie jar 文件路径")
    parser.add_argument("--code", help="提交已有代码文件")
    parser.add_argument("--solution", help="发布已有题解文件")
    args = parser.parse_args()

    # 日志系统
    setup_logging(quiet=args.quiet, verbose=args.verbose,
                  log_file=args.log_file, name="oj_solver")

    parsed_root, parsed_api_base, parsed_pid = parse_problem_url(args.pid)
    args.pid = parsed_pid
    if parsed_root:
        log.info("[*] 目标题目: %s/p/%s", parsed_api_base, parsed_pid)

    cli = {}
    for k in ["username", "password", "lang"]:
        if getattr(args, k): cli[k] = getattr(args, k)
    for k in ["api_key_env", "base_url", "model"]:
        v = getattr(args, k.replace("-", "_"))
        if v: cli[f"ai_{k.replace('-', '_')}"] = v
    if args.base: cli["oj_base"] = args.base
    if parsed_root: cli["oj_root"] = parsed_root; cli["oj_base"] = parsed_api_base
    if args.cookie_jar: cli["cookie_jar"] = args.cookie_jar
    if args.timeout: cli["verify_timeout"] = args.timeout
    cli["show_thinking"] = args.show_thinking

    config = Config(cli_overrides=cli)
    config.repair()  # 自动补齐缺失配置
    oj = OJClient(config)
    ai = AIClient(config)
    solver = SolverOrchestrator(oj, ai, config)

    if args.dry_run:
        if not oj.login(): return
        prob = oj.get_problem(args.pid)
        if prob:
            log.info("\n" + "=" * 50 + f"\n题目: {prob['title']}\n" + "=" * 50 + f"\n{prob['content']}")
        return

    if args.ai_only:
        if not oj.login(): return
        prob = oj.get_problem(args.pid)
        if prob:
            result = ai.generate(prob, use_stream=args.stream)
            if result:
                log.info("\n" + "=" * 50 + "\n" + result["solution_md"] + "\n--- code ---\n" + result["code"])
        return

    if args.code or args.solution:
        if not oj.login(): return
        if args.code:
            try: code = Path(args.code).read_text(encoding="utf-8"); oj.submit_code(args.pid, code)
            except (FileNotFoundError, UnicodeDecodeError, OSError) as e: log.error("[-] 读取失败: %s", e)
        if args.solution:
            try: md = Path(args.solution).read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError, OSError) as e: md = None; log.error("[-] 读取失败: %s", e)
            if md:
                footer = solver._build_footer(0, {}, 0)
                oj.post_solution(args.pid, md + footer)
        return

    solver.solve(args.pid, submit=not args.no_submit, post=not args.no_post,
                  use_stream=args.stream)


if __name__ == "__main__":
    main()
