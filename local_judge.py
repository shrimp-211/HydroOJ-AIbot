#!/usr/bin/env python3
"""本地评测工具 — 让求解流程在提交前就能编译、跑样例、对拍。

这是「agent 化」的关键一环：模型写完代码后先在本地验证，把编译错误、
样例不过、随机对拍被 hack 这三类问题挡在提交之前，而不是靠 OJ 评测
（慢、消耗提交次数、容易触发限流）来发现。

安全提醒：这里执行的是 AI 生成的代码。已做的基本约束——
  * 每次运行都有超时（默认取题目时限的若干倍）
  * POSIX 下限制地址空间 / CPU 时间 / 输出文件大小，并在超时后杀进程组
  * 输出长度截断
但它不是真正的沙箱：请只在可丢弃的环境里运行，不要给该进程过高权限。
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_OUT_LIMIT = 64 * 1024        # 单次运行输出上限（字节）
DEFAULT_TIMEOUT = 10.0               # 单次运行超时（秒）
DEFAULT_MEM_LIMIT_MB = 1024          # POSIX 地址空间上限


# ══════════════════════════════════════════════════════════════
# 结果对象
# ══════════════════════════════════════════════════════════════
@dataclass
class CompileResult:
    ok: bool
    exe: str = ""
    message: str = ""


@dataclass
class RunResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    time_s: float = 0.0
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class CaseResult:
    n: int
    status: str            # AC | WA | RE | TLE
    stdin: str = ""
    expected: str = ""
    actual: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "AC"


@dataclass
class StressResult:
    rounds: int = 0
    counterexample: dict | None = None   # {"stdin":..., "expected":..., "actual":...}
    detail: str = ""
    applicable: bool = False             # 对拍是否真正跑起来（暴力解/生成器可用）

    @property
    def ok(self) -> bool:
        # 只有真正跑过对拍且没找到反例，才算通过；没跑起来不算通过也不阻塞提交
        return self.applicable and self.counterexample is None


@dataclass
class CheckReport:
    """一次代码的完整本地检查结果，用于回喂给模型。"""
    compile_ok: bool = False
    compile_message: str = ""
    cases: list[CaseResult] = field(default_factory=list)
    stress: StressResult | None = None

    @property
    def sample_failures(self) -> list[CaseResult]:
        return [c for c in self.cases if not c.ok]

    @property
    def ok(self) -> bool:
        return (self.compile_ok and not self.sample_failures
                and (self.stress is None or self.stress.ok))

    def to_prompt(self, max_chars: int = 2000) -> str:
        """把检查结果压成给模型的反馈文本。"""
        lines = []
        if not self.compile_ok:
            lines.append("【本地编译失败】")
            if self.compile_message:
                lines.append(self.compile_message[:1200])
        for c in self.sample_failures[:3]:
            lines.append(f"【样例 {c.n} {c.status}】{c.detail}")
            lines.append(f"输入:\n{c.stdin[:400]}\n期望输出:\n{c.expected[:400]}\n实际输出:\n{c.actual[:400]}")
        if self.stress is not None and not self.stress.ok:
            ce = self.stress.counterexample or {}
            lines.append(f"【随机对拍失败】{self.stress.detail}")
            lines.append(f"反例输入:\n{ce.get('stdin','')[:400]}\n"
                         f"暴力解输出:\n{ce.get('expected','')[:400]}\n"
                         f"你的输出:\n{ce.get('actual','')[:400]}")
        text = "\n".join(lines) if lines else "本地检查通过"
        return text[:max_chars]


# ══════════════════════════════════════════════════════════════
# 工具链探测
# ══════════════════════════════════════════════════════════════
@dataclass
class Toolchain:
    name: str
    source_ext: str
    compile_cmd: list[str]      # 占位 {src} {exe}
    run_cmd: list[str]          # 占位 {exe}，空表示用解释器直接跑源码

    def build_compile(self, src: str, exe: str) -> list[str]:
        return [a.replace("{src}", src).replace("{exe}", exe) for a in self.compile_cmd]

    def build_run(self, exe: str) -> list[str]:
        return [a.replace("{exe}", exe) for a in self.run_cmd]


def detect_toolchain(lang_ext: str) -> Toolchain | None:
    """按语言探测本地工具链；不可用时返回 None（调用方退回原流程）。"""
    ext = (lang_ext or "cpp").lower()
    if ext == "cpp":
        for cxx in ("g++", "clang++", "c++"):
            path = shutil.which(cxx)
            if path:
                return Toolchain("cpp", ".cpp",
                                 [path, "-O2", "-std=c++14", "-pipe", "-o", "{exe}", "{src}"],
                                 ["{exe}"])
        return None
    if ext == "python":
        exe = shutil.which("python3") or shutil.which("python") or sys.executable
        return Toolchain("python", ".py", [], [exe, "{exe}"])
    if ext in ("c",):
        path = shutil.which("gcc") or shutil.which("clang")
        if path:
            return Toolchain("c", ".c", [path, "-O2", "-pipe", "-o", "{exe}", "{src}"], ["{exe}"])
    return None


# ══════════════════════════════════════════════════════════════
# 输出比对
# ══════════════════════════════════════════════════════════════
def normalize(text: str) -> str:
    """逐行去尾空白 + 去掉末尾空行（多数 OJ 的比对方式）。"""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def outputs_match(expected: str, actual: str) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。宽松：行尾空白与末尾换行不影响判定。"""
    exp, act = normalize(expected), normalize(actual)
    if exp == act:
        return True, ""
    if exp.split() == act.split():
        return True, "token 相同但空白格式不同（OJ 通常判 AC，建议对齐格式）"
    return False, "输出不一致"


