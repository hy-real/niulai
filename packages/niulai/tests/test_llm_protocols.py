"""两套协议的转换与流式解析测试。

全部**不碰网络** —— 转换和累积都是纯函数/纯状态机，直接喂字典。
这样 CI 里秒级跑完，也不会因为某家接口变动而红。
"""

from __future__ import annotations

import pytest

from niulai.config import Settings, infer_protocol
from niulai.errors import FatalLLMError, RetryableLLMError
from niulai.llm import AnthropicClient, LLMClient, OpenAICompatClient, build_client
from niulai.llm.anthropic import (
    StreamState,
    assistant_blocks,
    build_payload as anthropic_payload,
    messages_to_wire,
    parse_usage as anthropic_usage,
)
from niulai.llm.openai_compat import (
    build_payload as openai_payload,
    message_to_wire,
    parse_usage as openai_usage,
    strip_leaked_chunk_json,
    tool_spec_to_wire,
)
from niulai.llm.types import Message, ToolCall, ToolSpec, Usage


def _settings(protocol: str = "anthropic", **overrides) -> Settings:
    base = {
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "k",
        "model": "claude-sonnet-5",
        "protocol": protocol,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


SPEC = ToolSpec(
    name="stock_quote",
    description="查询行情",
    parameters={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
)


# --------------------------------------------------------------------------
# Anthropic 消息转换：三处跨消息的约束
# --------------------------------------------------------------------------


def test_system_必须提到顶层而不是留在消息列表里():
    system, wire = messages_to_wire(
        [
            Message(role="system", content="你是助手"),
            Message(role="system", content="说中文"),
            Message(role="user", content="茅台多少钱"),
        ]
    )

    assert system == "你是助手\n\n说中文"
    assert wire == [{"role": "user", "content": [{"type": "text", "text": "茅台多少钱"}]}]


def test_同一轮的多个工具结果必须合并进一条user消息():
    _, wire = messages_to_wire(
        [
            Message(role="user", content="查两家的行情"),
            Message(
                role="assistant",
                content="好",
                tool_calls=[
                    ToolCall(id="t1", name="stock_quote", arguments='{"symbol":"600519"}'),
                    ToolCall(id="t2", name="stock_quote", arguments='{"symbol":"000858"}'),
                ],
            ),
            Message(role="tool", content="茅台 1252.57", tool_call_id="t1", name="stock_quote"),
            Message(role="tool", content="五粮液 148.2", tool_call_id="t2", name="stock_quote"),
        ]
    )

    # 两条 tool 消息 → 一条 user 消息里的两个 tool_result 块
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    results = wire[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"]
    assert all(b["type"] == "tool_result" for b in results)

    # 工具调用也要变成 tool_use 块
    tool_uses = [b for b in wire[1]["content"] if b["type"] == "tool_use"]
    assert [b["input"] for b in tool_uses] == [{"symbol": "600519"}, {"symbol": "000858"}]


def test_工具结果后面的user文本要并进同一条消息而不是新开一条():
    """收口轮那句"请立刻回答"就紧跟工具结果。不合并就会出现连续两条 user，
    而 Anthropic 要求 user / assistant 交替。"""
    _, wire = messages_to_wire(
        [
            Message(role="user", content="查行情"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="t1", name="x", arguments="{}")]),
            Message(role="tool", content="结果", tool_call_id="t1", name="x"),
            Message(role="user", content="请立刻回答"),
        ]
    )

    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    blocks = wire[2]["content"]
    assert blocks[0]["type"] == "tool_result"
    assert blocks[1] == {"type": "text", "text": "请立刻回答"}


def test_工具失败要带is_error标记():
    _, wire = messages_to_wire(
        [Message(role="tool", content="参数不是合法 JSON", tool_call_id="t1", is_error=True)]
    )
    assert wire[0]["content"][0]["is_error"] is True


def test_raw_blocks原样透传不做任何加工():
    """thinking / redacted_thinking 必须逐字节一致，改一个字符就被 400。"""
    raw = [
        {"type": "thinking", "thinking": "", "signature": "sig=="},
        {"type": "text", "text": "答案"},
    ]
    message = Message(role="assistant", content="答案", raw_blocks=raw)

    assert assistant_blocks(message) is raw


def test_anthropic的input_tokens不含缓存命中():
    usage = anthropic_usage(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 80,
        }
    )

    assert usage.prompt_tokens == 1080  # 100 + 900 + 80
    assert usage.cache_read_tokens == 900
    assert usage.cache_write_tokens == 80
    assert usage.total_tokens == 1100


