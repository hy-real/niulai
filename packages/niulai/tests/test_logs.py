"""logging 接入测试。

两个重点：

1. **stdout 纯洁性。** `--json` 模式的 stdout 是事件流管道，日志只能去 stderr。
   埋点本身散在各层测不到，这里锁的是 setup 的行为。
2. **级别解析要宽容。** 配置打错字（`NIULAI_LOG_LEVEL=DEBUGG`）不该挂掉，
   退回 warning。
"""

from __future__ import annotations

import io
import logging

from niulai.logs import get_logger, setup_logging


def _reset() -> None:
    """测试之间互不污染：清掉 niulai 这个 logger 的状态。"""
    logger = logging.getLogger("niulai")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


def test_日志进stderr不进stdout():
    stream = io.StringIO()
    setup_logging("debug", stream=stream)
    get_logger("llm.test").debug("一条调试信息")

    assert "一条调试信息" in stream.getvalue()


def test_级别过滤warning下debug不输出():
    stream = io.StringIO()
    setup_logging("warning", stream=stream)
    get_logger("llm.test").debug("不该出现的调试信息")

    assert stream.getvalue() == ""


def test_级别warning的会输出():
    stream = io.StringIO()
    setup_logging("warning", stream=stream)
    get_logger("datasource.test").warning("重试了")

    assert "重试了" in stream.getvalue()


def test_未知的级别退回warning():
    setup_logging("debugg")  # 打错字
    assert logging.getLogger("niulai").level == logging.WARNING
    _reset()


def test_调试级别自动落盘到logs目录(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    setup_logging("debug", stream=io.StringIO())
    get_logger("llm.test").debug("落盘验证")

    log_file = tmp_path / "logs" / "niulai.log"
    assert log_file.exists()
    assert "落盘验证" in log_file.read_text(encoding="utf-8")


def test_默认warning级别不建日志文件(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    setup_logging("warning", stream=io.StringIO())
    get_logger("llm.test").warning("只在stderr")

    assert not (tmp_path / "logs" / "niulai.log").exists()


def test_重复setup不会产生重复handler():
    stream = io.StringIO()
    setup_logging("debug", stream=stream)
    setup_logging("debug", stream=stream)
    logger = logging.getLogger("niulai")
    # DEBUG 级别会额外挂一个默认文件 handler（logs/niulai.log），
    # 所以总数可能是 2——不变量是"到终端的 console handler 只有一个"
    console_count = sum(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
    assert console_count == 1

    get_logger("llm.test").info("只该出现一次")
    assert stream.getvalue().count("只该出现一次") == 1
    _reset()
