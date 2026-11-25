"""HydroOJ AIbot — 解题、监控、用量与额度管理。

入口脚本保留在仓库根目录（``oj_solver.py`` 等），实现按领域拆分在以下子包：

- :mod:`hydroai.config`  配置加载、校验、命令行与运行时路径
- :mod:`hydroai.ai`      AI 客户端、协议、路由、定价与提示词
- :mod:`hydroai.oj`      OJ 站点交互、样例解析与本地评测
- :mod:`hydroai.solve`   单题/比赛求解、Agent、题解与测试数据
- :mod:`hydroai.monitor` 比赛守护、私信与 OneBot 接入
- :mod:`hydroai.usage`   逐请求 token/费用统计与订阅额度
- :mod:`hydroai.web`     Web 面板与仪表盘
"""

__version__ = "2.0.0"
