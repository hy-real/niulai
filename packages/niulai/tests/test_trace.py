"""trace 写入与回读测试。

两个重点：

1. 损坏尾行要被跳过。进程被杀时最后一行可能只写了一半，回放不能因为
   半行 JSON 就崩掉。
2. 写入失败要吭声。EventEmitter 会吞掉 sink 的异常（旁路不能拖垮 agent），
   所以 TraceWriter 自己必须把写入失败暴露出来，否则磁盘满时 trace 静默全丢。
"""

from __future__ import annotations

import json

import pytest

from niulai.agent.loop import INTERRUPTED_RESULT, _repair_transcript
from niulai.events import EventEmitter, EventType
from niulai.llm.types import Usage
from niulai.trace import TraceResumeError, TraceWriter, load_transcript, read_trace


def _emit_one(writer: TraceWriter, task: str = "测试") -> None:
    """模拟内核发一个事件：新建 emitter（seq 从 1 起）并挂上 writer。"""
    emitter = EventEmitter("test-run")
    emitter.subscribe(writer)
    emitter.emit(EventType.RUN_START, task=task)


def test_写满一行一个JSON(tmp_path):
    writer = TraceWriter("run1", directory=tmp_path)
    _emit_one(writer)
    lines = writer.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["type"] == "run_start"


def test_损坏尾行被跳过(tmp_path):
    """前两行正常 + 半行残缺：能读回前两行，不抛异常。"""
    path = tmp_path / "run1.jsonl"
    ok_line = json.dumps({"type": "run_start", "seq": 1}, ensure_ascii=False)
    ok_line2 = json.dumps({"type": "run_end", "seq": 2}, ensure_ascii=False)
    path.write_text(f"{ok_line}\n{ok_line2}\n{{\"type\": \"turn_st", encoding="utf-8")

    records = list(read_trace(path))
    assert [r["type"] for r in records] == ["run_start", "run_end"]


def test_空行被忽略(tmp_path):
    path = tmp_path / "run1.jsonl"
    path.write_text('\n{"type": "run_start"}\n\n', encoding="utf-8")
    assert len(list(read_trace(path))) == 1


def test_写入失败会警告而不是静默丢弃(tmp_path, capsys):
    """目录被删后写入：EventEmitter 吞掉 OSError，但 TraceWriter 要打到 stderr，
    且只警告一次 —— 之后的事件不再刷屏。"""
    writer = TraceWriter("run1", directory=tmp_path)
    _emit_one(writer)  # 这条正常落盘
    assert writer.path.exists()

    writer.path.unlink()  # 先删文件，目录才能删得掉
    writer._dir.rmdir()  # 模拟磁盘/权限问题：open("a") 撞上缺失目录 → OSError

    _emit_one(writer, task="第二条")  # 异常在 emit 内部被吞，这里不抛
    _emit_one(writer, task="第三条")  # 同理

    captured = capsys.readouterr()
    assert captured.err.count("trace 写入失败") == 1


# ---------------------------------------------------------------------- 恢复


def _journal_run(tmp_path, *, with_run_end: bool) -> TraceWriter:
    """写一段与真实内核同构的事件序列：一问、一答、一次工具调用。"""
    writer = TraceWriter("run-r", directory=tmp_path)
    emitter = EventEmitter("run-r")
    emitter.subscribe(writer)
    e = emitter.emit
    e(EventType.RUN_START, task="茅台多少钱", model="m", protocol="openai")
    e(EventType.ASSISTANT_MESSAGE, turn=1, text="",
      tool_calls=[{"id": "c1", "name": "stock_quote", "arguments": "{\"symbol\": \"600519\"}"}])
    e(EventType.TOOL_RESULT, turn=1, name="stock_quote", ok=True, content_chars=30,
      original_chars=30, artifact=None, preview="贵州茅台 1253.80",
      tool_call_id="c1", content="贵州茅台最新价 1253.80 元")
    e(EventType.ASSISTANT_MESSAGE, turn=2, text="最新价 1253.80 元", tool_calls=[])
    e(EventType.USAGE, turn=2, cumulative=Usage(10, 5, 15).to_dict())
    if with_run_end:
        e(EventType.RUN_END, stop_reason="done", turns=2, usage=Usage(10, 5, 15).to_dict())
    return writer


def test_从journal重建完整transcript(tmp_path):
    """正常结束的 run：按事件顺序重建出 user / assistant / tool / assistant。"""
    writer = _journal_run(tmp_path, with_run_end=True)
    plan = load_transcript(writer.path)

    assert not plan.interrupted
    assert plan.run_id == "run-r"
    roles = [m.role for m in plan.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert plan.messages[0].content == "茅台多少钱"
    assert plan.messages[1].tool_calls[0].id == "c1"
    # tool 消息拿的是完整 content，不是 600 字预览
    assert plan.messages[2].content == "贵州茅台最新价 1253.80 元"
    assert plan.messages[2].tool_call_id == "c1"


def test_中断的run_悬空tool_call由repair补结果未知(tmp_path):
    """进程死在工具执行中：journal 有 assistant 声明、无 tool_result。
    重建出悬空调用后，_repair_transcript 补「结果未知」，不猜成败。"""
    writer = TraceWriter("run-x", directory=tmp_path)
    emitter = EventEmitter("run-x")
    emitter.subscribe(writer)
    emitter.emit(EventType.RUN_START, task="查五粮液")
    emitter.emit(EventType.ASSISTANT_MESSAGE, turn=1, text="",
                 tool_calls=[{"id": "c9", "name": "stock_quote", "arguments": "{}"}])
    # 没有 TOOL_RESULT，也没有 RUN_END —— 进程在这里被杀了

    plan = load_transcript(writer.path)
    assert plan.interrupted
    assert [m.role for m in plan.messages] == ["user", "assistant"]

    repaired = _repair_transcript(plan.messages)
    assert repaired == 1
    tail = plan.messages[-1]
    assert tail.role == "tool" and tail.tool_call_id == "c9"
    assert tail.content == INTERRUPTED_RESULT


def test_熔断事件也参与重建(tmp_path):
    """被拦下的调用同样回灌了 tool 消息，恢复时不能丢。"""
    writer = TraceWriter("run-b", directory=tmp_path)
    emitter = EventEmitter("run-b")
    emitter.subscribe(writer)
    emitter.emit(EventType.RUN_START, task="再查一次")
    emitter.emit(EventType.ASSISTANT_MESSAGE, turn=1, text="",
                 tool_calls=[{"id": "c1", "name": "stock_quote", "arguments": "{}"}])
    emitter.emit(EventType.CIRCUIT_BREAK, turn=1, name="stock_quote",
                 reason="repeat", message="同参数重复调用已被拦下", tool_call_id="c1")
    emitter.emit(EventType.RUN_END, stop_reason="max_turns", turns=1)

    plan = load_transcript(writer.path)
    tool_msgs = [m for m in plan.messages if m.role == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0].tool_call_id == "c1"
    assert tool_msgs[0].is_error


def test_旧格式journal缺对账id_拒绝恢复(tmp_path):
    """旧 trace 的 tool_call 没有 id，无法与结果对账 —— 明确拒绝而不是静默修。"""
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"type": "run_start", "run_id": "old", "task": "t"}, ensure_ascii=False) + "\n"
        + json.dumps({"type": "assistant_message", "text": "",
                      "tool_calls": [{"name": "stock_quote", "arguments": "{}"}]},
                     ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(TraceResumeError):
        load_transcript(path)
