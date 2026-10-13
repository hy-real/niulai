"""上下文裁剪测试。

最要紧的一条是结构不能被破坏：`tool_call` 和它的 `tool_result` 是一对，
拆散就是非法请求。按"条"裁剪（`del history[:n]`）就是栽在这里。
"""

from __future__ import annotations

from niulai.context import fit_context
from niulai.llm import Message, ToolCall


def _tool_message(name: str = "stock_quote", content: str = "x" * 500, cid: str = "c1") -> Message:
    return Message(role="tool", content=content, tool_call_id=cid, name=name)


def _round(index: int, content: str = "x" * 500) -> list[Message]:
    """造一轮完整的工具调用：assistant(tool_calls) + tool 结果。"""
    call = ToolCall(id=f"c{index}", name="stock_quote", arguments=f'{{"symbol":"{index}"}}')
    return [
        Message(role="assistant", content="", tool_calls=[call]),
        _tool_message(content=content, cid=f"c{index}"),
    ]


def _conversation(rounds: int) -> list[Message]:
    messages: list[Message] = [Message(role="system", content="S")]
    for index in range(rounds):
        messages.append(Message(role="user", content=f"第 {index} 个问题"))
        messages.extend(_round(index))
    return messages


def test_工具结果不够多时不裁剪():
    messages = _conversation(2)
    view, stats = fit_context(messages, keep_recent_tool_results=4)
    assert stats.changed is False
    assert view == messages


def test_只压缩更早的工具结果():
    messages = _conversation(4)  # 4 条工具结果
    view, stats = fit_context(messages, keep_recent_tool_results=2)

    assert stats.omitted == 2
    # 最近两条原样保留
    kept = [m for m in view if m.role == "tool" and m.content == "x" * 500]
    assert len(kept) == 2


def test_消息条数与角色序列完全不变():
    """压缩是换内容，不是删消息 —— 结构必须一字不动。"""
    messages = _conversation(5)
    view, _ = fit_context(messages, keep_recent_tool_results=1)

    assert len(view) == len(messages)
    assert [m.role for m in view] == [m.role for m in messages]


def test_被压缩的tool_call仍然有对应的tool_result():
    """拆散成对消息 = 非法请求。这条是回归测试。"""
    messages = _conversation(5)
    view, _ = fit_context(messages, keep_recent_tool_results=1)

    for index, message in enumerate(view):
        if message.role == "assistant" and message.tool_calls:
            ids = {call.id for call in message.tool_calls}
            following = {m.tool_call_id for m in view[index + 1 :] if m.role == "tool"}
            assert ids <= following, f"第 {index} 条的 tool_call 没有对应结果"


def test_占位内容说清原大小并指引重取():
    messages = _conversation(3)
    view, _ = fit_context(messages, keep_recent_tool_results=1)

    placeholder = [m for m in view if m.role == "tool"][0].content
    assert "省略" in placeholder
    assert "500" in placeholder  # 原字符数，让模型知道数据规模
    assert "重新调用" in placeholder
    assert "stock_quote" in placeholder


def test_不改动原列表():
    messages = _conversation(4)
    before = [m.content for m in messages]
    fit_context(messages, keep_recent_tool_results=1)
    assert [m.content for m in messages] == before


def test_保留零条时全部压缩():
    messages = _conversation(3)
    view, stats = fit_context(messages, keep_recent_tool_results=0)
    assert stats.omitted == 3
    assert all("省略" in (m.content or "") for m in view if m.role == "tool")


def test_统计能报出省下多少字符():
    messages = _conversation(3)
    _, stats = fit_context(messages, keep_recent_tool_results=1)
    assert stats.saved_chars == 1000  # 两条 × 500
