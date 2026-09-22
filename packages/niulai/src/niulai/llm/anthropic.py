"""Anthropic Messages API 客户端。

和 OpenAI 兼容协议比，它有 **六处结构性的不同**，全都必须在这一层消化掉，
一丁点都不能漏到内核去：

1. **system 是顶层参数**，不是 `role="system"` 的消息；
2. **工具结果要放进 user 消息的 content block**，而且同一轮的多个
   tool_result 必须合并进**同一条** user 消息（分开发会被拒）；
3. **内容是一组带类型的 block**（text / tool_use / thinking / …），
   不是一根字符串；
4. **流式事件带类型**（message_start / content_block_delta / message_delta…），
   不像 OpenAI 那样只有一个 delta 形状；
5. **`max_tokens` 必填**，没有默认值；
6. **thinking 块必须原样送回**。Opus 5 / Sonnet 5 默认开思考，把 thinking
   块删掉再发回去会被 400 拒绝 —— 所以它走 `raw_blocks` 原样透传。

另外两条当前模型上的硬约束（不遵守直接 400，不是警告）：

- **采样参数已废弃**：temperature / top_p / top_k 在 Opus 4.7+ 与 Sonnet 5 上
  传非默认值会被拒。所以我们一个都不传。
- **assistant 预填充被移除**：不要试图用末尾的 assistant 消息引导格式。

代价说明：`max_tokens` 对 Opus 5 / Sonnet 5 是**包含思考**的预算。默认给
8192 是奔着"别在长报告里被截断"去的，改小要留意这一点。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from ..errors import FatalLLMError, RetryableLLMError, classify_status
from .base import TextHandler, endpoint, stream_with_retry
from .sse import parse_sse_line
from .types import LLMResponse, Message, ToolCall, ToolSpec, Usage

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

MESSAGES_PATH = "/messages"
ANTHROPIC_VERSION = "2023-06-01"

# 流式过程中服务端推过来的错误类型 → 是否值得重试
RETRYABLE_STREAM_ERRORS = frozenset(
    {"overloaded_error", "rate_limit_error", "api_error", "timeout_error"}
)


# --------------------------------------------------------------------------
# 线格式转换（Anthropic 形状）
# --------------------------------------------------------------------------


def load_input(arguments: str) -> dict[str, Any]:
    """`ToolCall.arguments` 是原始 JSON 字符串，Anthropic 要的是对象。

    拼不出来时退回空对象：宁可让工具收到空参数（然后由工具层报"缺少必填参数"），
    也不要在这里抛异常把整轮打断。
    """
    if not arguments or not arguments.strip():
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def tool_spec_to_wire(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        # 注意不是 OpenAI 的 function.parameters
        "input_schema": spec.parameters,
    }


def assistant_blocks(message: Message) -> list[dict[str, Any]]:
    """assistant 轮的内容块。

    有 `raw_blocks` 就**原样送回**。这是唯一安全的做法：Anthropic 要求
    thinking / redacted_thinking 块与收到时逐字节一致，改动（哪怕只是
    删掉一个"空文本"的思考块）都会被拒。
    """
    if message.raw_blocks:
        return message.raw_blocks

    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls or []:
        blocks.append(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": load_input(call.arguments),
            }
        )
    return blocks or [{"type": "text", "text": message.content or ""}]


def tool_result_block(message: Message) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id or "",
        "content": message.content or "",
    }
    if message.is_error:
        # 失败的工具不要丢弃，标记 is_error 送回去 —— 模型对结构化失败
        # 的纠错能力明显强于读一段自然语言抱怨
        block["is_error"] = True
    return block


def append_blocks(out: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """追加内容块，遇到相同 role 就并入上一条消息。

    这条规则顺带解决了两个必须解决的问题：

    - 同一轮的多个 tool_result 必须合并成**一条** user 消息；
    - 收口轮追加的那句"请立刻回答"，会紧跟在工具结果后面。不合并就会出现
      连续两条 user 消息 —— 同样是非法请求。
    """
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def messages_to_wire(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """把 IR 消息转成 Anthropic 需要的 `(system, messages)`。"""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            if message.content:
                system_parts.append(message.content)
        elif message.role == "assistant":
            append_blocks(out, "assistant", assistant_blocks(message))
        elif message.role == "tool":
            append_blocks(out, "user", [tool_result_block(message)])
        else:
            append_blocks(out, "user", [{"type": "text", "text": message.content or ""}])

    return "\n\n".join(system_parts) or None, out


def system_blocks(system: str, *, cache: bool) -> list[dict[str, Any]]:
    """system 用 block 形式传，好挂缓存断点。

    缓存前缀的拼接顺序是 `tools → system → messages`，所以断点打在 system
    末尾，一次就把「工具表 + 系统提示」这段最稳定的前缀覆盖了。这俩在长会话里
    是纯重复内容，命中一次省 90% 的输入价。

    低于最小可缓存长度（Sonnet 5 是 1024 token）时服务端**静默跳过**，
    不报错 —— 所以默认一直开着是安全的。
    """
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def build_payload(
    settings: Settings, messages: Sequence[Message], tools: Sequence[ToolSpec]
) -> dict[str, Any]:
    system, wire_messages = messages_to_wire(messages)

    payload: dict[str, Any] = {
        "model": settings.model,
        "max_tokens": settings.max_tokens,  # 必填项
        "messages": wire_messages,
        "stream": True,
        # 刻意不传 temperature / top_p / top_k：当前模型上非默认值直接 400
    }
    if system:
        payload["system"] = system_blocks(system, cache=settings.prompt_cache)
    if tools:
        payload["tools"] = [tool_spec_to_wire(t) for t in tools]
    return payload


# --------------------------------------------------------------------------
# 流式事件累积
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    type: str
    text: str = ""
    partial_json: str = ""
    thinking: str = ""
    signature: str = ""
    data: str = ""  # redacted_thinking 的密文
    block_id: str = ""
    name: str = ""


@dataclass(slots=True)
class StreamState:
    """把 Anthropic 的事件流累积成 `LLMResponse`。

    单独抽出来是因为这是全文件最容易写错的一段，也是最值得测的一段：
    工具参数走 `input_json_delta` 分片、usage 分两次给、thinking 要留底。
    它不碰网络 —— 测试可以直接喂事件字典。
    """

    blocks: dict[int, _Block] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None

    def feed(self, event: dict[str, Any]) -> str | None:
        """消费一个事件。若产生新的正文增量就返回它，否则返回 None。"""
        kind = event.get("type")

        if kind == "message_start":
            message = event.get("message") or {}
            self.usage = parse_usage(message.get("usage") or {})
            return None

        if kind == "content_block_start":
            return self._start(int(event.get("index") or 0), event.get("content_block") or {})

        if kind == "content_block_delta":
            return self._delta(int(event.get("index") or 0), event.get("delta") or {})

        if kind == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            self._merge_output_usage(event.get("usage") or {})
            return None

        if kind == "error":
            raise_stream_error(event.get("error") or {})

        return None  # message_stop / content_block_stop / ping 之类不用管

    def _start(self, index: int, raw: dict[str, Any]) -> str | None:
        block_type = str(raw.get("type") or "")
        block = _Block(type=block_type)

        if block_type == "text":
            block.text = raw.get("text") or ""
        elif block_type == "tool_use":
            block.block_id = raw.get("id") or ""
            block.name = raw.get("name") or ""
            # start 里的 input 一般是空对象，真参数全在 delta 里
            initial = raw.get("input")
            if initial:
                block.partial_json = json.dumps(initial, ensure_ascii=False)
        elif block_type == "thinking":
            block.thinking = raw.get("thinking") or ""
        elif block_type == "redacted_thinking":
            block.data = raw.get("data") or ""

        self.blocks[index] = block
        return block.text if block_type == "text" and block.text else None

    def _delta(self, index: int, delta: dict[str, Any]) -> str | None:
        block = self.blocks.get(index)
        if block is None:
            return None  # 没收到 start 的块直接忽略，不为一个意外事件打断整轮

        delta_type = delta.get("type")
        if delta_type == "text_delta":
            piece = delta.get("text") or ""
            block.text += piece
            return piece or None
        if delta_type == "input_json_delta":
            block.partial_json += delta.get("partial_json") or ""
        elif delta_type == "thinking_delta":
            block.thinking += delta.get("thinking") or ""
        elif delta_type == "signature_delta":
            block.signature += delta.get("signature") or ""
        return None

    def _merge_output_usage(self, raw: dict[str, Any]) -> None:
        """message_delta 里的 output_tokens 是这一轮的**最终值**，直接覆盖。"""
        if "output_tokens" not in raw:
            return
        completion = int(raw.get("output_tokens") or 0)
        self.usage.completion_tokens = completion
        self.usage.total_tokens = self.usage.prompt_tokens + completion

    def to_response(self) -> LLMResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict[str, Any]] = []

        for index in sorted(self.blocks):
            block = self.blocks[index]

            if block.type == "text":
                if block.text:
                    text_parts.append(block.text)
                    raw_blocks.append({"type": "text", "text": block.text})

            elif block.type == "tool_use":
                arguments = block.partial_json or "{}"
                tool_calls.append(
                    ToolCall(
                        id=block.block_id or f"call_{index}",
                        name=block.name,
                        arguments=arguments,
                        index=index,
                    )
                )
                raw_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.block_id,
                        "name": block.name,
                        "input": load_input(arguments),
                    }
                )

            elif block.type == "thinking":
                # 空文本的思考块也要原样留着：Opus 5 / Sonnet 5 默认
                # display="omitted"，正文就是空串，但**签名不能丢**。
                raw_blocks.append(
                    {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
                )

            elif block.type == "redacted_thinking":
                raw_blocks.append({"type": "redacted_thinking", "data": block.data})

        return LLMResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            usage=self.usage,
            finish_reason=self.stop_reason,
            raw_blocks=raw_blocks or None,
        )


def parse_usage(raw: dict[str, Any]) -> Usage:
    """Anthropic 用量字段 → 公共 Usage。

    坑：**`input_tokens` 不含缓存命中的部分**。真实输入量是
    `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`。
    漏掉后两项，token 预算会严重低估 —— 长会话里能差好几倍。
    """
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    cache_write = int(raw.get("cache_creation_input_tokens") or 0)
    prompt = int(raw.get("input_tokens") or 0) + cache_read + cache_write
    completion = int(raw.get("output_tokens") or 0)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def raise_stream_error(error: dict[str, Any]) -> None:
    """流式过程中下发的错误事件。

    这类错误是从正常 200 的流里推出来的，所以拿不到 HTTP 状态码，
    只能自己按 `type` 分类 —— 否则"服务过载"这种临时错误会被当成致命错误，
    用户得手动重跑。
    """
    kind = str(error.get("type") or "error")
    detail = str(error.get("message") or "")
    if kind in RETRYABLE_STREAM_ERRORS:
        raise RetryableLLMError(f"{kind}：{detail}")
    raise FatalLLMError(f"{kind}：{detail}")


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class AnthropicClient:
    """只依赖 httpx 的 Messages API 客户端。

    没装官方 SDK 有两个理由：一是少一个重依赖（桌面打包要小）；二是
    `input_json_delta` 的拼接、thinking 块的留底这些恰恰是"必须亲手踩一遍"
    的地方，交给 SDK 就学不到了。
    """

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._url = endpoint(settings.base_url, MESSAGES_PATH)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AnthropicClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def stream_chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        on_text: TextHandler | None = None,
    ) -> LLMResponse:
        return await stream_with_retry(
            max_retries=self._settings.max_retries,
            attempt_runner=lambda handler: self._stream_once(messages, tools, on_text=handler),
            on_text=on_text,
        )

    async def _stream_once(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        *,
        on_text: TextHandler | None,
    ) -> LLMResponse:
        payload = build_payload(self._settings, messages, tools)
        headers = {
            "x-api-key": self._settings.api_key,  # 注意不是 Authorization: Bearer
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

        state = StreamState()

        try:
            async with self._client.stream(
                "POST", self._url, json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    error_cls = classify_status(resp.status_code)
                    raise error_cls(f"HTTP {resp.status_code}：{body}", status=resp.status_code)

                async for line in resp.aiter_lines():
                    event = parse_sse_line(line)
                    if event is None:
                        continue
                    piece = state.feed(event)
                    if piece and on_text is not None:
                        on_text(piece)

        except httpx.TimeoutException as exc:
            raise RetryableLLMError(f"请求超时：{exc!r}") from exc
        except httpx.TransportError as exc:
            raise RetryableLLMError(f"网络错误：{exc!r}") from exc

        return state.to_response()
