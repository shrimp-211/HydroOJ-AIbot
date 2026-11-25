"""Separate immutable application files from persistent runtime data."""
import os
from pathlib import Path

# Repository/application root (the directory containing the root entry scripts).
APP_ROOT = Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """应用根目录，用于定位入口脚本、模板等只读资源。"""
    return APP_ROOT


def resource_path(*parts) -> Path:
    """按应用根目录拼接只读资源路径。"""
    return APP_ROOT.joinpath(*parts)


def data_dir(default=None) -> Path:
    configured = os.environ.get('OJ_DATA_DIR')
    return Path(configured).expanduser().resolve() if configured else Path(default or Path.cwd()).resolve()


def data_file(name, default=None) -> Path:
    return data_dir(default) / name


def config_file(name, default=None) -> Path:
    """用户配置文件位置（config.json / .env / 价格缓存）。

    优先级：
    1. ``OJ_DATA_DIR``（Docker 的 /data，或显式指定的数据目录）
    2. ``<root>/config/<name>``（本地统一布局）
    3. ``<root>/<name>``（兼容旧布局）

    都不存在时返回 ``config/<name>``，供初始化命令创建。
    本地不设置 OJ_DATA_DIR 时使用 config/；Docker 设置 /data 时使用卷内配置。
    """
    configured = os.environ.get('OJ_DATA_DIR')
    if configured:
        return Path(configured).expanduser().resolve() / name
    root = Path(default or project_root()).resolve()
    candidate = root / 'config' / name
    legacy = root / name
    if candidate.exists():
        return candidate
    if legacy.exists():
        return legacy
    return candidate
