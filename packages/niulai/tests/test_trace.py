"""trace 写入与回读测试。

两个重点：

1. **损坏尾行要被跳过。** 进程被杀时最后一行可能只写了一半（architecture.md
   7.13），回放不能因为半行 JSON 就崩掉。
2. **写入失败要吭声。** EventEmitter 会吞掉 sink 的异常（旁路不能拖垮 agent），
   所以 TraceWriter 自己必须把写入失败暴露出来，否则磁盘满时 trace 静默全丢。
"""

from __future__ import annotations

import json

from niulai.events import EventEmitter, EventType
from niulai.trace import TraceWriter, read_trace


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
