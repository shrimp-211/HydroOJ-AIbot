#!/usr/bin/env python3
"""统一入口：``python hydro.py <模块> [参数]``；不带参数列出所有模块。"""
import sys

from hydroai.modules import describe, run_module


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("modules", "-h", "--help"):
        print("可用模块（python hydro.py <模块> --help 查看各自参数）:\n")
        for m in describe():
            dep = f"  依赖: {', '.join(m['requires'])}" if m["requires"] else ""
            print(f"  {m['name']:<10} {m['title']} — {m['summary']}{dep}")
        return 0
    try:
        return run_module(argv[0], argv[1:])
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
