#!/usr/bin/env python3
"""兼容入口：实现位于 :mod:`hydroai.web.app`。"""
from hydroai.web.app import main

if __name__ == "__main__":
    raise SystemExit(main())
