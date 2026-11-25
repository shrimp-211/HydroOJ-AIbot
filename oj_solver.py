#!/usr/bin/env python3
"""兼容入口：实现位于 :mod:`hydroai.solve.solver`。"""
from hydroai.solve.solver import main

if __name__ == "__main__":
    raise SystemExit(main())
