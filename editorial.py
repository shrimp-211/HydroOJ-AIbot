#!/usr/bin/env python3
"""题解产出管线 — 求解与写作分离，发布前做结构与一致性校验。

原流程把「求解时模型的原始输出」直接当题解发布：里面混着解题过程的口语、
代码块可能和真正提交的版本不一致、公式也没人检查。现在改成：

    AC 代码 + 题目 + 评测数据 + 求解踩坑记录
        → 结构化题解（题意转化 / 算法思路 / 正确性 / 复杂度 / 易错点 / 代码）
        → 校验（代码块必须与 AC 代码逐字一致、公式配对、结构完整）
        → 有问题的定向修复（最多 N 轮）
        → 交付发布

代码块一致性是**确定性修正**（直接同步为 AC 代码），不依赖模型自觉；
模型只负责讲解部分，讲解写不好时用校验结果定向让它改。
"""

import logging
import re

log = logging.getLogger(__name__)

_BLOCK = re.compile(r"(?:```|~~~)\s*([A-Za-z_][\w+.-]*)?[^\n]*\n(.*?)(?:```|~~~)", re.DOTALL)

# 题解应包含的结构（缺哪个就补哪句，提示词里也是同样顺序）
SECTIONS = ("题意", "思路", "正确性", "复杂度", "实现", "代码")


def _norm(text: str) -> str:
    """忽略行尾空白与末尾空行后的比较用文本。"""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def find_code_block(md: str) -> tuple[int, int, str] | None:
    """返回正文中最后一个代码块的 (start, end, 内容)。"""
    last = None
    for m in _BLOCK.finditer(md or ""):
        last = m
    return (last.start(), last.end(), last.group(2).strip()) if last else None


def enforce_code_block(md: str, code: str, ext: str) -> str:
    """保证正文里的代码与真正 AC 的代码逐字一致（确定性修正）。

    - 已有代码块：内容不同则替换为 AC 代码
    - 没有代码块：在末尾补一个
    """
    block = find_code_block(md)
    fenced = f"```{ext}\n{code.strip()}\n```"
    if block is None:
        body = (md or "").rstrip()
        return f"{body}\n\n## 代码\n{fenced}\n" if body else fenced + "\n"
    start, end, content = block
    if _norm(content) == _norm(code):
        return md
    return md[:start] + fenced + md[end:]


def validate_editorial(md: str, code: str, ext: str = "cpp") -> list[str]:
    """返回题解的问题列表（空 = 通过）。此函数只做确定性检查，不调用模型。"""
    issues: list[str] = []
    text = md or ""
    if len(text.strip()) < 200:
        issues.append("题解过短，讲解不充分")

    block = find_code_block(text)
    if block is None:
        issues.append("缺少代码块")
    elif _norm(block[2]) != _norm(code):
        issues.append("代码块与通过评测的代码不一致")

    if not re.search(r"题\s*意|题目要求|题意转化|问题转化", text):
        issues.append("缺少「题意」说明")
    if not re.search(r"思\s*路|算\s*法", text):
        issues.append("缺少「算法思路」")
    if not re.search(r"复杂度|O\s*\\?\(|O\s*\(", text):
        issues.append("缺少复杂度分析")
    if not re.search(r"正确性|为什么正确|不变量|证明|归纳", text):
        issues.append("缺少正确性说明")

    # 公式配对：$ 必须成对（忽略 $$）
    if (text.replace("$$", "").count("$")) % 2:
        issues.append("$ 公式符号不配对（行内公式必须用一对 $ 包裹）")
    # 未包裹的 LaTeX 命令（常见于模型偷懒）
    bare = re.findall(r"(?<!\$)\\(?:le|ge|sum|frac|times|cdot|leq|geq)\b", text)
    if bare:
        issues.append(f"存在未被 $ 包裹的公式命令: {', '.join(sorted(set(bare))[:4])}")

    # 残留的解题过程痕迹
    for bad in ("[标签:", "以下是", "希望对你有帮助", "如果你需要"):
        if bad in text:
            issues.append(f"残留对话痕迹「{bad}」，需要删掉")
    return issues


class EditorialWriter:
    """把「已 AC 的代码」写成结构化题解，并可定向修复。"""

    def __init__(self, ai, config, call_fn=None):
        self.ai = ai
        self.config = config
        # call_fn(prompt, model, effort) -> str；不传则用 ai.chat
        self._call_fn = call_fn or self._default_call

    def _default_call(self, prompt: str, model: str, effort: str) -> str:
        r = self.ai.chat([{"role": "system", "content": self.ai._p("editorial_system", "")},
                          {"role": "user", "content": prompt}],
                         model=model, effort=effort)
        return (r or {}).get("content", "") or ""

    # ── 对外入口 ──
    def write(self, problem: dict, code: str, *, verdict: dict | None = None,
              difficulty: int = 0, tags: list | None = None,
              journal: list | None = None, model: str = "", effort: str = "",
              fallback: str = "") -> str:
        """产出可发布的题解 markdown；失败时退回 fallback（原求解输出）。"""
        if not self.config.get("editorial_enable", True):
            return fallback
        ext = self.config.lang_ext
        prompt = self._build_prompt(problem, code, verdict, difficulty, tags, journal, ext)
        draft = self._call_fn(prompt, model, effort)
        if not draft.strip():
            log.warning("[题解] 生成失败，使用求解时的原始输出")
            return fallback or code

        draft = enforce_code_block(draft, code, ext)
        issues = validate_editorial(draft, code, ext)
        rounds = int(self.config.get("editorial_fix_rounds", 2) or 0)
        for i in range(rounds):
            if not issues:
                break
            log.info("[题解] 校验未通过（%d 项），第 %d 轮定向修复：%s",
                     len(issues), i + 1, "；".join(issues[:4]))
            fixed = self._call_fn(
                self._build_fix_prompt(draft, issues, code, ext), model, effort)
            if not fixed.strip():
                break
            draft = enforce_code_block(fixed, code, ext)
            issues = validate_editorial(draft, code, ext)

        if issues:
            log.warning("[题解] 仍有 %d 项未通过（%s），已强制同步代码块后发布",
                        len(issues), "；".join(issues[:3]))
        else:
            log.info("[题解] 结构校验通过（代码块与 AC 代码一致）")
        return draft

    # ── 提示词组装 ──
    def _build_prompt(self, problem, code, verdict, difficulty, tags, journal, ext) -> str:
        tpl = self.ai._p("editorial_write", "")
        v = verdict or {}
        return tpl.format(
            title=problem.get("title", ""),
            content=problem.get("content", "")[:6000],
            time_limit=problem.get("time_limit", "?"),
            memory_limit=problem.get("memory_limit", "?"),
            code=code.strip(),
            score=v.get("score", 100),
            time_ms=v.get("time_ms", 0),
            memory_kb=v.get("memory_kb", 0),
            difficulty=difficulty or "未评定",
            tags=", ".join(tags or []) or "无",
            journal="\n".join(f"- {x[:200]}" for x in (journal or [])[-6:]) or "（无）",
            ext=ext)

    def _build_fix_prompt(self, draft: str, issues: list[str], code: str, ext: str) -> str:
        tpl = self.ai._p("editorial_fix", "")
        return tpl.format(draft=draft[:8000], issues="\n".join(f"- {i}" for i in issues),
                          code=code.strip()[:6000], ext=ext)
