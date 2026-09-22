"""会话持久化测试。

最要紧的一条是**往返不丢东西**：`tool_calls` 和 `raw_blocks` 少一样，
恢复出来的 transcript 就发不出去了（Anthropic 的 thinking 块缺了直接 400）。
"""

from __future__ import annotations

from niulai.llm import Message, ToolCall, Usage
from niulai.session import SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(directory=tmp_path / "sessions")


def test_新建会话有可排序的id(tmp_path):
    store = _store(tmp_path)
    a = store.create()
    assert a.id
    assert a.messages == []


def test_保存与加载往返(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.messages = [
        Message(role="user", content="茅台多少钱"),
        Message(role="assistant", content="1252.57 元"),
    ]
    store.save(session)

    loaded = store.load(session.id)
    assert [m.content for m in loaded.messages] == ["茅台多少钱", "1252.57 元"]
    assert loaded.created_at == session.created_at


def test_工具调用不丢(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.messages = [
        Message(role="user", content="茅台多少钱"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="stock_quote", arguments='{"symbol":"600519"}')],
        ),
        Message(
            role="tool",
            content="贵州茅台 1252.57",
            tool_call_id="c1",
            name="stock_quote",
            is_error=False,
        ),
        Message(role="assistant", content="1252.57 元"),
    ]
    store.save(session)
    loaded = store.load(session.id)

    call = loaded.messages[1].tool_calls
    assert call is not None
    assert call[0].id == "c1"
    assert call[0].arguments == '{"symbol":"600519"}'
    assert loaded.messages[2].tool_call_id == "c1"
    assert loaded.messages[2].name == "stock_quote"
    assert loaded.messages[2].is_error is False


def test_错误标记不丢(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.messages = [
        Message(role="tool", content="失败了", tool_call_id="c1", name="x", is_error=True)
    ]
    store.save(session)
    assert store.load(session.id).messages[0].is_error is True


def test_厂商私有块不丢(tmp_path):
    """Anthropic 的 thinking 块必须一起存 —— 恢复时少一个块就会被拒。"""
    store = _store(tmp_path)
    session = store.create()
    raw = [{"type": "thinking", "thinking": "", "signature": "sig=="}]
    session.messages = [Message(role="assistant", content="x", raw_blocks=raw)]
    store.save(session)

    loaded = store.load(session.id)
    assert loaded.messages[0].raw_blocks == raw


def test_标题取自首条用户消息(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.messages = [
        Message(role="user", content="帮我看看贵州茅台最近一个月怎么样，谢谢"),
        Message(role="assistant", content="……"),
    ]
    session.note_run("run1", Usage(1, 1, 2))
    store.save(session)

    loaded = store.load(session.id)
    assert loaded.title.startswith("帮我看看贵州茅台")
    assert len(loaded.title) <= 41  # 40 + 省略号


def test_用量与运行记录会累计(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.note_run("run1", Usage(10, 5, 15))
    session.note_run("run2", Usage(20, 5, 25))
    store.save(session)

    loaded = store.load(session.id)
    assert loaded.usage.total_tokens == 40
    assert loaded.runs == ["run1", "run2"]


def test_提问次数只数用户消息(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    session.messages = [
        Message(role="user", content="一"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c", name="t", arguments="{}")]),
        Message(role="tool", content="r", tool_call_id="c", name="t"),
        Message(role="assistant", content="答"),
        Message(role="user", content="二"),
        Message(role="assistant", content="答2"),
    ]
    assert session.turns == 2  # 不是 6 条消息，也不是 2 个 ReAct 轮


def test_取最近一条(tmp_path):
    import os

    store = _store(tmp_path)
    first = store.create()
    first.title = "早的"
    early = store.save(first)
    second = store.create()
    second.title = "晚的"
    late = store.save(second)

    # 同一秒内建的两个文件 mtime 可能相同，显式错开，否则这条测的是运气
    os.utime(early, (1_600_000_000, 1_600_000_000))
    os.utime(late, (1_700_000_000, 1_700_000_000))

    assert store.latest().title == "晚的"


def test_按id前缀查找(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    store.save(session)
    assert store.load(session.id[:8]).id == session.id


def test_会话文件是可读的json(tmp_path):
    """出问题时用文本编辑器就能看 —— 这是 JSON 而非 SQLite 的理由。"""
    import json

    store = _store(tmp_path)
    session = store.create()
    session.messages = [Message(role="user", content="你好")]
    path = store.save(session)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"] == "你好"
    # 没值的字段不写，文件才读得下去
    assert "tool_calls" not in payload["messages"][0]
