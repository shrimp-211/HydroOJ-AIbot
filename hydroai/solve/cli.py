"""solver 模块入口：``hydro.py solver <子命令> [参数]``。

子命令与原脚本一一对应，参数原样透传，原有命令行入口保持不变。
"""
from __future__ import annotations

import importlib
import sys

SUBCOMMANDS = {
    "solve": "hydroai.solve.solver:main",          # 单题求解 + 题解
    "contest": "hydroai.solve.contest:main",       # 比赛 / 训练批量
    "benchmark": "hydroai.solve.benchmark:main",   # 标程题解
    "testdata": "hydroai.solve.testdata:main",     # 测试数据补充
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in SUBCOMMANDS:
        print("用法: hydro.py solver {" + "|".join(SUBCOMMANDS) + "} [参数...]")
        return 0 if (not argv or argv[0] in ("-h", "--help")) else 2
    module_path, func = SUBCOMMANDS[argv[0]].split(":")
    entry = getattr(importlib.import_module(module_path), func)
    old = sys.argv
    sys.argv = [f"solver {argv[0]}", *argv[1:]]
    try:
        return int(entry() or 0)
    finally:
        sys.argv = old
