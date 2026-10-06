"""会话持久化测试（SQLite 存储）。

最要紧的一条是**往返不丢东西**：`tool_calls` 和 `raw_blocks` 少一样，
恢复出来的 transcript 就发不出去了（Anthropic 的 thinking 块缺了直接 400）。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from niulai.llm import Message, ToolCall, Usage
from niulai.session import SessionError, SessionStore


def _store(tmp_path) -> SessionStore:
    return SessionStore(db=tmp_path / "niulai.db")


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


def test_重复保存不产生重复消息(tmp_path):
    """保存 = 整段重写（全删全插），连存两次也不能把历史翻倍。"""
    store = _store(tmp_path)
    session = store.create()
    session.messages = [Message(role="user", content="你好")]
    store.save(session)
    store.save(session)
    assert [m.content for m in store.load(session.id).messages] == ["你好"]


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


def test_取最近一条按更新时间(tmp_path):
    store = _store(tmp_path)
    first = store.create()
    first.title = "早的"
    first.updated_at = "2026-01-01T09:00:00+08:00"
    store.save(first)
    second = store.create()
    second.title = "晚的"
    second.updated_at = "2026-01-02T09:00:00+08:00"
    store.save(second)

    assert store.latest().title == "晚的"


def test_按id前缀查找(tmp_path):
    store = _store(tmp_path)
    session = store.create()
    store.save(session)
    assert store.load(session.id[:8]).id == session.id


def test_存储是单个sqlite文件(tmp_path):
    """P4 的落点：会话进 niulai.db（单文件、零运维），messages 按序存 JSON payload。"""
    store = _store(tmp_path)
    session = store.create()
    session.messages = [Message(role="user", content="你好")]
    store.save(session)

    assert store.path == tmp_path / "niulai.db"
    with closing(sqlite3.connect(store.path)) as conn:
        payloads = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload FROM messages ORDER BY ord"
            ).fetchall()
        ]
    assert payloads[0]["role"] == "user"
    assert payloads[0]["content"] == "你好"
    # 没值的字段不写，payload 才读得下去
    assert "tool_calls" not in payloads[0]


def test_会话id不接受任意路径(tmp_path):
    """load 的输入经 webapi 原样暴露给浏览器 —— 围栏必须在 _resolve 收口。

    SQLite 版的围栏更硬：返回值只能是库内已存在的主键，不存在
    "按路径读任意文件" 这条路。
    """
    (tmp_path / "secret.json").write_text('{"id":"x"}', encoding="utf-8")
    store = _store(tmp_path)

    with pytest.raises(SessionError):
        store.load(str(tmp_path / "secret.json"))  # 绝对路径
    with pytest.raises(SessionError):
        store.load("../secret")       # 相对穿越
    with pytest.raises(SessionError):
        store.load("")                # 空串
