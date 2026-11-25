#!/usr/bin/env python3
"""兼容入口：实现位于 :mod:`hydroai.config.cli`。"""
from hydroai.config.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
