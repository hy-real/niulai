"""标准库 logging 的最小接入。

## 为什么用 logging，而不是多加一种事件

项目已有事件流（events.py）+ trace JSONL，但它们的定位是**运行轨迹**——
给 UI 和回放看的东西。而调试要的是另一类信息：HTTP 发了什么、重试了几次、
错误响应体长什么样。这类"过程性噪音"不该进事件流（会污染回放），
标准库 logging 正是干这个的。

## 两条硬规矩

1. **只写 stderr，绝不碰 stdout。** `niulai ask --json` 的 stdout 是事件流
   管道，下游按行解析 JSON——混进一行日志就把下游弄坏了。
2. **API Key 永远不进日志。** 埋点时只记 model / url / 数量，不记请求头
   和完整消息体。见 config.py 的同一原则。

## 开关

    NIULAI_LOG_LEVEL=DEBUG    # debug / info / warning / error，默认 warning
    NIULAI_LOG_FILE=路径       # 可选：显式指定日志文件

日志文件：NIULAI_LOG_FILE 设了就用它；没设但级别开到了 INFO/DEBUG，
自动落到 `~/.niulai/logs/niulai.log`（长调试不怕终端滚走）。
默认 WARNING 时文件不存在、零噪音。
"""

from __future__ import annotations

import logging
import os
import sys

from .paths import logs_dir

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get_logger(name: str) -> logging.Logger:
    """取一个 `niulai.` 前缀的子 logger，如 `get_logger("llm.openai")`。

    用前缀的原因：setup_logging 只配置 `niulai` 这一个祖先，
    子 logger 自动继承级别与 handler，各模块不用重复配置。
    """
    return logging.getLogger(f"niulai.{name}")


def setup_logging(level: str | None = None, *, stream: object = sys.stderr) -> None:
    """初始化 niulai 的日志。CLI 入口调一次即可，重复调用无害。

    `level` 显式传参优先；否则读 `NIULAI_LOG_LEVEL`；再否则 warning。
    认不出的值按 warning 处理——配置打错字不该让程序挂掉。
    """
    raw = (level or os.environ.get("NIULAI_LOG_LEVEL") or "warning").strip().lower()
    resolved = _LEVELS.get(raw, logging.WARNING)

    logger = logging.getLogger("niulai")
    logger.setLevel(resolved)
    logger.propagate = False  # 别再往 root 传——root 可能被使用方的框架配置过

    # 重复调用（测试里常见）先清掉旧 handler，避免日志重复打两遍
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt="%H:%M:%S")

    console = logging.StreamHandler(stream)  # type: ignore[arg-type]
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_path = os.environ.get("NIULAI_LOG_FILE", "").strip()
    if not file_path and resolved <= logging.INFO:
        # 调试级别自动落盘到默认位置；WARNING 及以上不打扰文件系统
        file_path = str(logs_dir() / "niulai.log")
    if file_path:
        file_handler = logging.FileHandler(file_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


__all__ = ["get_logger", "setup_logging"]
