"""数据目录：一切程序生成的文件都住在 `~/.niulai/`。

统一用一个目录（而不是按平台拆 config/data/cache 三处）是刻意的：

1. "你的一切都在这一个文件夹里，删掉它 = 完全重置"，一句话讲清隐私故事；
2. 跨平台形状一致，文档只写一份；
3. 用户找得到：`cd ~/.niulai` 就能看会话、翻 trace。

布局：

    <data_dir>/
      config.toml   用户配置（唯一需要人动手的文件；env 优先级高于它）
      traces/       每次 run 一个 JSONL（运行轨迹，可回放）
      niulai.db     会话（SQLite 单文件）
      artifacts/    大结果落盘（按 run 分子目录，内容寻址命名）
      logs/         调试日志（DEBUG/INFO 级别时自动落盘）

`NIULAI_HOME` 可整体覆盖根目录（测试与便携版靠它）。Key 不在这里的任何
地方：它走环境变量。
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = ".niulai"


def data_dir() -> Path:
    """数据根目录（不保证已存在）。"""
    override = os.environ.get("NIULAI_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / APP_NAME


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


def db_file() -> Path:
    """SQLite 数据库（会话；将来自选 / 缓存也进这一个文件）。"""
    return ensure_data_dir() / "niulai.db"


def artifacts_dir() -> Path:
    return _sub("artifacts")


def logs_dir() -> Path:
    return _sub("logs")
