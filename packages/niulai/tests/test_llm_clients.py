"""用假 transport 走完整 HTTP 路径的测试。

上面的 `test_llm_protocols.py` 测的是纯转换逻辑；这一组补上它测不到的部分：
URL、请求头、请求体里的协议细节。两套协议最容易出的错恰好都在这儿 ——
认证头写错（Bearer vs x-api-key）、system 忘了提到顶层、把 OpenAI 的
`function.parameters` 发给 Anthropic。

用 `httpx.MockTransport` 顶掉网络，所以 CI 里照样没有外部依赖。
"""

from __future__ import annotations

import json

import httpx
import pytest

from niulai.config import Settings
from niulai.llm import AnthropicClient, Message, OpenAICompatClient, ToolCall, ToolSpec

SPEC = ToolSpec(
    name="stock_quote",
    description="查询行情",
    parameters={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
)

ANTHROPIC_SSE = "\n".join(
    [
        "event: message_start",
        'data: {"type":"message_start","message":{"usage":'
        '{"input_tokens":100,"cache_read_input_tokens":900,"cache_creation_input_tokens":0}}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"茅台现价 "}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":1,"content_block":'
        '{"type":"tool_use","id":"toolu_1","name":"stock_quote","input":{}}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":1,"delta":'
        '{"type":"input_json_delta","partial_json":"{\\"symbol\\""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":1,"delta":'
        '{"type":"input_json_delta","partial_json":":\\"600519\\"}"}}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":42}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "",
    ]
)

OPENAI_SSE = "\n".join(
    [
        'data: {"choices":[{"delta":{"role":"assistant","content":"茅台"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"现价 "}}]}',
        "",
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"stock_quote","arguments":"{\\"sym"}}]}}]}',
        "",
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        '{"arguments":"bol\\":\\"600519\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "",
        'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":42,"total_tokens":142}}',
        "",
        "data: [DONE]",
        "",
    ]
)

CONVERSATION = [
    Message(role="system", content="你是 A 股助手"),
    Message(role="user", content="茅台多少钱"),
    Message(
        role="assistant",
        content=None,
        tool_calls=[ToolCall(id="t1", name="stock_quote", arguments='{"symbol":"600519"}')],
    ),
    Message(role="tool", content="已停牌", tool_call_id="t1", name="stock_quote", is_error=True),
]


class _Recorder:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.url = ""
        self.headers: dict[str, str] = {}
        self.body: dict = {}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.url = str(request.url)
            self.headers = {k.lower(): v for k, v in request.headers.items()}
            self.body = json.loads(request.content)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=self.raw.encode()
            )

        return httpx.MockTransport(handler)


def _anthropic_settings(**overrides) -> Settings:
    base = {
        "base_url": "https://api.anthropic.com/v1",
        "api_key": "sk-ant-fake",
        "model": "claude-sonnet-5",
        "protocol": "anthropic",
        "max_tokens": 4096,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_anthropic走messages端点且认证头正确():
    recorder = _Recorder(ANTHROPIC_SSE)
    async with httpx.AsyncClient(transport=recorder.transport()) as http:
        response = await AnthropicClient(_anthropic_settings(), client=http).stream_chat(
            CONVERSATION, [SPEC]
        )

    assert recorder.url == "https://api.anthropic.com/v1/messages"
    assert recorder.headers["x-api-key"] == "sk-ant-fake"
    assert recorder.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in recorder.headers  # 不是 Bearer

    body = recorder.body
    assert body["max_tokens"] == 4096
    # system 必须提出来当顶层参数
    assert body["system"][0]["text"] == "你是 A 股助手"
    assert all(m["role"] != "system" for m in body["messages"])
    # 工具schema用input_schema
    assert "input_schema" in body["tools"][0]
    # 采样参数一个都不能传
    assert not {"temperature", "top_p", "top_k"} & set(body)

    assert response.text == "茅台现价 "
    assert response.tool_calls[0].arguments == '{"symbol":"600519"}'
    assert response.usage.cache_read_tokens == 900
    assert response.raw_blocks is not None


async def test_anthropic把工具结果和失败标记合并成一条user消息():
    recorder = _Recorder(ANTHROPIC_SSE)
    async with httpx.AsyncClient(transport=recorder.transport()) as http:
        await AnthropicClient(_anthropic_settings(), client=http).stream_chat(CONVERSATION, [SPEC])

    messages = recorder.body["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]

    last = messages[-1]["content"]
    assert last[0]["tool_use_id"] == "t1"
    assert last[0]["is_error"] is True
    # tool_result 属于 user 轮，直接给字符串会被拒
    assert isinstance(last, list)


async def test_openai走chat_completions且用bearer():
    recorder = _Recorder(OPENAI_SSE)
    settings = Settings(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-fake",
        model="deepseek-chat",
        protocol="openai",
    )
    async with httpx.AsyncClient(transport=recorder.transport()) as http:
        response = await OpenAICompatClient(settings, client=http).stream_chat(CONVERSATION, [SPEC])

    assert recorder.url == "https://api.deepseek.com/v1/chat/completions"
    assert recorder.headers["authorization"] == "Bearer sk-fake"

    body = recorder.body
    assert body["stream_options"] == {"include_usage": True}
    assert body["tool_choice"] == "auto"
    # system 在 OpenAI 这边是普通消息，不能提到顶层
    assert body["messages"][0] == {"role": "system", "content": "你是 A 股助手"}
    assert "parameters" in body["tools"][0]["function"]

    assert response.text == "茅台现价 "
    assert response.tool_calls[0].arguments == '{"symbol":"600519"}'
    assert response.usage.total_tokens == 142
    assert response.raw_blocks is None  # 这个协议没有需要回传的私有块


async def test_用户把完整endpoint填进base_url也不会拼重复():
    """真实用户会直接把 https://api.deepseek.com/v1/chat/completions 粘进去。"""
    recorder = _Recorder(OPENAI_SSE)
    settings = Settings(
        base_url="https://api.deepseek.com/v1/chat/completions",
        api_key="sk-fake",
        model="deepseek-chat",
        protocol="openai",
    )
    async with httpx.AsyncClient(transport=recorder.transport()) as http:
        await OpenAICompatClient(settings, client=http).stream_chat(
            [Message(role="user", content="hi")]
        )

    assert recorder.url == "https://api.deepseek.com/v1/chat/completions"


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
async def test_http错误码按可重试与否分类(status):
    from niulai.errors import FatalLLMError, RetryableLLMError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b'{"error":"boom"}')

    settings = _anthropic_settings(max_retries=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AnthropicClient(settings, client=http)
        expected = RetryableLLMError if status in (429, 500, 503) else FatalLLMError
        with pytest.raises(expected):
            await client.stream_chat([Message(role="user", content="hi")])