# ══════════════════════════════════════════════════════════════
# 本地评测器
# ══════════════════════════════════════════════════════════════
class LocalJudge:
    """编译 / 运行 / 跑样例 / 对拍。所有方法在工具缺失时安全降级。"""

    def __init__(self, lang_ext: str = "cpp", workdir: str | None = None,
                 run_timeout: float = DEFAULT_TIMEOUT, out_limit: int = DEFAULT_OUT_LIMIT,
                 mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB):
        self.lang_ext = (lang_ext or "cpp").lower()
        self.toolchain = detect_toolchain(self.lang_ext)
        self.workdir = workdir or tempfile.mkdtemp(prefix="ojjudge_")
        Path(self.workdir).mkdir(parents=True, exist_ok=True)
        self.run_timeout = float(run_timeout)
        self.out_limit = int(out_limit)
        self.mem_limit_mb = int(mem_limit_mb)
        self._seq = 0
        # 编译器与运行程序都需要可写临时目录：沙箱/容器里系统 TEMP 常常不可写，
        # 统一指向自己的工作目录，避免 "Cannot create temporary file"。
        self.env = dict(os.environ)
        self.env.update({"TMPDIR": self.workdir, "TEMP": self.workdir, "TMP": self.workdir})

    @property
    def available(self) -> bool:
        return self.toolchain is not None

    def describe(self) -> str:
        if not self.available:
            return f"无本地工具链（{self.lang_ext}），已跳过本地验证"
        return f"{self.toolchain.name} @ {self.toolchain.compile_cmd[0] if self.toolchain.compile_cmd else self.toolchain.run_cmd[0]}"

    # ---- 编译 ----
    def compile(self, code: str, tag: str = "main") -> CompileResult:
        if not self.available:
            return CompileResult(False, message="本地无可用编译器")
        tc = self.toolchain
        self._seq += 1
        src = str(Path(self.workdir) / f"{tag}_{self._seq}{tc.source_ext}")
        exe = str(Path(self.workdir) / f"{tag}_{self._seq}")
        if tc.name == "python":
            exe += ".py"
        try:
            Path(src).write_text(code, encoding="utf-8")
        except OSError as e:
            return CompileResult(False, message=f"写入源码失败: {e}")
        if not tc.compile_cmd:            # 解释型语言
            return CompileResult(True, exe=src)
        try:
            proc = subprocess.run(tc.build_compile(src, exe), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=60, env=self.env, **_spawn_kwargs())
        except subprocess.TimeoutExpired:
            return CompileResult(False, message="编译超时（60s）")
        except OSError as e:
            return CompileResult(False, message=f"无法执行编译器: {e}")
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return CompileResult(False, message=_truncate(msg, 1500))
        return CompileResult(True, exe=exe)

    # ---- 运行 ----
    def run(self, exe: str, stdin: str = "", timeout: float | None = None) -> RunResult:
        tc = self.toolchain
        if tc is None:
            return RunResult(stderr="本地无可用工具链", exit_code=127)
        timeout = float(timeout or self.run_timeout)
        stdout = stderr = ""
        truncated = False
        out_path = str(Path(self.workdir) / f"out_{time.monotonic_ns()}.txt")
        t0 = time.monotonic()
        try:
            if os.name == "posix" and tc.name != "python":
                # 输出重定向到文件 + RLIMIT_FSIZE：跑飞时进程直接被信号杀掉，
                # 不会把内存吃满（AI 代码里 for(;;) cout<<... 并不罕见）。
                with open(out_path, "wb") as fh:
                    proc = subprocess.Popen(tc.build_run(exe), stdin=subprocess.PIPE,
                                            stdout=fh, stderr=subprocess.STDOUT,
                                            env=self.env,
                                            **_spawn_kwargs(limit_fsize=self.out_limit))
                    try:
                        proc.communicate((stdin or "").encode("utf-8"), timeout=timeout)
                    except subprocess.TimeoutExpired:
                        _kill(proc)
                        return RunResult(timed_out=True, time_s=time.monotonic() - t0,
                                         stderr="本地运行超时")
                    data = Path(out_path).read_bytes()[:self.out_limit]
                truncated = Path(out_path).stat().st_size > self.out_limit
                stdout = data.decode("utf-8", errors="replace")
            else:
                proc = subprocess.Popen(tc.build_run(exe), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        env=self.env, **_spawn_kwargs())
                try:
                    out, _ = proc.communicate((stdin or "").encode("utf-8"), timeout=timeout)
                except subprocess.TimeoutExpired:
                    _kill(proc)
                    return RunResult(timed_out=True, time_s=time.monotonic() - t0,
                                     stderr="本地运行超时")
                data = out or b""
                truncated = len(data) > self.out_limit
                stdout = data[:self.out_limit].decode("utf-8", errors="replace")
        except OSError as e:
            return RunResult(stderr=f"无法运行: {e}", exit_code=127)
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        return RunResult(stdout=stdout, stderr=stderr, exit_code=proc.returncode,
                         time_s=time.monotonic() - t0, truncated=truncated)

    # ---- 跑样例 ----
    def check_samples(self, code: str, samples: list[dict],
                      timeout: float | None = None) -> CheckReport:
        report = CheckReport()
        comp = self.compile(code, tag="sol")
        report.compile_message = comp.message
        if not comp.ok:
            report.compile_ok = False
            return report
        report.compile_ok = True
        for i, s in enumerate(samples, 1):
            r = self.run(comp.exe, s.get("in", ""), timeout=timeout)
            if r.timed_out:
                status, detail = "TLE", f"运行超时（>{timeout or self.run_timeout}s）"
            elif r.exit_code != 0:
                status, detail = "RE", f"退出码 {r.exit_code}"
            else:
                same, why = outputs_match(s.get("out", ""), r.stdout)
                status, detail = ("AC", why) if same else ("WA", why)
            report.cases.append(CaseResult(
                n=i, status=status, stdin=s.get("in", ""), expected=s.get("out", ""),
                actual=r.stdout, detail=detail))
            if status != "AC" and status != "WA":
                break
        return report

    # ---- 对拍（自测 / self-hack） ----
    def stress(self, code: str, reference: str, generator: str, rounds: int = 30,
               timeout: float | None = None) -> StressResult:
        """把解与暴力解在随机数据上对拍，返回第一个反例。"""
        if not self.available:
            return StressResult(0, None, "本地无可用工具链")
        timeout = float(timeout or min(self.run_timeout, 5.0))
        sol = self.compile(code, tag="sol")
        if not sol.ok:
            return StressResult(0, None, f"解编译失败: {sol.message[:200]}")
        ref = self.compile(reference, tag="ref")
        if not ref.ok:
            return StressResult(0, None, f"暴力解编译失败: {ref.message[:200]}")
        gen = self.compile(generator, tag="gen")
        if not gen.ok:
            return StressResult(0, None, f"数据生成器编译失败: {gen.message[:200]}")
        for rnd in range(1, int(rounds) + 1):
            g = self.run(gen.exe, str(rnd), timeout=timeout)
            if not g.ok or not g.stdout.strip():
                return StressResult(rnd - 1, None, f"数据生成器第 {rnd} 轮无有效输出")
            data = g.stdout
            a = self.run(sol.exe, data, timeout=timeout)
            b = self.run(ref.exe, data, timeout=timeout)
            if b.timed_out or b.exit_code != 0:
                continue  # 暴力解自己超时/崩溃，跳过这组数据
            if a.timed_out:
                return StressResult(rnd, {"stdin": data, "expected": b.stdout,
                                          "actual": "（超时）"}, "解在随机数据上超时",
                                    applicable=True)
            if a.exit_code != 0:
                return StressResult(rnd, {"stdin": data, "expected": b.stdout,
                                          "actual": a.stdout + a.stderr}, "解在随机数据上崩溃",
                                    applicable=True)
            same, _ = outputs_match(b.stdout, a.stdout)
            if not same:
                return StressResult(rnd, {"stdin": data, "expected": b.stdout,
                                          "actual": a.stdout}, "输出与暴力解不一致",
                                    applicable=True)
        return StressResult(int(rounds), None, "", applicable=True)


# ══════════════════════════════════════════════════════════════
# 进程辅助
# ══════════════════════════════════════════════════════════════
def _spawn_kwargs(limit_fsize: int | None = None) -> dict:
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _make_limiter(limit_fsize)
    else:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _make_limiter(fsize: int | None):
    def _limit():
        try:
            import resource
            mem = DEFAULT_MEM_LIMIT_MB * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
            if fsize:
                resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        except Exception:
            pass
    return _limit


def _kill(proc):
    try:
        if os.name == "posix":
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def parse_time_limit(time_limit: str, default: float = 1.0) -> float:
    """把题面的 "1000ms" / "2s" / "2000" 解析成秒。"""
    if not time_limit:
        return default
    m = re.search(r"(\d+(?:\.\d+)?)\s*(ms|s|秒|毫秒)?", str(time_limit))
    if not m:
        return default
    val = float(m.group(1))
    unit = (m.group(2) or "ms").lower()
    return val / 1000.0 if unit in ("ms", "毫秒") else val