def test_openai的缓存命中能被解析():
    """OpenAI 形状：缓存量嵌套在 prompt_tokens_details.cached_tokens 里。"""
    usage = openai_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "total_tokens": 1020,
            "prompt_tokens_details": {"cached_tokens": 800, "audio_tokens": 0},
        }
    )
    assert usage.cache_read_tokens == 800
    assert usage.prompt_tokens == 1000  # OpenAI 的 prompt_tokens 本身就含缓存，不再累加


def test_deepseek的缓存命中能被解析():
    """DeepSeek 形状：字段是平铺的 prompt_cache_hit_tokens。"""
    usage = openai_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 20,
            "total_tokens": 1020,
            "prompt_cache_hit_tokens": 900,
            "prompt_cache_miss_tokens": 100,
        }
    )
    assert usage.cache_read_tokens == 900


def test_openai用量没有缓存字段时不产生噪音():
    """老接口/小请求没有缓存字段，cache_read_tokens 保持 0，不写进 trace。"""
    usage = openai_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert usage.cache_read_tokens == 0
    assert "cache_read_tokens" not in usage.to_dict()


# --------------------------------------------------------------------------
# Anthropic payload
# --------------------------------------------------------------------------


def test_payload必须有max_tokens且不传采样参数():
    payload = anthropic_payload(
        _settings(max_tokens=4096), [Message(role="user", content="hi")], [SPEC]
    )

    assert payload["max_tokens"] == 4096
    # temperature / top_p / top_k 在 Opus 4.7+ 与 Sonnet 5 上非默认值直接 400
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "top_k" not in payload
    assert payload["tools"][0]["input_schema"] == SPEC.parameters
    assert "function" not in payload["tools"][0]


def test_payload的system挂缓存断点():
    messages = [Message(role="system", content="S"), Message(role="user", content="u")]

    with_cache = anthropic_payload(_settings(prompt_cache=True), messages, [])
    without = anthropic_payload(_settings(prompt_cache=False), messages, [])

    assert with_cache["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in without["system"][0]
    # 除了缓存标记，两种情况的 payload 必须完全一致
    assert with_cache["messages"] == without["messages"]


# --------------------------------------------------------------------------
# Anthropic 流式累积
# --------------------------------------------------------------------------


def _feed(state: StreamState, events: list[dict]) -> list[str]:
    pieces: list[str] = []
    for event in events:
        piece = state.feed(event)
        if piece:
            pieces.append(piece)
    return pieces


def test_流式解析文本与按分片拼接的工具参数():
    state = StreamState()
    pieces = _feed(
        state,
        [
            {
                "type": "message_start",
                "message": {
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 900,
                    }
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "我来查"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "一下"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "stock_quote", "input": {}},
            },
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"sym'}},
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": 'bol": "600519"}'},
            },
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}},
            {"type": "message_stop"},
        ],
    )

    assert pieces == ["我来查", "一下"]

    response = state.to_response()
    assert response.text == "我来查一下"
    assert response.finish_reason == "tool_use"
    assert response.wants_tools

    call = response.tool_calls[0]
    assert call.id == "toolu_1"
    assert call.name == "stock_quote"
    assert call.arguments == '{"symbol": "600519"}'

    # message_start 给输入、message_delta 给最终输出
    assert response.usage.prompt_tokens == 1000
    assert response.usage.completion_tokens == 42
    assert response.usage.total_tokens == 1042

    assert response.raw_blocks is not None
    assert response.raw_blocks[0] == {"type": "text", "text": "我来查一下"}
    assert response.raw_blocks[1]["input"] == {"symbol": "600519"}


def test_思考块即使正文为空也必须留底():
    """Opus 5 / Sonnet 5 默认 display="omitted"，思考正文就是空串，
    但签名不能丢 —— 丢了下一轮就被拒。"""
    state = StreamState()
    _feed(
        state,
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            },
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
        ],
    )

    response = state.to_response()
    assert response.text == ""  # 思考不算正文，不流给用户
    assert response.raw_blocks[0] == {"type": "thinking", "thinking": "", "signature": "sig=="}
    # 无参数的工具，arguments 要归一成 "{}" 而不是空串
    assert response.tool_calls[0].arguments == "{}"


def test_红acted思考块也要原样保留():
    state = StreamState()
    _feed(
        state,
        [
            {"type": "message_start", "message": {"usage": {}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "redacted_thinking", "data": "ENCRYPTED"},
            },
        ],
    )

    assert {"type": "redacted_thinking", "data": "ENCRYPTED"} in state.to_response().raw_blocks


def test_没收到start的delta不会打断整轮():
    state = StreamState()
    assert state.feed({"type": "content_block_delta", "index": 9, "delta": {"type": "text_delta", "text": "x"}}) is None
    assert state.to_response().text == ""


