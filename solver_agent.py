#!/usr/bin/env python3
"""Agent 式求解循环 — 把「写完就交」改成「分析 → 实现 → 本地验证 → 提交 → 反思」。

与原三层循环的区别（本次能力升级的核心）：

    原流程:  生成代码 → 直接提交 OJ → 等评测 → 拿 WA/CE 反馈 → 修 → 再提交
    Agent :  出方案 → 写代码 → 本地编译 → 跑样例 → 随机对拍 → 才提交 OJ

直接收益：
  * 编译错误在本地发现，不占提交次数、不用等评测队列
  * 样例不过时拿到的是精确 diff（输入 / 期望 / 实际），远比 OJ 一句 WA 有用
  * 随机对拍（self-hack）能在提交前抓出边界 WA
  * 每次失败写入结构化日志（试过什么、错在哪），后续尝试不再重复同一条死路

循环由本模块的代码驱动（代码调用工具），不依赖模型端 function calling，
任何 OpenAI 兼容端点都能用；本地工具链不可用时自动退化为「生成一次 + 提交」。
"""

import logging
import re
import time
from dataclasses import dataclass, field

from local_judge import CheckReport, LocalJudge, parse_time_limit
from oj_samples import extract_samples

log = logging.getLogger(__name__)

_BLOCK_RE = re.compile(r"```\s*([A-Za-z_][\w+-]*)\s*\n(.*?)```", re.DOTALL)


def extract_labeled_blocks(text: str, labels: tuple[str, ...]) -> dict[str, str]:
    """按围栏标签取块，例如 ```brute / ```gen（取每类第一块）。"""
    wanted = {lab.lower() for lab in labels}
    found: dict[str, str] = {}
    for m in _BLOCK_RE.finditer(text or ""):
        label = m.group(1).lower()
        if label in wanted and label not in found:
            found[label] = m.group(2).strip()
    return found


@dataclass
class AttemptOutcome:
    """一次 agent 求解结果。字段与原三层循环对齐，便于直接复用后续发布流程。"""
    code: str = ""
    solution_md: str = ""
    is_ac: bool = False
    verdict: dict | None = None
    usage: dict = field(default_factory=dict)
    cost: float = 0.0
    elapsed: float = 0.0
    model: str = ""
    effort: str = ""
    rids: list = field(default_factory=list)
    verdicts: list = field(default_factory=list)
    steps: int = 0            # 模型调用轮次
    local_runs: int = 0       # 本地跑过的样例/对拍次数
    submissions: int = 0      # 实际提交 OJ 次数
    stress_rounds: int = 0    # 对拍轮数
    is_cost_capped: bool = False
    journal: list = field(default_factory=list)
    last_check_text: str = ""  # 最近一次本地检查的可读报告（回喂给模型）

    @property
    def local_saves(self) -> int:
        """本地验证替 OJ 挡下的次数（有产出但没提交的轮次）。"""
        return max(0, self.steps - self.submissions)


