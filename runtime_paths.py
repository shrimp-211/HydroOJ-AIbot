"""Separate immutable application files from persistent runtime data."""
import os
from pathlib import Path


def data_dir(default=None) -> Path:
    configured = os.environ.get('OJ_DATA_DIR')
    return Path(configured).expanduser().resolve() if configured else Path(default or Path.cwd()).resolve()


def data_file(name, default=None) -> Path:
    return data_dir(default) / name
