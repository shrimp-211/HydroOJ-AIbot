"""模块框架：每个业务能力是一个独立模块，经注册表发现并由统一入口调用。"""
from hydroai.modules.base import ModuleSpec
from hydroai.modules.registry import describe, get_spec, list_specs, run_module

__all__ = ["ModuleSpec", "describe", "get_spec", "list_specs", "run_module"]
