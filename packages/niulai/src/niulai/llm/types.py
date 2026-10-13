"""LLM 层的公共类型 —— 一套协议无关的中间表示（IR）。

这里的类型刻意不绑定任何厂商的线格式。序列化（怎么变成 HTTP body）是各
客户端自己的事，放在 openai_compat.py / anthropic.py 里，不要放回这一层。

不给 Message 挂 to_wire()：Anthropic 协议需要跨消息重组（system 提到
顶层参数、连续的 tool_result 合进同一条 user 消息），per-message 的方法
表达不了这种约束 —— 转换必须由客户端握着整个消息列表来做。

raw_blocks 是留给"模型吐出来、但必须原样送回去"的不透明块（Anthropic 的
thinking / redacted_thinking）。内核不解读它，只负责搬运。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# system 是给模型的指令、user 是用户、assistant 是模型、tool 是工具结果。
Role = Literal["system", "user", "assistant", "tool"]


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串。不要在这一层解析，解析失败要能原样回灌给模型
    index: int = 0


@dataclass(slots=True)
class Message:
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    # 工具结果是否失败。OpenAI 那边只能靠正文表达（忽略这个字段），
    # Anthropic 有正式的 is_error 标记 —— 模型对它的反应比读正文更准。
    is_error: bool = False
    # 厂商私有、必须原样回传的内容块。内核只搬运，不解读。
    raw_blocks: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 缓存读写单独记：读缓存只要约 0.1x 输入价，不算清就没法知道省了多少
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def merged(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        data = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
        # 只在真的用到缓存时才写入 trace，避免 JSONL 里全是 0 的噪音
        if self.cache_read_tokens:
            data["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens:
            data["cache_write_tokens"] = self.cache_write_tokens
        return data


@dataclass(slots=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    # 见 `Message.raw_blocks`。由客户端填充，由 loop 原样透传回 Message。
    raw_blocks: list[dict[str, Any]] | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)
