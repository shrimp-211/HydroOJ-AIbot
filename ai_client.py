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
from config_manager import ConfigManager as Config

log = logging.getLogger(__name__)

# 当 prompts.json 缺失时使用的默认提示词（与 prompts.json 内容一致）
DEFAULT_PROMPTS = {
    "system_solve": "你是算法竞赛讲师。\n\n## 铁律\n- 所有数学符号、公式必须用 $...$（行内）或 $$...$$（独立行）包裹，禁止裸符号\n- 使用 long long 处理所有可能超过 2e9 的整数\n- 数组大小 = 最大数据量 + 5 的余量\n- 多组测试用例时，所有全局/静态变量必须重置\n- 循环内避免 endl，用 '\\n' 防止 TLE\n- 大输入（>10^5）使用 ios::sync_with_stdio(false); cin.tie(nullptr)\n- 递归深度 >10^5 时改写为迭代或开栈\n- 浮点数比较用 fabs(a-b) < 1e-9\n- 取模运算每一步都取模，避免溢出\n\n## 输出\n- 「解题思路」段（讲解式）\n- ```{ext} 代码块，含 #include 和 main 函数",

    "system_solve_easy": "你是算法竞赛讲师。\n\n## 铁律\n- 所有数学符号用 $...$ 包裹，禁止裸符号\n- 使用 long long 处理大整数\n- 检查边界：n=0、n=1、最大/最小值\n- 代码简洁清晰\n\n## 输出\n- 「解题思路」段（简练）\n- ```{ext} 代码块，含 #include 和 main 函数",

    "system_solve_hard": "你是算法竞赛讲师。\n\n## 铁律\n- 所有数学符号用 $...$ 或 $$...$$ 包裹，禁止裸符号\n- 使用 long long 处理所有可能超过 2e9 的整数\n- 数组大小 = 最大数据量 + 5 的余量\n- 多组测试用例时，所有全局/静态变量必须重置\n- 循环内避免 endl，用 '\\n' 防止 TLE\n- 大输入（>10^5）使用 ios::sync_with_stdio(false); cin.tie(nullptr)\n- 递归深度 >10^5 时改写为迭代或开栈\n- 浮点数比较用 fabs(a-b) < 1e-9\n- 取模运算每一步都取模，避免溢出\n- 考虑离散化、坐标压缩、离线处理等技巧\n\n## 输出\n- 「解题思路」段（含算法对比 + 推导证明）\n- ```{ext} 代码块，含 #include 和 main 函数",
    "system_fix": "你是算法讲师，根据评测反馈修正解法并给出更好的讲解。\n\n## 修正流程\n1. 对照评测反馈，精准定位失败原因（不是猜测）\n2. 编译错误 → 检查语法/头文件；运行时错误 → 检查越界/溢出/空指针；WA → 检查逻辑/边界/特殊 case；TLE → 分析复杂度瓶颈\n3. 修正后用题目样例在脑中模拟验证\n4. 在题解中讲解「之前的错误是什么、为什么错、正确做法是什么」\n\n## 铁律\n- 不猜测，根据评测证据推导\n- 如果原算法正确但实现有 bug → 修正 bug，讲解 bug 成因\n- 如果算法本身有缺陷 → 换用正确算法，讲解为什么新算法更优\n- 输出必须有「解题思路」（讲解式）+「代码」",
    "system_obfuscate": "你是一个代码混淆专家。仅输出混淆后的代码，不添加任何解释、注释或额外文本。",
    "fix_with_history": "请修正上一份代码。\n\n## 评测反馈\n得分: {score}\n{error_hint}\n{ce_section}### 测试详情\n{errors}\n\n## 修正任务\n根据错误信息修正代码。\n\n## 输出格式\n## 解题思路\n（讲解正确解法）\n\n## 代码\n```{ext}\n（完整修正后的代码）\n```",
    "obfuscate": "请对以下代码进行强力混淆，使其难以阅读和理解，但功能完全不变。\n\n要求：\n- 重命名所有变量、函数、类名为无意义的短名称（如 a1, b2, c3 或 _0x 前缀）\n- 将连续语句合并为逗号表达式\n- 删除所有注释和多余空白\n- 展开简单函数为内联代码（宏或直接嵌入）\n- 将常量替换为晦涩的等价表达式\n- 保持代码能通过相同的编译和评测\n\n原始代码：\n```{ext}\n{code}\n```\n\n仅输出混淆后的代码，不要任何解释：\n```{ext}\n（混淆后的代码）\n```",
    "generate": "【{title}】{info}\n{content}\n\n---\n请讲解并给出代码。时限 {time_limit}，内存 {memory_limit}，{io_hint}\n\n## 解题思路\n（算法选择理由 → 关键推导 → 边界 → 复杂度）\n\n## 代码\n```{ext}\n（完整代码，含 #include 和 main）\n```",

    "generate_easy": "【{title}】{info}\n{content}\n\n---\n简单题，请简洁讲解。时限 {time_limit}，内存 {memory_limit}，{io_hint}\n\n## 解题思路\n（3-5 句话）\n\n## 代码\n```{ext}\n（完整代码）\n```",

    "generate_flash": "【{title}】{info}\n{content}\n\n---\n请给出正确解法。时限 {time_limit}，内存 {memory_limit}，{io_hint}\n\n## 解题思路\n（简短）\n\n## 代码\n```{ext}\n（完整代码）\n```",

    "generate_hard": "【{title}】{info}\n{content}\n\n---\n困难题，请深入讲解。时限 {time_limit}，内存 {memory_limit}，{io_hint}\n\n## 解题思路\n（问题转化 → 算法对比 → 推导证明 → 边界清单 → 实现要点 → 复杂度）\n\n## 代码\n```{ext}\n（完整代码）\n```",

    # ── Agent 求解循环用的提示词（分析 → 实现 → 修正 → 对拍素材）──
    "agent_system": "你是算法竞赛选手。你的产出会被自动编译、跑样例、随机对拍，只有本地验证通过的代码才会提交评测。\n\n## 铁律\n- 代码必须是完整可编译的程序（含必要的头文件与 main 函数）\n- 读入方式按题面要求：默认标准输入输出，注明文件 IO 时必须按要求读写文件\n- 使用 long long 处理可能超过 2e9 的整数；数组大小 = 最大数据量 + 5\n- 多组数据时重置全局/静态变量；大输入加 ios::sync_with_stdio(false); cin.tie(nullptr);\n- 先保证正确，再考虑优化；不确定的边界（n=0/1、极值、相同元素、退化图）必须显式处理\n- 输出格式严格按题面（多余空格、缺换行都会判错）",
    "agent_plan": """先分析题目，给出解题方案，不要写代码。

## 题目
{title}

{content}

时限 {time_limit}，内存 {memory_limit}

## 输出格式
1. **问题本质**：一句话说明这题在求什么
2. **算法**：选定算法 + 为什么它能过（与备选方案对比）
3. **关键推导/不变量**：写清正确性依据
4. **复杂度**：时间/空间，与限制比较
5. **边界清单**：需要显式处理的情况（逐条列出）""",
    "agent_implement": """按方案写出完整代码。

## 题目
{title}

{content}

时限 {time_limit}，内存 {memory_limit}

## 方案
{plan}

## 要求
- 严格按上述方案实现，逐个处理边界清单
- 只输出一个代码块（```{ext} ... ```），外加不超过 3 行的要点说明
- 写完后在脑中用样例验证一遍，确认输出格式正确""",
    "agent_repair": """上一份代码没通过验证，请修正。**不要重复已经失败的思路**。

## 题目
{title}

{content}

## 原方案
{plan}

## 当前代码
```{ext}
{code}
```

{feedback}

## 已尝试过（避免重复）
{journal}

## 修正要求
1. 先根据反馈定位**确切原因**（编译错误看报错行；样例 WA 看 diff；对拍失败看反例）
2. 如果是思路本身错了 → 换算法并说明，不要在原思路上打补丁
3. 输出完整的新代码（```{ext} ... ```），不要只给片段
4. 最后用反例/样例自检一遍""",
    "agent_reference": """为下面的题目提供**暴力解**和**随机数据生成器**，用于对拍验证。

## 题目
{title}

{content}

## 要求
- 暴力解：正确性优先，允许指数级/平方级复杂度，只需能处理很小的数据（n ≤ 10 量级）
- 数据生成器：从标准输入读入轮次编号（随机种子），向标准输出打印一组**合法且规模很小**的随机数据
  - 多个数/多行时用空格或换行分隔，格式必须与题面输入格式完全一致
  - 有时用种子初始化随机数，保证不同轮次数据不同
- 两者都只依赖标准输入输出，不读文件

## 输出格式（严格，两个代码块）
```brute
（暴力解完整代码）
```
```gen
（数据生成器完整代码）
```""",

    # ── 候选解法多样化：第二条思路必须真的换算法，而不是改改上一版 ──
    "agent_alternative_plan": """上一版思路没能通过评测，请**换一个不同的算法角度**重新分析。

## 题目
{title}

{content}

时限 {time_limit}，内存 {memory_limit}

## 已经试过的思路（不要重复）
{journal}

## 要求
1. 明确说明**为什么上一版不可行**（复杂度、边界、正确性）
2. 给出一个**根本不同**的算法：不同数据结构 / 不同转化 / 正难则反 / 不同复杂度级别
3. 写出新算法的复杂度与边界清单
4. 只输出方案，不要写代码""",

    # ── 题解产出 ──
    "editorial_system": "你是算法竞赛讲师，负责把已经通过评测的解法写成高质量中文题解。你的读者是没做过这道题的人：要先讲清题意与思路来源，再给证明与复杂度。数学符号一律用 $...$ 包裹。",
    "editorial_write": """根据一份**已经通过评测**的代码，写一篇可以直接发布的题解。

## 题目
{title}

{content}

时限 {time_limit}，内存 {memory_limit}

## 通过评测的代码（必须原样出现在题解里）
```{ext}
{code}
```

## 评测信息
得分 {score}｜用时 {time_ms}ms｜内存 {memory_kb}KB｜难度 {difficulty} 级｜标签：{tags}

## 求解过程中踩过的坑（写进「易错点」会更有说服力）
{journal}

## 写作要求
1. 用二级标题组织，顺序固定：
   `## 题意` / `## 思路` / `## 正确性说明` / `## 复杂度分析` / `## 实现要点与易错点` / `## 代码`
2. 「思路」要讲**怎么想到的**：从暴力做法出发，说明哪里不够、怎么改进到最终算法
3. 「正确性说明」给出关键不变量或归纳依据，不要写「显然」「易证」
4. 「复杂度分析」给出时间与空间的式子（用 $O(...)$），并与题目限制对照
5. 「实现要点与易错点」结合上面的踩坑记录，写清容易写错的地方（边界、溢出、初始化、输出格式）
6. 代码块必须与上面给出的代码**逐字一致**：不要改写、不要精简、不要加注释、不要换语言标记
7. 数学符号统一用 $...$，禁止裸写 \\le、\\sum、\\frac 等命令
8. 直接输出题解正文，不要出现「好的」「以下是」「希望对你有帮助」这类口气""",
    "editorial_fix": """这篇题解没有通过发布前检查，请**只针对下列问题**修改，其余内容保持原样。

## 需要修正的问题
{issues}

## 当前题解
{draft}

## 必须原样保留的代码（与 AC 代码逐字一致）
```{ext}
{code}
```

## 要求
1. 逐条解决上面的问题，不要重写无关段落
2. 仍然保持六个二级标题的结构：题意 / 思路 / 正确性说明 / 复杂度分析 / 实现要点与易错点 / 代码
3. 输出完整的修正后题解""",
}


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
        return self._call_ai_with_messages(
            history + [{"role": "user", "content": fix_prompt}], use_stream,
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
                             max_tokens=max_tokens, effort=effort)

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
            content, reasoning, usage_obj = (self._stream_call(args) if use_stream
                                             else self._block_call(args))
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
        cost = self._calc_cost(usage, model)
        elapsed = time.monotonic() - t_start
        if not content:
            log.warning("[-] AI 未返回完整正文: %s（已记录用量）", model)
        log.info("[+] %s 耗时%.1fs | Token %di/%do | 费用¥%.4f",
                 model, elapsed, usage.get("input", 0), usage.get("output", 0), cost)
        return {"content": content, "usage": usage, "cost": cost,
                "elapsed_s": elapsed, "model": model, "_incomplete": not bool(content)}

    def _calc_cost(self, usage: dict, model: str = "") -> float:
        """根据模型定价计算费用（元），支持多峰值峰谷价"""
        model = model or self.config["ai_model"]
        pricing = self.config.get_model_pricing(model)
        if not pricing: return 0.0
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
        return round(cost, 6)

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
                               effort: str = "") -> dict | None:
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
        r = self.chat(messages, model=model, use_stream=use_stream, effort=effort)
        if not r or r.get("_rate_limited"):
            return r
        code = self._extract_code(r["content"], self.config.lang_ext)
        return {"solution_md": r["content"], "code": code, "raw": r["content"],
                "usage": r["usage"], "elapsed_s": r["elapsed_s"], "cost": r["cost"],
                "model": r.get("model", model or self.config["ai_model"])}

    # ---- 核心调用 ----
    def _call_ai(self, user_prompt: str, system_msg: str,
                 use_stream: bool = False, images: list | None = None,
                 model: str = "", effort: str = "") -> dict | None:
        return self._call_ai_with_messages(
            [{"role": "system", "content": system_msg},
             {"role": "user", "content": user_prompt}],
            use_stream, images=images, model=model, effort=effort)

    def _stream_call(self, args: dict) -> tuple:
        client = self._get_client(args["model"])
        options = {"stream": True}
        if self.config.get_model_config(args["model"]).get("stream_usage", True):
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

    def _block_call(self, args: dict) -> tuple:
        resp = self._get_client(args["model"]).chat.completions.create(**args)
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


