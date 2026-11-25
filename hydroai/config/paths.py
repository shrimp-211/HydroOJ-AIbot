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
