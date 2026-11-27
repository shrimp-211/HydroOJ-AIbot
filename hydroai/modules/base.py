"""模块声明。

一个模块 = 一个 Python 包 + 一个 ``ModuleSpec``。约定：

* 包内提供 ``cli.main(argv) -> int`` 作为命令行入口；
* 模块之间只通过 ``requires`` 声明的公开接口依赖，不触碰对方内部实现；
* 对 OJ 的所有访问都经 ``hydroai.oj``（ojkit），不在模块内拼 URL。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModuleSpec:
    name: str                       # 命令行与配置里使用的短名
    title: str                      # 展示名
    summary: str                    # 一句话说明
    package: str                    # 实现所在的 Python 包
    entry: str                      # ``包.模块:函数``，签名 ``(argv: list[str]) -> int``
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()  # 依赖的其他模块名
    config_keys: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict, compare=False)
