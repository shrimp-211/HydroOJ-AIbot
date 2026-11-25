#!/usr/bin/env python3
"""题面样例提取 — 求解前本地自测与测试数据补充共用。

原先只在 testdata_supplement 内部实现，agent 求解需要同一套解析，
抽成公共模块避免两份实现逐渐不一致。
"""

import logging
import re

log = logging.getLogger(__name__)

# 有编号的围栏：```input1 / ```input_1 / ```in1
# 注意这里只能用 [ \t] 不能用 \s：\s 会吃掉标签后的换行，
# 导致「```input\n1 2」里的 1 被当成编号吞掉（曾把 in 解析成 "```"）。
_FENCE_INPUT = re.compile(r"```[ \t]*(?:input|in)[ \t_]*(\d*)[ \t]*\r?\n(.*?)```",
                          re.DOTALL | re.IGNORECASE)
_FENCE_OUTPUT = re.compile(r"```[ \t]*(?:output|out|answer|ans)[ \t_]*(\d*)[ \t]*\r?\n(.*?)```",
                           re.DOTALL | re.IGNORECASE)
_FENCE_ANY = re.compile(r"```[ \t]*(input|in|output|out|answer|ans)[ \t_]*\d*[ \t]*\r?\n(.*?)```",
                        re.DOTALL | re.IGNORECASE)
# 「样例输入 #1」/「输入格式」这类标题后紧跟的裸代码块
_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_LABELED_IN = re.compile(r"(?:样例)?输入\s*#?\s*(\d*)\s*[:：]?\s*\n?(.+?)(?=(?:样例)?输出|```|$)", re.DOTALL)
_LABELED_OUT = re.compile(r"(?:样例)?输出\s*#?\s*(\d*)\s*[:：]?\s*\n?(.+?)(?=(?:样例)?输入|```|$)", re.DOTALL)


def _clean(text: str) -> str:
    """去掉首尾空白与 HTML 残留，保留内部换行（对比时按行处理）。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    return text.strip("\n \t\r")


def extract_samples(content) -> list[dict]:
    """从题面文本提取样例输入输出。

    返回 [{"in": "...", "out": "...", "n": 1}, ...]，无法识别时返回空列表。
    依次尝试：
      1. ```inputN / ```outputN（按编号配对）
      2. ```input / ```output 交错出现（相邻配对）
      3. 「输入 #1」「输出 #1」标题 + 任意代码块
      4. 纯文本「输入：... 输出：...」
    """
    text = str(content or "")
    if not text:
        return []

    # 方式 1：编号围栏按编号配对
    inputs = list(_FENCE_INPUT.finditer(text))
    outputs = list(_FENCE_OUTPUT.finditer(text))
    if inputs and outputs:
        in_map = {m.group(1) or "0": _clean(m.group(2)) for m in inputs}
        out_map = {m.group(1) or "0": _clean(m.group(2)) for m in outputs}
        common = set(in_map) & set(out_map)
        samples = [{"in": in_map[k], "out": out_map[k],
                    "n": i + 1 if not k.isdigit() else int(k)}
                   for i, k in enumerate(sorted(common))]
        if samples:
            log.debug("[样例] 编号围栏 %d 组", len(samples))
            return _renumber(samples)

    # 方式 2：input/output 交错，必须是相邻的一对
    blocks = list(_FENCE_ANY.finditer(text))
    samples = []
    i = 0
    while i < len(blocks) - 1:
        b1, b2 = blocks[i], blocks[i + 1]
        kind1 = b1.group(1).lower()
        kind2 = b2.group(1).lower()
        if kind1 in ("input", "in") and kind2 in ("output", "out", "answer", "ans"):
            samples.append({"in": _clean(b1.group(2)), "out": _clean(b2.group(2)),
                            "n": len(samples) + 1})
            i += 2
        else:
            i += 1
    if samples:
        log.debug("[样例] 交错围栏 %d 组", len(samples))
        return samples

    # 方式 3：「输入 #1」标题 + 紧随其后的任意代码块
    sections = _split_labeled_sections(text)
    if sections:
        log.debug("[样例] 标题分段 %d 组", len(sections))
        return sections

    # 方式 4：裸文本 输入：/ 输出：
    ins = [_clean(m.group(2)) for m in _LABELED_IN.finditer(text)]
    outs = [_clean(m.group(2)) for m in _LABELED_OUT.finditer(text)]
    pairs = [{"in": a, "out": b, "n": i + 1} for i, (a, b) in enumerate(zip(ins, outs))
             if a and b]
    if pairs:
        log.debug("[样例] 文本标注 %d 组", len(pairs))
    return pairs


def _split_labeled_sections(text: str) -> list[dict]:
    """处理「样例输入 #1 <代码块> 样例输出 #1 <代码块>」这种混排。"""
    events = []
    for m in re.finditer(r"(?:样例)?(输入|输出)\s*#?\s*(\d*)", text):
        events.append(("in" if m.group(1) == "输入" else "out", m.group(2), m.end()))
    if not events:
        return []
    samples = []
    pending_in = None
    for kind, num, pos in events:
        rest = text[pos:]
        fence = _BLOCK.search(rest)
        if not fence:
            continue
        # 只接受紧跟在标题后的代码块（中间不夹另一个标题）
        nxt = re.search(r"(?:样例)?(?:输入|输出)\s*#?\s*\d*", rest)
        if nxt and nxt.start() < fence.start():
            continue
        body = _clean(fence.group(1))
        if kind == "in":
            pending_in = body
        elif pending_in is not None:
            samples.append({"in": pending_in, "out": body, "n": len(samples) + 1})
            pending_in = None
    return samples


def _renumber(samples: list[dict]) -> list[dict]:
    for i, s in enumerate(samples, 1):
        s["n"] = i
    return samples
