"""跨平台数据目录。

P0 用最小实现即可：所有路径都能被环境变量 `NIULAI_HOME` 整体覆盖，
方便测试、也方便做成便携版（数据跟着程序走）。
P4 会换成更严格的平台标准目录并补上迁移逻辑。

目录布局：

    <data_dir>/
      traces/      每次 run 一个 JSONL
      sessions/    会话（多轮对话历史）
      artifacts/   大结果落盘（按 run 分子目录）

**Key 不在这里的任何地方**：它属于系统凭据管理器，见 config.py 的说明。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "niulai"


def data_dir() -> Path:
    """应用数据根目录（不保证已存在）。"""
    override = os.environ.get("NIULAI_HOME")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"

    return root / APP_NAME


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sub(name: str) -> Path:
    path = ensure_data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def trace_dir() -> Path:
    return _sub("traces")


def sessions_dir() -> Path:
    return _sub("sessions")


def artifacts_dir() -> Path:
    return _sub("artifacts")
