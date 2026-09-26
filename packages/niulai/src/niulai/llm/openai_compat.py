"""OpenAI 兼容协议的流式客户端。

只依赖 httpx，不装各家 SDK —— 因为 DeepSeek / 通义 / 混元 / Kimi / OpenAI
都实现了同一套 `/chat/completions`，一个客户端就够。

裸 HTTP 还带来两个好处：token 用量和错误码都看得见，
不会像框架那样把 token 记账藏在抽象后面。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from ..config import Settings
from ..errors import RetryableLLMError, classify_status
from ..logs import get_logger
from .base import TextHandler, endpoint, stream_with_retry
from .sse import parse_sse_line
from .types import LLMResponse, Message, ToolCall, ToolSpec, Usage

_log = get_logger("llm.openai")

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

CHAT_COMPLETIONS_PATH = "/chat/completions"


# --------------------------------------------------------------------------
# 线格式转换（OpenAI 形状）
# --------------------------------------------------------------------------


def tool_call_to_wire(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def message_to_wire(message: Message) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        wire["content"] = message.content
    elif message.role == "assistant" and message.tool_calls:
        # 部分服务端要求 assistant 消息即使只带 tool_calls 也必须给 content 字段
        wire["content"] = ""
    if message.tool_calls:
        wire["tool_calls"] = [tool_call_to_wire(tc) for tc in message.tool_calls]
    if message.tool_call_id:
        wire["tool_call_id"] = message.tool_call_id
    if message.name:
        wire["name"] = message.name
    return wire


def tool_spec_to_wire(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def build_payload(
    settings: Settings, messages: Sequence[Message], tools: Sequence[ToolSpec]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": [message_to_wire(m) for m in messages],
        "stream": True,
        # 不加这个，大部分服务端在流式模式下不返回 usage ——
        # 我们就没法统计花费，也做不了 token 预算。
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = [tool_spec_to_wire(t) for t in tools]
        payload["tool_choice"] = "auto"
    return payload


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class OpenAICompatClient:
    """一个最小的、流式的、会重试的 chat completions 客户端。"""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._url = endpoint(settings.base_url, CHAT_COMPLETIONS_PATH)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatClient:
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
        """流式调用，返回聚合后的结果。

        重试策略：**只在还没吐出任何文本时重试。**
        一旦已经有内容流给用户，再重试就会重复输出 —— 那种情况不如直接失败。
        """
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
            "Authorization": f"Bearer {self._settings.api_key}",  # 永远不进日志
            "Content-Type": "application/json",
        }
        _log.debug(
            "POST %s model=%s messages=%d tools=%d",
            self._url,
            self._settings.model,
            len(messages),
            len(tools),
        )

        text_parts: list[str] = []
        tool_slots: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason: str | None = None

        try:
            async with self._client.stream(
                "POST", self._url, json=payload, headers=headers
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:500]
                    _log.warning("LLM HTTP %d：%s", resp.status_code, body)
                    error_cls = classify_status(resp.status_code)
                    raise error_cls(f"HTTP {resp.status_code}：{body}", status=resp.status_code)

                async for line in resp.aiter_lines():
                    chunk = parse_sse_line(line)
                    if chunk is None:
                        continue

                    if chunk.get("usage"):
                        usage = parse_usage(chunk["usage"])

                    for choice in chunk.get("choices") or []:
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta") or {}

                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                            if on_text is not None:
                                on_text(piece)

                        for raw_call in delta.get("tool_calls") or []:
                            accumulate_tool_call(tool_slots, raw_call)

        except httpx.TimeoutException as exc:
            raise RetryableLLMError(f"请求超时：{exc!r}") from exc
        except httpx.TransportError as exc:
            raise RetryableLLMError(f"网络错误：{exc!r}") from exc

        tool_calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
                index=index,
            )
            for index, slot in sorted(tool_slots.items())
        ]

        _log.debug(
            "LLM 完成 finish=%s tokens=%d 工具=%d 正文=%d 字",
            finish_reason,
            usage.total_tokens,
            len(tool_calls),
            len("".join(text_parts)),
        )

        return LLMResponse(
            text=strip_leaked_chunk_json("".join(text_parts)) or "",
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )


def strip_leaked_chunk_json(content: str | None) -> str | None:
    """剔除个别兼容服务泄漏到正文里的流式 chunk JSON。

    现象（在国内某家兼容服务上真实踩到过）：模型在"正文 + 工具调用"混合回复里，
    会把内部的流式 delta 块原样拼进正文末尾，形如
    `好的，我来查一下{"index":0,"finish_reason":"tool_calls","delta":{...}}`。

    只在**末尾那段确实能解析成流式 chunk 结构**时才剔除，所以用户让模型
    正常输出 JSON 时不会被误删。

    注意它只清洗最终聚合结果：已经流到终端上的那几个字符收不回来。
    真正重要的是别让它进 `messages` —— 脏正文下一轮会被当成模型自己的
    历史输出重新喂回去，越滚越乱。
    """
    if not content:
        return content
    index = content.find('{"index"')
    if index == -1:
        return content
    try:
        parsed = json.loads(content[index:])
    except json.JSONDecodeError:
        return content
    if isinstance(parsed, dict) and "delta" in parsed and "finish_reason" in parsed:
        return content[:index].rstrip() or None
    return content


def parse_usage(raw: dict[str, Any]) -> Usage:
    """兼容两种缓存字段名：

    - OpenAI：`prompt_tokens_details.cached_tokens`（嵌套在 details 里）
    - DeepSeek：`prompt_cache_hit_tokens`（平铺，自家字段名）

    缓存命中只有约 0.1x 输入价，不算清就没法知道省了多少 ——
    与 anthropic.parse_usage 同一原则：IR 的 cache_read_tokens 必须被填上。
    """
    details = raw.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    cached += int(raw.get("prompt_cache_hit_tokens") or 0)
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens") or 0),
        completion_tokens=int(raw.get("completion_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        cache_read_tokens=cached,
    )


def accumulate_tool_call(slots: dict[int, dict[str, str]], delta: dict[str, Any]) -> None:
    """流式 tool_call 是按 index 分片下发的，参数要拼完才是合法 JSON。

    这是最容易写错的一处：`function.arguments` 会被切成好几段
    （`{"sym` / `bol":` / ` "600519"}`），必须按 index 累加字符串。
    """
    index = int(delta.get("index") or 0)
    slot = slots.setdefault(index, {"id": "", "name": "", "arguments": ""})

    if delta.get("id"):
        slot["id"] = delta["id"]

    function = delta.get("function") or {}
    if function.get("name"):
        slot["name"] = function["name"]
    if function.get("arguments"):
        slot["arguments"] += function["arguments"]
