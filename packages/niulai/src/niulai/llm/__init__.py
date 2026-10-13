"""LLM 适配层。

两套协议，一套中间表示。上层（agent / tools）只认 LLMClient，换协议 =
换一个实现类 + 改一个配置项，内核一行都不用动。

- OpenAICompatClient —— /chat/completions，覆盖 DeepSeek / 通义 / 混元 /
  Kimi / OpenAI / 各种中转。兼容面最广，是默认。
- AnthropicClient —— /messages，Claude 系。差异最多，具体见该模块开头。

用法：

    from niulai.llm import build_client

    async with build_client(settings) as llm:
        response = await llm.stream_chat(messages, tools, on_text=print)
"""

from __future__ import annotations

from ..config import Settings
from .anthropic import AnthropicClient
from .base import LLMClient, TextHandler
from .openai_compat import OpenAICompatClient
from .types import LLMResponse, Message, ToolCall, ToolSpec, Usage


def build_client(settings: Settings) -> LLMClient:
    """按 `settings.protocol` 造客户端。

    返回的是 `LLMClient` 而不是具体类 —— 调用方拿不到协议相关的方法，
    也就不会不小心依赖上某一家的特性。
    """
    if settings.protocol == "anthropic":
        return AnthropicClient(settings)
    return OpenAICompatClient(settings)


__all__ = [
    "AnthropicClient",
    "LLMClient",
    "LLMResponse",
    "Message",
    "OpenAICompatClient",
    "TextHandler",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "build_client",
]