class SolverAgent:
    """带本地工具的求解循环。

    通过回调与外部解耦（submit_fn / judge_fn / banner_fn / cost_exceeded），
    既便于离线测试，也让主流程继续负责发布题解、统计与通知。

    回调约定：
        submit_fn(code) -> rid | None
        judge_fn(rid) -> verdict dict | None
        banner_fn(model, effort, usage, cost) -> str   # 提交时附加的注释头
        cost_exceeded() -> bool
    """

    def __init__(self, ai, config, judge: LocalJudge | None = None,
                 submit_fn=None, judge_fn=None, banner_fn=None, cost_exceeded=None):
        self.ai = ai
        self.config = config
        self.judge = judge
        self.submit_fn = submit_fn
        self.judge_fn = judge_fn
        self.banner_fn = banner_fn
        self.cost_exceeded = cost_exceeded or (lambda: False)
        self._reference: tuple[str, str] | None = None
        self._reference_tried = False

    # ══════════════════════════════════════════════════════════
    # 对外入口
    # ══════════════════════════════════════════════════════════
    def attempt(self, problem: dict, *, model: str, effort: str = "", difficulty: int = 0,
                candidate_tags: list | None = None, submit: bool = True,
                use_stream: bool = False, contest_id: str = "",
                max_steps: int | None = None, stress_enable: bool = True,
                stress_rounds: int = 30, plan_enable: bool = True) -> AttemptOutcome:
        t0 = time.monotonic()
        out = AttemptOutcome(model=model, effort=effort)

        if not self._judge_usable():
            log.info("[Agent] 本地工具链不可用，退化为「生成一次 + 提交」")
            return self._fallback_single_shot(problem, out, model, effort, submit,
                                              use_stream, contest_id)

        steps_limit = int(max_steps or self.config.get("agent_max_steps", 6) or 6)
        submit_limit = int(self.config.get("agent_max_submissions", 4) or 4)
        deadline = t0 + float(self.config.get("agent_max_seconds", 900) or 900)
        samples = extract_samples(problem.get("content", ""))
        time_limit = parse_time_limit(problem.get("time_limit", ""), 1.0)
        run_timeout = max(2.0, min(float(self.config.get("agent_run_timeout", 10) or 10),
                                   time_limit * 5 + 2))

        log.info("[Agent] 求解循环启动 | %s | 样例 %d 组 | 时限 %.1fs | 上限 %d 步 / %d 次提交",
                 self.judge.describe(), len(samples), time_limit, steps_limit, submit_limit)

        plan = self._plan(problem, model, effort, out) if plan_enable else ""
        code, solution_md = "", ""

        while out.steps < steps_limit and not out.is_ac:
            if time.monotonic() > deadline:
                log.warning("[Agent] 达到总时间上限，停止")
                out.journal.append("达到总时间上限")
                break
            if self.cost_exceeded():
                out.is_cost_capped = True
                out.journal.append("费用达到上限")
                break
            out.steps += 1

            # 1) 出代码（首轮按方案实现，之后带着反馈修正）
            code, solution_md = self._write_code(
                problem, plan, out, model, effort, use_stream=use_stream)
            if not code:
                out.journal.append(f"第{out.steps}轮：模型未给出代码块")
                continue
            out.code, out.solution_md = code, solution_md

            # 2) 本地验证：编译 + 样例
            check = (self.judge.check_samples(code, samples, timeout=run_timeout)
                     if samples else _compile_only(self.judge, code))
            out.local_runs += len(check.cases) or 1
            if not check.compile_ok:
                log.info("[Agent] 本地编译失败 → 不提交，回喂编译器报错")
                out.last_check_text = check.to_prompt()
                out.journal.append(
                    f"第{out.steps}轮：本地编译失败\n{check.compile_message[:500]}")
                continue
            if check.sample_failures:
                bad = check.sample_failures[0]
                log.info("[Agent] 样例 %d %s → 不提交，回喂具体差异", bad.n, bad.status)
                out.last_check_text = check.to_prompt()
                out.journal.append(f"第{out.steps}轮：样例 {bad.n} {bad.status}")
                continue
            log.info("[Agent] 本地样例通过（%d 组）", len(check.cases))

            # 3) 随机对拍（self-hack）
            if stress_enable and samples:
                stress = self._stress(problem, code, out, stress_rounds, run_timeout)
                if stress is not None:
                    out.stress_rounds = max(out.stress_rounds, stress.rounds)
                    if stress.counterexample:
                        log.info("[Agent] 对拍发现反例 → 不提交：%s", stress.detail)
                        check.stress = stress
                        out.last_check_text = check.to_prompt()
                        out.journal.append(f"第{out.steps}轮：对拍反例（{stress.detail}）")
                        continue
                    log.info("[Agent] 对拍通过 %d 轮", stress.rounds)

            # 4) 提交 OJ
            if not submit or out.submissions >= submit_limit:
                log.info("[Agent] 不再提交（submit=%s，已提交 %d 次）", submit, out.submissions)
                break
            verdict = self._submit_and_judge(code, out)
            if verdict is None:
                out.journal.append(f"第{out.steps}轮：提交或评测无结果")
                break
            if verdict.get("is_ac"):
                out.is_ac = True
                log.info("[Agent] AC！共 %d 轮，本地拦下 %d 次失败提交",
                         out.steps, out.local_saves)
                break
            if verdict.get("is_system_error"):
                log.warning("[Agent] 疑似评测机故障，原样重交")
                out.journal.append(f"第{out.steps}轮：评测机故障，代码重交")
                continue
            out.journal.append(
                f"第{out.steps}轮：评测得分 {verdict.get('score', 0)}，"
                f"{str(verdict.get('case_summary', ''))[:120]}")

        # 5) 兜底：全程被本地检查拦下（一次都没提交）时，最后再交一次，
        #    避免出现「看起来跑完但 OJ 上什么都没有」的情况
        if (not out.is_ac and not out.verdicts and submit and out.code
                and out.submissions < submit_limit):
            log.info("[Agent] 本地反复失败，兜底提交最后一版代码")
            out.verdict = self._submit_and_judge(out.code, out)
            out.is_ac = bool(out.verdict and out.verdict.get("is_ac"))

        out.elapsed = time.monotonic() - t0
        log.info("[Agent] 结束 | AC=%s | 轮次 %d | 提交 %d | 本地验证 %d 次 | 费用 ¥%.4f",
                 out.is_ac, out.steps, out.submissions, out.local_runs, out.cost)
        return out

    # ══════════════════════════════════════════════════════════
    # 内部实现
    # ══════════════════════════════════════════════════════════
    def _judge_usable(self) -> bool:
        return bool(self.judge and self.judge.available)

    def _call(self, prompt: str, model: str, effort: str, out: AttemptOutcome,
              use_stream: bool = False, max_tokens: int = 0) -> str:
        """一次模型调用，并把 token/费用记入本次尝试。"""
        r = self.ai.chat([{"role": "system", "content": self.ai._p("agent_system", "")},
                          {"role": "user", "content": prompt}],
                         model=model, effort=effort, use_stream=use_stream,
                         max_tokens=max_tokens)
        if not r:
            return ""
        for k in ("input", "output", "total", "cache_hit"):
            out.usage[k] = out.usage.get(k, 0) + (r.get("usage", {}) or {}).get(k, 0)
        out.cost += r.get("cost", 0)
        return r.get("content", "") or ""

    def _plan(self, problem: dict, model: str, effort: str, out: AttemptOutcome) -> str:
        tpl = self.ai._p("agent_plan", "")
        if not tpl:
            return ""
        prompt = tpl.format(title=problem.get("title", ""),
                            content=problem.get("content", "")[:6000],
                            time_limit=problem.get("time_limit", "?"),
                            memory_limit=problem.get("memory_limit", "?"))
        plan = self._call(prompt, model, effort, out)
        if plan:
            log.info("[Agent] 方案已生成（%d 字符）", len(plan))
        return plan

    def _write_code(self, problem: dict, plan: str, out: AttemptOutcome, model: str,
                    effort: str, *, use_stream: bool) -> tuple[str, str]:
        ext = self.config.lang_ext
        if out.steps <= 1:
            tpl = self.ai._p("agent_implement", "") or self.ai._p("generate", "")
            prompt = tpl.format(title=problem.get("title", ""),
                                content=problem.get("content", ""),
                                plan=plan or "（自行选择你认为正确的算法）",
                                time_limit=problem.get("time_limit", "?"),
                                memory_limit=problem.get("memory_limit", "?"), ext=ext)
        else:
            tpl = self.ai._p("agent_repair", "")
            prompt = tpl.format(title=problem.get("title", ""),
                                content=problem.get("content", "")[:4000],
                                plan=plan or "-",
                                code=out.code or "(尚未生成)",
                                feedback=self._feedback_text(out),
                                journal=self._journal_text(out), ext=ext)
        content = self._call(prompt, model, effort, out, use_stream=use_stream)
        if not content:
            return "", ""
        code = self._extract_solution_code(content, ext)
        if not code:
            log.warning("[Agent] 模型输出没有可识别的代码块")
            return "", content
        # 题解正文保持模型原始输出（含代码块）——与原流程发布的内容一致，
        # 避免 agent 模式下题解区里看不到代码。
        return code, content

    @staticmethod
    def _extract_solution_code(content: str, ext: str) -> str:
        # 复用主流程的围栏解析（含 ```C++ title=x、~~~ 等变体）
        from oj_solver import AIClient
        return AIClient._extract_code(content, ext)

    def _feedback_text(self, out: AttemptOutcome) -> str:
        parts = []
        if out.verdict:
            v = out.verdict
            parts.append(f"## OJ 评测结果\n得分 {v.get('score', 0)} | "
                         f"{v.get('case_summary', '')}\n{str(v.get('errors_text', ''))[:1500]}")
        if out.last_check_text:
            parts.append("## 最近一次本地检查\n" + out.last_check_text)
        if not parts:
            parts.append("（还没有反馈，请重新推导）")
        return "\n\n".join(parts)

    def _journal_text(self, out: AttemptOutcome) -> str:
        if not out.journal:
            return "（首次尝试）"
        return "\n".join(f"- {x[:300]}" for x in out.journal[-6:])

    def _reference_solutions(self, problem: dict, out: AttemptOutcome,
                             model: str, effort: str) -> tuple[str, str] | None:
        """要一份暴力解 + 随机数据生成器用于对拍（整个尝试只请求一次）。"""
        if self._reference_tried:
            return self._reference
        self._reference_tried = True
        tpl = self.ai._p("agent_reference", "")
        if not tpl:
            return None
        prompt = tpl.format(title=problem.get("title", ""),
                            content=problem.get("content", "")[:4000],
                            ext=self.config.lang_ext)
        content = self._call(prompt, model, effort, out)
        blocks = extract_labeled_blocks(content, ("brute", "gen", "generator"))
        brute = blocks.get("brute", "")
        gen = blocks.get("gen") or blocks.get("generator") or ""
        if brute and gen:
            log.info("[Agent] 已取得暴力解与数据生成器，准备对拍")
            self._reference = (brute, gen)
        else:
            log.info("[Agent] 未取得对拍素材，本次跳过错拍")
            self._reference = None
        return self._reference

    def _stress(self, problem: dict, code: str, out: AttemptOutcome,
                rounds: int, run_timeout: float):
        ref = self._reference_solutions(problem, out, out.model, out.effort)
        if not ref:
            return None
        brute, gen = ref
        result = self.judge.stress(code, brute, gen, rounds=rounds, timeout=run_timeout)
        if not result.applicable:
            log.info("[Agent] 对拍未能运行：%s", result.detail)
            return None
        return result

    def _submit_and_judge(self, code: str, out: AttemptOutcome):
        if not self.submit_fn:
            return None
        banner = (self.banner_fn(out.model, out.effort, out.usage, out.cost)
                  if self.banner_fn else "")
        rid = self.submit_fn(banner + code)
        if not rid:
            return None
        out.submissions += 1
        out.rids.append(rid)
        verdict = self.judge_fn(rid) if self.judge_fn else None
        if verdict:
            out.verdict = verdict
            out.verdicts.append(verdict)
        return verdict

    def _fallback_single_shot(self, problem, out: AttemptOutcome, model: str, effort: str,
                              submit: bool, use_stream: bool, contest_id: str) -> AttemptOutcome:
        t0 = time.monotonic()
        tpl = self.ai._p("generate", "") or "{content}"
        ext = self.config.lang_ext
        prompt = tpl.format(title=problem.get("title", ""), info="",
                            content=problem.get("content", ""),
                            time_limit=problem.get("time_limit", "?"),
                            memory_limit=problem.get("memory_limit", "?"),
                            io_hint="使用标准输入输出", ext=ext)
        content = self._call(prompt, model, effort, out, use_stream=use_stream)
        out.steps = 1
        if content:
            code = self._extract_solution_code(content, ext)
            out.code = code
            out.solution_md = content
            if code and submit:
                self._submit_and_judge(code, out)
                out.is_ac = bool(out.verdict and out.verdict.get("is_ac"))
        out.elapsed = time.monotonic() - t0
        return out


def _compile_only(judge: LocalJudge, code: str) -> CheckReport:
    """题面没有样例时，至少做一次编译检查（CE 是最常见的白交）。"""
    comp = judge.compile(code, tag="sol")
    return CheckReport(compile_ok=comp.ok, compile_message=comp.message)