def test_流式错误按类型分为可重试与终止():
    state = StreamState()
    with pytest.raises(RetryableLLMError):
        state.feed({"type": "error", "error": {"type": "overloaded_error", "message": "过载"}})

    with pytest.raises(FatalLLMError):
        state.feed({"type": "error", "error": {"type": "invalid_request_error", "message": "参数错"}})


# --------------------------------------------------------------------------
# OpenAI 兼容协议：序列化搬出 types 之后要保证形状不变
# --------------------------------------------------------------------------


def test_openai的消息形状保持原样():
    wire = message_to_wire(
        Message(
            role="assistant",
            content=None,
            tool_calls=[ToolCall(id="c1", name="stock_quote", arguments='{"symbol":"600519"}')],
        )
    )

    assert wire["role"] == "assistant"
    assert wire["content"] == ""  # 只带 tool_calls 时也要给 content 字段
    assert wire["tool_calls"][0]["function"]["name"] == "stock_quote"

    tool_wire = message_to_wire(Message(role="tool", content="ok", tool_call_id="c1", name="stock_quote"))
    assert tool_wire["tool_call_id"] == "c1"

    spec_wire = tool_spec_to_wire(SPEC)
    # OpenAI 用 function.parameters，Anthropic 用顶层 input_schema —— 别串了
    assert spec_wire["function"]["parameters"] == SPEC.parameters


def test_openai_payload请求包含用量():
    payload = openai_payload(_settings("openai"), [Message(role="user", content="hi")], [SPEC])
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tool_choice"] == "auto"
    assert "max_tokens" not in payload  # 交给服务端默认


def test_gml泄漏的流式chunk_json会被剔除():
    leaked = (
        "好的，我将查询花园生物（300401）的财务数据。请稍等。"
        '{"index":0,"finish_reason":"tool_calls","delta":{"role":"assistant","content":null}}'
    )

    assert strip_leaked_chunk_json(leaked) == "好的，我将查询花园生物（300401）的财务数据。请稍等。"
    # 用户让模型正常输出 JSON 时不能被误删
    normal = '结果是 {"index": 0, "name": "300401"}'
    assert strip_leaked_chunk_json(normal) == normal
    assert strip_leaked_chunk_json(None) is None


# --------------------------------------------------------------------------
# 装配与配置
# --------------------------------------------------------------------------


def test_build_client按协议选实现():
    assert isinstance(build_client(_settings("anthropic")), AnthropicClient)
    assert isinstance(build_client(_settings("openai")), OpenAICompatClient)
    # 内核只认 Protocol，所以两种实现都满足
    assert isinstance(build_client(_settings("openai")), LLMClient)


@pytest.mark.parametrize(
    ("explicit", "base_url", "expected"),
    [
        ("anthropic", None, "anthropic"),
        ("openai", "https://api.anthropic.com/v1", "openai"),  # 显式优先
        (None, "https://api.anthropic.com/v1", "anthropic"),
        (None, "https://api.deepseek.com/v1", "openai"),
        (None, None, "openai"),
    ],
)
def test_协议推断(explicit, base_url, expected, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NIULAI_API_KEY", raising=False)

    assert infer_protocol(explicit, base_url) == expected


def test_只给了anthropic密钥时自动切到anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("NIULAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert infer_protocol(None, None) == "anthropic"

    settings = Settings.from_env()
    assert settings.protocol == "anthropic"
    assert settings.api_key == "sk-ant-x"
    assert settings.base_url == "https://api.anthropic.com/v1"
    assert settings.model == "claude-sonnet-5"


def test_命令行指定协议时接入点和模型一起跟着换(monkeypatch):
    """否则会出现"用 anthropic 协议去打 DeepSeek 地址"这种必错组合。"""
    for name in ("NIULAI_BASE_URL", "NIULAI_MODEL", "NIULAI_PROTOCOL", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")

    settings = Settings.from_env(protocol="anthropic")
    assert settings.protocol == "anthropic"
    assert settings.base_url == "https://api.anthropic.com/v1"
    assert settings.model == "claude-sonnet-5"

    # 显式设了接入点就尊重用户，不强改
    monkeypatch.setenv("NIULAI_BASE_URL", "https://proxy.example.com/anthropic/v1")
    assert Settings.from_env().protocol == "anthropic"


def test_usage合并时缓存计数也要累加():
    a = Usage(10, 5, 15, cache_read_tokens=100, cache_write_tokens=7)
    b = Usage(1, 1, 2, cache_read_tokens=900)

    merged = a.merged(b)
    assert merged.cache_read_tokens == 1000
    assert merged.cache_write_tokens == 7
    assert merged.total_tokens == 17
    # to_dict 只在真的用到缓存时才带这两个字段，避免 trace 里全是 0
    assert Usage(1, 1, 2).to_dict() == {
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
    }
