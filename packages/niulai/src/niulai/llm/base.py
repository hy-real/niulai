"""LLM 客户端契约。

内核只认这个 Protocol，不认具体厂商 —— 所以 `AgentRuntime` 的签名里
出现的是 `LLMClient`，不是 `OpenAICompatClient`。
换协议 = 换一个实现，内核一行都不用改（也没法改，它看不到差别）。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, runtime_checkable

from ..errors import RetryableLLMError
from ..logs import get_logger
from .types import LLMResponse, Message, ToolSpec

_log = get_logger("llm")

TextHandler = Callable[[str], None]


@runtime_checkable
class LLMClient(Protocol):
    async def stream_chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        on_text: TextHandler | None = None,
    ) -> LLMResponse:
        """流式调用，返回聚合后的结果。

        `on_text` 会被逐段调用（只对正文，不含思考过程）。
        工具调用的参数是分片下发的，必须在这儿拼完整 —— 上层拿到的
        `ToolCall.arguments` 永远是一个完整的 JSON 字符串。
        """
        ...

    async def aclose(self) -> None: ...


async def stream_with_retry(
    *,
    max_retries: int,
    attempt_runner: Callable[[TextHandler], Awaitable[LLMResponse]],
    on_text: TextHandler | None,
) -> LLMResponse:
    """只在还没吐出任何正文时重试。

    一旦有内容已经流给用户，再重试就会重复输出 —— 那种情况不如直接失败。
    两个协议的重试策略完全一样，抽出来共用，免得改一处忘一处。

    不能把重试放在 `async with client.stream(...)` 里面：响应体被消费掉之后
    那条连接就废了，只能整个请求重发，所以 `attempt_runner` 必须是"一整次请求"。
    """
    attempts = max_retries + 1
    last_error: RetryableLLMError | None = None

    for attempt in range(attempts):
        emitted = False

        def _on_text(chunk: str) -> None:
            nonlocal emitted
            emitted = True
            if on_text is not None:
                on_text(chunk)

        try:
            return await attempt_runner(_on_text)
        except RetryableLLMError as exc:
            last_error = exc
            if emitted or attempt == attempts - 1:
                raise
            delay = 2**attempt + random.uniform(0, 0.5)
            _log.warning(
                "LLM 请求第 %d/%d 次失败（%s），%.1f 秒后重试", attempt + 1, attempts, exc, delay
            )
            # 退避 + 抖动。抖动不是装饰：限流场景下所有客户端如果都按
            # 1s/2s/4s 整齐重试，会在同一时刻再次撞在一起（重试风暴）。
            await asyncio.sleep(delay)

    raise last_error or RetryableLLMError("LLM 请求失败")


def endpoint(base_url: str, path: str) -> str:
    """拼接口地址，容忍用户直接把完整 endpoint 填进 base_url。

    真实用户会这么填：`https://api.deepseek.com/v1/chat/completions`。
    这时不能再拼一次，否则变成 `.../chat/completions/chat/completions`。
    """
    base = base_url.rstrip("/")
    return base if base.endswith(path) else base + path
