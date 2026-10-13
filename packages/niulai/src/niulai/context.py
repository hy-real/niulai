"""上下文预算：历史太长时怎么裁。

两条铁律，违反任何一条都会立刻炸。

一、按轮对齐，绝不拆散成对的消息。一次工具调用由两条消息组成：
`assistant(tool_calls=[...])` 和紧跟其后的 `tool(tool_call_id=...)`，
拆散它们就是非法请求 —— 两个协议都会报错。所以这里不删消息，只改内容；
按"条"裁剪，tool_call 和 tool_result 被切开只是时间问题。

二、压缩而不是丢弃。直接删掉早期工具结果，会让模型以为那段调用从没发生
过，于是"重新查一遍"—— 反而更贵。正确做法是把内容换成一句如实的占位
说明：它知道这里曾有数据、知道数据有多大，需要时可以重新取。

当前是有损内容压缩：结构全保留，只有最早的那几条工具结果被换成说明。

注意裁剪出来的是一份视图，不是新的历史。会话里存着的始终是完整
transcript，发给模型的才是裁剪后的 —— 恢复、回放、审计都不丢信息。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .llm.types import Message

# 保留最近多少条工具结果的原样内容。行情类数据时效性强，老结果留着价值低。
DEFAULT_KEEP_RECENT_TOOL_RESULTS = 15

_PLACEHOLDER = "[此处原有一次 `{name}` 的返回，共 {chars:,} 字符，为控制上下文已省略。" \
    "如果这份数据仍然需要，请重新调用该工具。]"


@dataclass(slots=True)
class ContextStats:
    """这次裁剪做了什么 —— 写进事件流，让"上下文被压了多少"看得见。"""

    omitted: int = 0
    saved_chars: int = 0

    @property
    def changed(self) -> bool:
        return self.omitted > 0


def fit_context(
    messages: list[Message],
    *,
    keep_recent_tool_results: int = DEFAULT_KEEP_RECENT_TOOL_RESULTS,
) -> tuple[list[Message], ContextStats]:
    """返回一份可用于发请求的裁剪视图，以及统计。

    输入列表不会被修改。
    """
    if keep_recent_tool_results < 0:
        keep_recent_tool_results = 0

    tool_positions = [i for i, m in enumerate(messages) if m.role == "tool"]
    drop = tool_positions[: max(0, len(tool_positions) - keep_recent_tool_results)]
    if not drop:
        return list(messages), ContextStats()

    drop_set = set(drop)
    stats = ContextStats()
    view: list[Message] = []

    for index, message in enumerate(messages):
        if index not in drop_set:
            view.append(message)
            continue

        original = message.content or ""
        stats.omitted += 1
        stats.saved_chars += len(original)
        view.append(
            replace(
                message,
                content=_PLACEHOLDER.format(name=message.name or "工具", chars=len(original)),
            )
        )

    return view, stats


__all__ = ["DEFAULT_KEEP_RECENT_TOOL_RESULTS", "ContextStats", "fit_context"]
