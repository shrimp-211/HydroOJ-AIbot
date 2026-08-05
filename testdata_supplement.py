#!/usr/bin/env python3
"""
测试数据补充脚本 — 自动为无测试数据的题目生成并上传测试数据。

流程:
  1. 获取题目 → 检测无测试数据
  2. 提取/生成样例 I/O → 上传
  3. 生成 std.cpp → 上传
  4. 生成 data.cpp → 上传
  5. 触发评测机生成测试数据
"""

import os, re, sys, json, time, logging, argparse, tempfile, shutil
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from oj_common import (load_dotenv, create_session, smart_login,
                       parse_contest_or_problem, parse_problem_url)

log = logging.getLogger("testdata_supplement")

DEFAULT_MODEL = os.environ.get("OJ_TD_MODEL", "deepseek-v4-pro")


# ═══════════════════════════════════════════════
# TestDataSupplement
# ═══════════════════════════════════════════════
class TestDataSupplement:
    def __init__(self, url: str, model: str = None,
                 root: str = None, username: str = None, password: str = None):
        load_dotenv()
        self.root = root or os.environ.get("OJ_ROOT", "https://oj.yuanyicode.com")
        self.username = username or os.environ.get("OJ_USERNAME", "")
        self.password = password or os.environ.get("OJ_PASSWORD", "")
        self.model = model or DEFAULT_MODEL
        self.url = url
        self.session = create_session(verify_ssl=False)
        self._parsed = None
        self._ai_client = None

    # ── 解析 ──
    @property
    def parsed(self):
        if self._parsed is None:
            self._parsed = parse_contest_or_problem(self.url)
        return self._parsed

    @property
    def domain(self) -> str:
        p = self.parsed
        return p.get("domain_id") or p.get("domain", "system")

    @property
    def pid(self) -> str:
        p = self.parsed
        pids = p.get("pids", [])
        return pids[0] if pids else "?"

    @property
    def api_base(self) -> str:
        return f"{self.root}/d/{self.domain}"

    # ── AI 客户端（懒加载，避免重复创建）──
    def _get_ai_client(self):
        if self._ai_client is None:
            from oj_solver import AIClient
            from config_manager import ConfigManager
            self._ai_client = AIClient(ConfigManager())
        return self._ai_client

    # ── 登录 ──
    def login(self) -> bool:
        if smart_login(self.session, self.root, self.username, self.password):
            log.info("[+] 登录成功"); return True
        log.error("[-] 登录失败"); return False

    # ── Phase 1: 获取题目 + 检测 ═───────────────────────
    def get_problem(self) -> dict | None:
        """获取题目，检测是否有测试数据"""
        try:
            r = self.session.get(f"{self.api_base}/p/{self.pid}",
                                 headers={"Accept": "application/json"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            pdoc = data.get("pdoc", {})
            title = pdoc.get("title", f"P{self.pid}")
            content = pdoc.get("content", "")

            # 稳健的 JSON 解析：始终 try json.loads
            if isinstance(content, dict):
                content = content.get("zh", str(content))
            elif isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        content = parsed.get("zh", content)
                except (json.JSONDecodeError, TypeError):
                    pass

            config = pdoc.get("config", {}) or {}
            time_limit = config.get("timeMax", "?")
            memory_limit = config.get("memoryMax", "?")

            # 检测无测试数据：必须同时有 warn 警告框 + 文字提示
            r2 = self.session.get(f"{self.api_base}/p/{self.pid}", timeout=15)
            has_no_data = "没有测试数据" in r2.text and "blockquote" in r2.text and "warn" in r2.text

            log.info("[+] 题目: %s, 无数据=%s, 时限=%s, 内存=%s",
                     title, has_no_data, time_limit, memory_limit)
            return {
                "pid": self.pid, "title": title, "content": content,
                "time_limit": time_limit, "memory_limit": memory_limit,
                "has_no_data": has_no_data, "tags": pdoc.get("tag", []),
            }
        except Exception as e:
            log.error("[-] 获取题目失败: %s", e); return None

    # ── Phase 2: 提取样例 ──────────────────────────────
    def extract_samples(self, content: str) -> list[dict]:
        """从 markdown 提取样例输入输出。
        返回: [{"in": "...", "out": "...", "n": 1}, ...]
        """
        samples = []
        text = str(content)

        # 方式1: ```inputN / ```outputN 格式（按编号匹配）
        inputs = list(re.finditer(r'```input(\d*)\s*\n(.+?)```', text, re.DOTALL))
        outputs = list(re.finditer(r'```output(\d*)\s*\n(.+?)```', text, re.DOTALL))

        if inputs and outputs:
            in_map = {m.group(1) or "0": m.group(2).strip() for m in inputs}
            out_map = {m.group(1) or "0": m.group(2).strip() for m in outputs}
            common = set(in_map.keys()) & set(out_map.keys())
            for i, k in enumerate(sorted(common)):
                samples.append({"in": in_map[k], "out": out_map[k], "n": i + 1})
            if samples:
                log.info("[+] 提取 %d 组结构化样例", len(samples))
                return samples

        # 方式2: 连续 ```input / ```output 交错（验证配对）
        blocks = list(re.finditer(r'```(input|output)\d*\s*\n(.+?)```', text, re.DOTALL))
        i = 0
        while i < len(blocks) - 1:
            b1, b2 = blocks[i], blocks[i + 1]
            if "input" in b1.group(1) and "output" in b2.group(1):
                samples.append({
                    "in": b1.group(2).strip(),
                    "out": b2.group(2).strip(),
                    "n": len(samples) + 1,
                })
                i += 2
            else:
                i += 1
        if samples:
            log.info("[+] 提取 %d 组连续样例", len(samples))
        return samples

    # ── Phase 2b: AI 生成样例 ──────────────────────────
    def generate_samples_with_ai(self, problem: dict) -> list[dict]:
        """用 AI 根据题面生成简单测试样例"""
        client = self._get_ai_client()
        log.info("[*] AI 生成样例 ...")
        prompt = (
            f"题目：{problem['title']}\n\n{problem['content'][:3000]}\n\n"
            "请为这道题生成 3 组简单的测试样例。每组包含输入和期望输出。\n"
            "输出格式：\n"
            "```input1\n<输入1>\n```\n```output1\n<输出1>\n```\n"
            "```input2\n<输入2>\n```\n```output2\n<输出2>\n```\n"
            "```input3\n<输入3>\n```\n```output3\n<输出3>\n```\n"
            "仅输出样例，不要额外解释。"
        )
        r = client.chat(messages=[{"role": "user", "content": prompt}],
                        model=self.model, max_tokens=4096)
        content = r["content"] if r else ""

        samples = self.extract_samples(content)
        if not samples:
            in_lines, out_lines = [], []
            for m in re.finditer(r'输入[:：]?\s*\n?(.+?)(?=输出[:：]|输入[:：]|```|$)', content, re.DOTALL):
                in_lines.append(m.group(1).strip())
            for m in re.finditer(r'输出[:：]?\s*\n?(.+?)(?=输入[:：]|输出[:：]|```|$)', content, re.DOTALL):
                out_lines.append(m.group(1).strip())
            for i, (inp, outp) in enumerate(zip(in_lines, out_lines)):
                samples.append({"in": inp, "out": outp, "n": i + 1})

        log.info("[+] AI 生成 %d 组样例", len(samples))
        return samples

    # ── Phase 3: 上传文件 ──────────────────────────────
    def upload_file(self, filename: str, content: str, tmpdir: str = None) -> bool:
        """上传单个文件到测试数据。
        模拟浏览器 fetch: POST /files, multipart + 表单字段
        (filename, file, type=testdata, operation=upload_file)
        """
        _cleanup = False
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp()
            _cleanup = True
        fp = os.path.join(tmpdir, filename)
        try:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
            with open(fp, "rb") as f:
                r = self.session.post(
                    f"{self.api_base}/p/{self.pid}/files",
                    files={"file": (filename, f, "application/octet-stream")},
                    data={
                        "filename": filename,
                        "type": "testdata",
                        "operation": "upload_file",
                    },
                    headers={
                        "Accept": "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=30)
            if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/json"):
                # 验证 JSON 响应
                try:
                    resp = r.json()
                    if resp:
                        log.info("  [+] %s", filename)
                        return True
                except (json.JSONDecodeError, ValueError):
                    pass
            log.warning("  [-] 上传失败 %s: status=%d", filename, r.status_code)
        except Exception as e:
            log.warning("  [-] 上传异常 %s: %s", filename, e)
        finally:
            if _cleanup:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return False

    # ── Phase 4: 检查 std.cpp ──────────────────────────
    def list_testdata(self) -> list[dict]:
        try:
            r = self.session.get(
                f"{self.api_base}/p/{self.pid}/files",
                headers={"Accept": "application/json"}, timeout=15)
            return r.json().get("testdata", [])
        except Exception:
            return []

    def has_std_cpp(self) -> bool:
        files = self.list_testdata()
        return any(f.get("name") == "std.cpp" for f in files)

    # ── Phase 5: 生成 std.cpp ──────────────────────────
    def generate_std_cpp(self, problem: dict) -> str | None:
        """用 pro 模型生成标准程序（正确性优先）"""
        client = self._get_ai_client()
        log.info("[*] AI 生成 std.cpp (模型=%s) ...", self.model)

        tl = problem.get("time_limit", "?")
        ml = problem.get("memory_limit", "?")
        prompt = (
            f"## 题目\n【{problem['title']}】\n\n{problem['content']}\n\n"
            "## 要求\n"
            "- 这是标准程序（std.cpp），用作评测机的答案参考\n"
            "- **正确性优先**：保证算法正确、答案无误，时间和空间效率次之\n"
            f"- 数据范围：时限{tl}ms, 内存{ml}MiB\n"
            "- 使用 C++，标准输入输出，long long 处理大数\n"
            "- 代码包含必要的 #include 和 main 函数\n"
            "- 覆盖所有边界条件\n\n"
            "## 输出格式\n"
            "```cpp\n（完整代码）\n```"
        )
        r = client.chat(
            messages=[
                {"role": "system", "content": "你是算法竞赛选手。输出正确无误的标准程序。"},
                {"role": "user", "content": prompt},
            ], model=self.model, max_tokens=16384)
        code = r["content"] if r else ""

        m = re.search(r'```(?:cpp|c\+\+)\s*\n(.+?)```', code, re.DOTALL)
        if m:
            code = m.group(1).strip()
        else:
            code = re.sub(r'^.*?(#include\b)', r'\1', code, flags=re.DOTALL)

        log.info("[+] std.cpp 生成完成 (%d 字符)", len(code))
        return code if "#include" in code else None

    # ── Phase 6: 生成 data.cpp ──────────────────────────
    def generate_data_cpp(self, problem: dict) -> str | None:
        """用 AI 生成测试数据生成器。
        优先 C++ 标准库；若涉及复杂图/树/字符串可用 Python+cyaron。
        生成 1~10 号测试点，每个点数据范围递增以卡不同复杂度。
        """
        client = self._get_ai_client()
        log.info("[*] AI 生成数据生成器 ...")
        prompt = (
            f"## 题目\n【{problem['title']}】\n\n{problem['content']}\n\n"
            "## 任务\n为这道题生成一个数据生成器。\n\n"
            "### 语言选择\n"
            "- **默认使用 C++，仅用标准库**（rand/mt19937），stdin/stdout 输出\n"
            "- 若题目涉及复杂图、树、字符串等 C++ 手写太繁琐，可用 Python\n"
            "- **Python 仅必要时引用 cyaron**（Graph/Vect或/String），普通随机数用 random 库\n"
            "- 不要求代码风格/注释，**仅需保证输出数据正确合法**\n\n"
            "### 数据分段要求\n"
            "测试点 1~10，每个点数据规模递增，用于卡不同复杂度：\n"
            "  #1~2: 最小数据，n,m≤10，用于验证正确性\n"
            "  #3~4: 小数据，n,m≤100，O(n²) 可过\n"
            "  #5~6: 中等，n,m≤3000，O(n²) 卡掉，O(n log n) 可过\n"
            "  #7~8: 较大，n,m≤10⁵，O(n log n) 可过\n"
            "  #9~10: 最大，n,m≤题目最大限制\n"
            "从 argv[1] 获取当前测试点编号(1~10)。\n\n"
            "## 输出格式\n"
            "```cpp 或 ```python\n（完整代码）\n```\n\n"
            "仅输出代码，不要解释。"
        )
        r = client.chat(messages=[{"role": "user", "content": prompt}],
                        model=self.model, max_tokens=16384)
        code = r["content"] if r else ""
        for lang in ["cpp", "c++", "python", "py"]:
            m = re.search(rf'```(?:{lang})\s*\n(.+?)```', code, re.DOTALL)
            if m:
                code = m.group(1).strip()
                break
        log.info("[+] 数据生成器生成完成 (%d 字符)", len(code))
        if "def " in code or "argv" in code.lower():
            return code  # Python
        if "#include" in code and "main" in code:
            return code  # C++
        return code if len(code) > 100 else None

    # ── Phase 7: 触发生成 ──────────────────────────────
    def _notify_result(self, problem: dict, samples: int, std_code: str | None,
                       data_code: str | None, gen_ok: bool, upload_ok: bool):
        """通过私聊回复测试数据生成结果"""
        try:
            uids_str = os.environ.get("OJ_PUSH_LIST", "")
            uids = [int(x.strip()) for x in uids_str.split(",") if x.strip().isdigit()]
            requester = int(os.environ.get("OJ_REQUESTER", 0))
            if requester > 0:
                uids.append(requester)
            if not uids:
                return
            from oj_common import push_oj_message
            parts = [f"📋 测试数据补充: #{problem.get('pid','?')} {problem.get('title','')[:20]}"]
            parts.append(f"  样例: {samples}组 | std: {'✅' if std_code else '已有'} | 生成器: {'✅' if data_code else '❌'}")
            parts.append(f"  触发: {'✅' if gen_ok else '❌'} | 上传: {'✅' if upload_ok else '⚠️'}")
            push_oj_message(self.session, self.root, "\n".join(parts), push_uids=uids)
        except Exception:
            pass

    def trigger_generation(self, gen_file: str = "data.cpp", count: int = 20) -> bool:
        """触发 OJ 生成测试数据。需要 std.cpp 和数据生成器已上传。"""
        try:
            r = self.session.post(
                f"{self.api_base}/p/{self.pid}/files",
                data={
                    "operation": "generate_testdata",
                    "gen": gen_file,
                    "std": "std.cpp",
                    "count": str(count),
                },
                headers={"Accept": "application/json"},
                timeout=30)
            if r.status_code == 200:
                log.info("[+] 测试数据生成已触发 (gen=%s, count=%d)", gen_file, count); return True
            log.warning("[-] 触发生成失败: status=%d", r.status_code)
        except Exception as e:
            log.warning("[-] 触发生成异常: %s", e)
        return False

    # ── 主流程 ─────────────────────────────────────────
    def supplement(self) -> bool:
        tmpdir = tempfile.mkdtemp(prefix=f"td_{self.pid}_")
        try:
            return self._supplement_impl(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            log.info("[*] 临时文件已清理")

    # ── 辅助: 检查题解数 ──
    def has_existing_solutions(self) -> bool:
        try:
            r = self.session.get(
                f"{self.api_base}/p/{self.pid}/files",
                headers={"Accept": "application/json"}, timeout=15)
            return r.json().get("solutionCount", 0) > 0
        except Exception:
            return False

    # ── 辅助: 提交代码并等待 AC（带修正）──
    def submit_and_verify_ac(self, code: str, lang: str = "cc.cc14o2") -> bool:
        """提交代码到 OJ，等待评测，非 AC 则修正，直到 AC 或放弃。"""
        # 提交
        r = self.session.post(
            f"{self.api_base}/p/{self.pid}/submit",
            data={"lang": lang, "code": code},
            headers={"Accept": "application/json"}, timeout=15)
        if r.status_code not in (200, 302, 303):
            log.warning("  [-] 提交失败: status=%d", r.status_code)
            return False
        rid = None
        if r.status_code == 200:
            rid = r.json().get("rid", "")
        else:
            m = re.search(r"/record/([a-f0-9]+)", r.headers.get("Location", ""))
            if m: rid = m.group(1)
        if not rid:
            log.warning("  [-] 无法获取 rid"); return False

        # 等待评测
        log.info("  [*] 等待评测 rid=%s ...", rid[:12])
        record_url = f"{self.api_base}/record/{rid}" if self.domain != "system" else f"{self.root}/record/{rid}"
        for _ in range(30):
            time.sleep(10)
            rr = self.session.get(record_url, headers={"Accept": "application/json"}, timeout=15)
            if rr.status_code != 200: continue
            rdoc = rr.json().get("rdoc", {})
            cases = rdoc.get("testCases", [])
            score = rdoc.get("score", 0)
            if not cases: continue  # 还在评测
            all_ac = all(c.get("status", 0) == 1 for c in cases)
            if all_ac and score == 100:
                log.info("  [+] AC! score=%d", score)
                return True
            break  # 有结果但非 AC

        log.warning("  [-] 未 AC, score=%d", score)
        return False

    def _supplement_impl(self, tmpdir: str) -> bool:
        if not self.login():
            return False

        problem = self.get_problem()
        if not problem:
            log.error("[-] 无法获取题目"); return False
        if not problem["has_no_data"]:
            log.warning("[!] 该题目可能已有测试数据，继续尝试补充 ...")

        uploaded_ok = True
        skip_samples = self.has_existing_solutions()

        # 1. 提取/生成样例（已有题解则跳过）
        if skip_samples:
            log.info("[*] 已有题解，跳过样例提取，直接生成 std.cpp")
            samples = []
        else:
            samples = self.extract_samples(problem["content"])
            if not samples:
                log.info("[*] 无结构样例，用 AI 生成 ...")
                samples = self.generate_samples_with_ai(problem)
            if not samples:
                log.error("[-] 无法获取样例"); return False
            log.info("[*] 共 %d 组样例", len(samples))

            # 2. 上传样例
            log.info("[*] 上传样例文件 ...")
            for s in samples:
                n = s["n"]
                for ext, data in [("in", s["in"]), ("out", s["out"])]:
                    if not self.upload_file(f"{n}.{ext}", data, tmpdir=tmpdir):
                        uploaded_ok = False

        # 3. 生成 std.cpp
        std_code = None
        if self.has_std_cpp():
            log.info("[*] std.cpp 已存在")
        else:
            log.info("[*] 生成 std.cpp ...")
            std_code = self.generate_std_cpp(problem)
            if std_code:
                # 必须评测 AC 后才上传
                log.info("[*] 评测 std.cpp ...")
                is_ac = self.submit_and_verify_ac(std_code)
                if is_ac:
                    if not self.upload_file("std.cpp", std_code, tmpdir=tmpdir):
                        uploaded_ok = False
                else:
                    log.warning("[!] std.cpp 未 AC，不进行上传。请手动修复后重新运行")
                    # 尝试修正（最多 2 次）
                    for fix_i in range(2):
                        log.info("[*] 第 %d 次修正 std.cpp ...", fix_i + 1)
                        fixed_code = self._fix_std_cpp(problem, std_code)
                        if not fixed_code:
                            break
                        std_code = fixed_code
                        if self.submit_and_verify_ac(std_code):
                            log.info("[+] 修正后 AC!")
                            if not self.upload_file("std.cpp", std_code, tmpdir=tmpdir):
                                uploaded_ok = False
                            break
                    else:
                        log.warning("[!] std.cpp %d 次修正仍未 AC", 2)

        # 4. 生成 data.cpp
        log.info("[*] 生成数据生成器 ...")
        data_code = self.generate_data_cpp(problem)
        gen_file = "data.cpp"
        if data_code:
            ext = "py" if ("def " in data_code or "argv" in data_code and "#include" not in data_code) else "cpp"
            gen_file = f"data.{ext}"
            if not self.upload_file(gen_file, data_code, tmpdir=tmpdir):
                uploaded_ok = False

        # 5. 触发生成
        log.info("[*] 触发测试数据生成 ...")
        gen_ok = self.trigger_generation(gen_file=gen_file)

        # 通知推送
        self._notify_result(problem, len(samples), std_code, data_code, gen_ok, uploaded_ok)

        log.info("\n" + "=" * 50)
        log.info("  任务完成!")
        log.info(f"  题目: {problem['title']} (#{self.pid})")
        if samples:
            log.info(f"  样例: {len(samples)} 组")
        log.info(f"  std.cpp: {'已生成' if std_code else '已存在/跳过'}")
        log.info(f"  data.cpp: {'已生成' if data_code else '失败'}")
        log.info(f"  生成触发: {'已触发' if gen_ok else '失败'}")
        if not uploaded_ok:
            log.info("  部分文件上传失败，请检查题目权限")
        log.info("=" * 50)
        return uploaded_ok and gen_ok

    def _fix_std_cpp(self, problem: dict, prev_code: str) -> str | None:
        """修正 std.cpp（使用 AI fix 逻辑）"""
        client = self._get_ai_client()
        prompt = (
            f"上一份 std.cpp 代码未通过评测。\n"
            f"## 题目\n【{problem['title']}】\n\n{problem['content'][:2000]}\n\n"
            f"## 之前的代码\n```cpp\n{prev_code[:4000]}\n```\n\n"
            "请修正代码，确保正确性。输出格式：\n"
            "```cpp\n（修正后的完整代码）\n```"
        )
        r = client.chat(messages=[{"role": "user", "content": prompt}],
                        model=self.model, max_tokens=16384)
        code = r["content"] if r else ""
        m = re.search(r'```(?:cpp|c\+\+)\s*\n(.+?)```', code, re.DOTALL)
        return m.group(1).strip() if m else None


# ═══════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════
def main():
    load_dotenv()

    from oj_common import setup_logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(message)s",
                        datefmt="%m-%d %H:%M:%S")

    parser = argparse.ArgumentParser(description="测试数据补充工具")
    parser.add_argument("url", help="题目链接")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="AI 模型 (默认: deepseek-v4-pro)")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
    elif args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    td = TestDataSupplement(args.url, model=args.model)
    ok = td.supplement()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
