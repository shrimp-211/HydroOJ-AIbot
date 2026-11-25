#!/usr/bin/env python3
"""兼容入口：实现位于 :mod:`hydroai.cli.entrypoint`。"""
from hydroai.cli.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
