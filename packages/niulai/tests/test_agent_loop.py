"""内核测试：用假的 LLM 客户端跑通整个循环，**不需要 API Key**。

这是 P0 最有价值的一组测试 —— 它证明"循环、工具回灌、预算收敛、事件序列"
四件事都对，而且不依赖任何外部服务，CI 里秒级完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from niulai.agent import AgentRuntime
from niulai.config import Settings
from niulai.events import EventEmitter, EventType
from niulai.llm import LLMResponse, Message, ToolCall, ToolSpec, Usage
from niulai.tools import ToolRegistry


@dataclass
class _FakeLLM:
    """脚本化的假客户端：按顺序吐出预设响应，并记录每次收到的输入。"""

    scripted: list[LLMResponse]
    seen_tools: list[list[ToolSpec]] = field(default_factory=list)
    seen_messages: list[list] = field(default_factory=list)

    async def stream_chat(self, messages, tools=(), *, on_text=None):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools))
        response = self.scripted.pop(0)
        if on_text is not None and response.text:
            on_text(response.text)
        return response


class _QuoteTool:
    spec = ToolSpec(
        name="stock_quote",
        description="查询行情",
        parameters={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    )
    calls: list[dict] = []

    async def run(self, args: dict) -> str:
        _QuoteTool.calls.append(args)
        return "贵州茅台（600519）\n最新价：1252.57"


@pytest.fixture(autouse=True)
def _reset_tool_calls():
    _QuoteTool.calls.clear()
    yield
    _QuoteTool.calls.clear()


def _settings(**overrides) -> Settings:
    base = {"base_url": "http://fake", "api_key": "fake-key", "model": "fake-model"}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _emitter() -> tuple[EventEmitter, list]:
    events: list = []
    emitter = EventEmitter(run_id="test-run")
    emitter.subscribe(events.append)
    return emitter, events


def _tool_call(call_id: str = "c1", symbol: str = "600519") -> ToolCall:
    return ToolCall(id=call_id, name="stock_quote", arguments=f'{{"symbol":"{symbol}"}}')


async def test_一轮工具调用后给出最终回答():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(10, 5, 15), finish_reason="tool_calls"),
            LLMResponse(text="茅台现价 1252.57 元。", usage=Usage(20, 8, 28), finish_reason="stop"),
        ]
    )
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    result = await runtime.run("茅台多少钱", emitter=emitter)

    assert result.stop_reason == "done"
    assert result.turns == 2
    assert result.text == "茅台现价 1252.57 元。"
    assert result.usage.total_tokens == 43
    assert _QuoteTool.calls == [{"symbol": "600519"}]

    kinds = [event.type for event in events]
    assert EventType.TOOL_CALL in kinds
    assert EventType.TOOL_RESULT in kinds
    assert kinds.index(EventType.TOOL_CALL) < kinds.index(EventType.TOOL_RESULT)


async def test_工具结果以_tool_角色回灌给模型():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2)),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    await runtime.run("x", emitter=emitter)

    second_round = llm.seen_messages[1]
    tool_messages = [m for m in second_round if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"
    assert "1252.57" in tool_messages[0].content


async def test_模型一直要工具时最后一轮必须收口():
    llm = _FakeLLM(
        [LLMResponse(tool_calls=[_tool_call(f"c{i}")], usage=Usage(1, 1, 2)) for i in range(5)]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(
        settings=_settings(max_turns=3), llm=llm, tools=ToolRegistry([_QuoteTool()])  # type: ignore[arg-type]
    )

    result = await runtime.run("x", emitter=emitter)

    assert result.turns == 3
    assert result.stop_reason == "max_turns"
    # 最后一轮不发放工具，否则"收口"是假的
    assert llm.seen_tools[-1] == []


async def test_超出token预算时强制收敛():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(400, 400, 800)),
            LLMResponse(text="收口回答", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(
        settings=_settings(token_budget=100, max_turns=10),  # type: ignore[arg-type]
        llm=llm,
        tools=ToolRegistry([_QuoteTool()]),
    )

    result = await runtime.run("x", emitter=emitter)

    assert result.stop_reason == "token_budget"
    assert result.text == "收口回答"


async def test_事件序列完整且序号连续():
    llm = _FakeLLM([LLMResponse(text="你好", usage=Usage(1, 1, 2))])
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([]))  # type: ignore[arg-type]

    await runtime.run("x", emitter=emitter)

    kinds = [event.type for event in events]
    assert kinds[0] is EventType.RUN_START
    assert kinds[-1] is EventType.RUN_END
    assert all(event.run_id == "test-run" for event in events)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))


async def test_没有工具时也能正常回答():
    llm = _FakeLLM([LLMResponse(text="我只是聊天", usage=Usage(5, 3, 8))])
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([]))  # type: ignore[arg-type]

    result = await runtime.run("你好", emitter=emitter)

    assert result.stop_reason == "done"
    assert result.turns == 1
    assert result.text == "我只是聊天"


# --------------------------------------------------------------------------
# 多协议相关的两条：内核必须原样搬运它看不懂的东西
# --------------------------------------------------------------------------


class _BrokenTool:
    spec = ToolSpec(name="broken", description="总是失败", parameters={"type": "object", "properties": {}})

    async def run(self, args: dict) -> str:
        raise RuntimeError("上游炸了")


async def test_厂商私有的raw_blocks会被原样透传到下一轮():
    """Anthropic 的 thinking 块必须原样回传，否则续跑被拒。
    内核不认识它的内容 —— 这一条测的是"别弄丢"。"""
    raw = [{"type": "thinking", "thinking": "", "signature": "sig=="}]
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2), raw_blocks=raw),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    await runtime.run("x", emitter=emitter)

    assistant = [m for m in llm.seen_messages[1] if m.role == "assistant"]
    assert assistant[-1].raw_blocks is raw


async def test_工具失败要给模型带上is_error():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="broken", arguments="{}")]),
            LLMResponse(text="失败了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_BrokenTool()]))  # type: ignore[arg-type]

    await runtime.run("x", emitter=emitter)

    failed = [m for m in llm.seen_messages[1] if m.role == "tool"][0]
    assert failed.is_error is True
    assert "上游炸了" in failed.content


async def test_工具成功时is_error为假():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()]),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    await runtime.run("x", emitter=emitter)

    ok = [m for m in llm.seen_messages[1] if m.role == "tool"][0]
    assert ok.is_error is False


# --------------------------------------------------------------------------
# 会话续写：run 接受 transcript，而不是每次从零构建
# --------------------------------------------------------------------------


async def test_传入历史时是续写而不是重建():
    """这是"多轮"能成立的最小条件：上一轮的消息必须还在，而且顺序不变。"""
    first_llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2)),
            LLMResponse(text="茅台 1252.57", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=first_llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]
    first = await runtime.run("茅台多少钱", emitter=emitter)

    second_llm = _FakeLLM([LLMResponse(text="它今天成交 25017 手", usage=Usage(1, 1, 2))])
    runtime2 = AgentRuntime(settings=_settings(), llm=second_llm, tools=ToolRegistry([]))  # type: ignore[arg-type]
    await runtime2.run("它今天成交多少", emitter=emitter, messages=first.messages)

    sent = second_llm.seen_messages[0]
    assert [m.role for m in sent][:2] == ["system", "user"]
    texts = [m.content for m in sent if m.role == "user"]
    assert texts[0] == "茅台多少钱"
    assert texts[-1] == "它今天成交多少"
    # 上一轮的工具结果也还在 —— 否则模型无从知道"它"是哪只
    assert any("1252.57" in (m.content or "") for m in sent)


async def test_历史里没有system时自动补上():
    llm = _FakeLLM([LLMResponse(text="好", usage=Usage(1, 1, 2))])
    emitter, _ = _emitter()
    runtime = AgentRuntime(
        settings=_settings(),  # type: ignore[arg-type]
        llm=llm,
        tools=ToolRegistry([]),
        system_prompt="你是测试用的系统提示",
    )

    await runtime.run("你好", emitter=emitter, messages=[Message(role="user", content="上一句")])

    sent = llm.seen_messages[0]
    assert sent[0].role == "system"
    assert sent[0].content == "你是测试用的系统提示"
    assert [m.content for m in sent if m.role == "user"] == ["上一句", "你好"]


async def test_system每次都用最新的那份():
    """prompt 迭代要立刻对老会话生效，所以会话里不该存 system。"""
    llm = _FakeLLM([LLMResponse(text="好", usage=Usage(1, 1, 2))])
    emitter, _ = _emitter()
    runtime = AgentRuntime(
        settings=_settings(),  # type: ignore[arg-type]
        llm=llm,
        tools=ToolRegistry([]),
        system_prompt="新版本提示",
    )

    stale = [Message(role="system", content="旧版本提示"), Message(role="user", content="上一句")]
    await runtime.run("你好", emitter=emitter, messages=stale)

    assert llm.seen_messages[0][0].content == "新版本提示"


async def test_runresult带回完整transcript():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2)),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]
    result = await runtime.run("x", emitter=emitter)

    assert [m.role for m in result.messages][0] == "system"
    assert result.messages[-1].role == "assistant"


# --------------------------------------------------------------------------
# 被中断的 transcript 修复
# --------------------------------------------------------------------------


async def test_以未执行的工具调用结尾的历史会被修复():
    """崩溃 / 取消后，transcript 尾部是"声明了工具调用但没有结果"。

    两个协议都不收这种结尾，所以续跑前必须补齐 —— 而且补的内容必须
    说明"结果未知"，不能猜成失败（工具可能已经把请求发出去了）。
    """
    interrupted = [
        Message(role="user", content="茅台多少钱"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="stock_quote", arguments='{"symbol":"600519"}')],
        ),
    ]
    llm = _FakeLLM([LLMResponse(text="我重新查一下", usage=Usage(1, 1, 2))])
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([]))  # type: ignore[arg-type]

    await runtime.run("继续", emitter=emitter, messages=interrupted)

    sent = llm.seen_messages[0]
    tool_messages = [m for m in sent if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"
    assert "结果未知" in tool_messages[0].content
    assert tool_messages[0].is_error is True

    run_start = [e for e in events if e.type is EventType.RUN_START][0]
    assert run_start.data["repaired_messages"] == 1
    assert run_start.data["resumed"] is True


async def test_已经完整的工具轮不会被补():
    complete = [
        Message(role="user", content="茅台多少钱"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="stock_quote", arguments="{}")],
        ),
        Message(role="tool", content="1252.57", tool_call_id="c1", name="stock_quote"),
        Message(role="assistant", content="1252.57 元"),
    ]
    llm = _FakeLLM([LLMResponse(text="好", usage=Usage(1, 1, 2))])
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([]))  # type: ignore[arg-type]

    await runtime.run("再问一句", emitter=emitter, messages=complete)

    sent = llm.seen_messages[0]
    assert len([m for m in sent if m.role == "tool"]) == 1  # 没有多出来
    run_start = [e for e in events if e.type is EventType.RUN_START][0]
    assert run_start.data["repaired_messages"] == 0


async def test_部分结果缺失时只补缺的那条():
    partial = [
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="c1", name="stock_quote", arguments='{"symbol":"600519"}'),
                ToolCall(id="c2", name="stock_quote", arguments='{"symbol":"000858"}'),
            ],
        ),
        Message(role="tool", content="茅台 1252.57", tool_call_id="c1", name="stock_quote"),
    ]
    llm = _FakeLLM([LLMResponse(text="好", usage=Usage(1, 1, 2))])
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([]))  # type: ignore[arg-type]

    await runtime.run("继续", emitter=emitter, messages=partial)

    sent = llm.seen_messages[0]
    ids = [m.tool_call_id for m in sent if m.role == "tool"]
    assert ids == ["c1", "c2"]  # c1 原样，c2 补上


# --------------------------------------------------------------------------
# 运行时熔断
# --------------------------------------------------------------------------


async def test_同参数重复调用被拦下且不会真的执行第二次():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2)),
            LLMResponse(tool_calls=[_tool_call(call_id="c2")], usage=Usage(1, 1, 2)),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    result = await runtime.run("茅台多少钱", emitter=emitter)

    assert _QuoteTool.calls == [{"symbol": "600519"}]  # 只执行了一次
    assert result.breaks == 1

    breaks = [e for e in events if e.type is EventType.CIRCUIT_BREAK]
    assert len(breaks) == 1
    assert breaks[0].data["reason"] == "duplicate_call"


async def test_熔断时仍然回灌一条tool消息():
    """assistant 声明了两个调用，就必须有两条结果 —— 少一条就是协议非法。"""
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[_tool_call()], usage=Usage(1, 1, 2)),
            LLMResponse(tool_calls=[_tool_call(call_id="c2")], usage=Usage(1, 1, 2)),
            LLMResponse(text="好了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, _ = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_QuoteTool()]))  # type: ignore[arg-type]

    await runtime.run("茅台多少钱", emitter=emitter)

    second_round = llm.seen_messages[1]
    tool_messages = [m for m in second_round if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"

    third_round = llm.seen_messages[2]
    tool_messages = [m for m in third_round if m.role == "tool"]
    assert len(tool_messages) == 2  # c1 的真结果 + c2 的熔断回灌
    assert tool_messages[1].tool_call_id == "c2"
    assert tool_messages[1].is_error is True
    assert "已经执行过了" in tool_messages[1].content


async def test_连续失败触发熔断():
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="broken", arguments="{}")]),
            LLMResponse(tool_calls=[ToolCall(id="c2", name="broken", arguments='{"x":1}')]),
            LLMResponse(tool_calls=[ToolCall(id="c3", name="broken", arguments='{"y":2}')]),
            LLMResponse(text="它坏了，我没辙", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, events = _emitter()
    runtime = AgentRuntime(settings=_settings(), llm=llm, tools=ToolRegistry([_BrokenTool()]))  # type: ignore[arg-type]

    result = await runtime.run("x", emitter=emitter)

    assert result.breaks == 1  # 第三次被拦
    assert result.text == "它坏了，我没辙"
    reason = [e for e in events if e.type is EventType.CIRCUIT_BREAK][0].data["reason"]
    assert reason == "consecutive_failures"
    # run_end 里也要能看见，否则"到底空转了几次"根本查不出来
    run_end = [e for e in events if e.type is EventType.RUN_END][0]
    assert run_end.data["breaks"] == 1


# --------------------------------------------------------------------------
# 大结果落盘
# --------------------------------------------------------------------------


class _HugeTool:
    spec = ToolSpec(
        name="huge",
        description="返回超长内容",
        parameters={"type": "object", "properties": {}},
    )

    async def run(self, args: dict) -> str:
        return "\n".join(f"2026-09-{i:02d},1252.57,{i}" for i in range(1, 400))


async def test_大结果落盘只把摘要送进上下文(tmp_path):
    from niulai.artifacts import ArtifactStore

    store = ArtifactStore(root=tmp_path / "artifacts")
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="huge", arguments="{}")]),
            LLMResponse(text="看完了", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, events = _emitter()
    runtime = AgentRuntime(
        settings=_settings(),  # type: ignore[arg-type]
        llm=llm,
        tools=ToolRegistry([_HugeTool()], artifact_store=store, inline_limit=200),
    )

    await runtime.run("x", emitter=emitter)

    tool_message = [m for m in llm.seen_messages[1] if m.role == "tool"][0]
    # 上下文里不该有 399 行原始数据
    assert len(tool_message.content) < 1000
    assert "已完整保存为文件" in tool_message.content
    assert "read_file" in tool_message.content
    # 而且必须明确告诉它预览不是全部 —— 否则它会当成完整数据下结论
    assert "不代表全部数据" in tool_message.content

    artifacts = [e for e in events if e.type is EventType.ARTIFACT]
    assert len(artifacts) == 1
    assert artifacts[0].data["content_chars"] > 5000

    # 文件真的落下来了，而且能读回
    ref = artifacts[0].data["path"]
    text, total, _ = store.read(ref)
    assert total == 399
    assert text.startswith("2026-09-01")


async def test_落盘结果的tool_result事件带artifact路径(tmp_path):
    from niulai.artifacts import ArtifactStore

    store = ArtifactStore(root=tmp_path / "artifacts")
    llm = _FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="huge", arguments="{}")]),
            LLMResponse(text="好", usage=Usage(1, 1, 2)),
        ]
    )
    emitter, events = _emitter()
    runtime = AgentRuntime(
        settings=_settings(),  # type: ignore[arg-type]
        llm=llm,
        tools=ToolRegistry([_HugeTool()], artifact_store=store, inline_limit=200),
    )

    await runtime.run("x", emitter=emitter)

    tool_result = [e for e in events if e.type is EventType.TOOL_RESULT][0]
    assert tool_result.data["artifact"]
    # 两个数字都要有：进上下文的（摘要）和原始的。少了后者，
    # 看 trace 的人会以为这个工具只返回了几百字符。
    assert tool_result.data["content_chars"] < 1000
    assert tool_result.data["original_chars"] > 5000
