"""运行时熔断。

**为什么不能只写在 prompt 里。**

`system_v1.md` 第 3 条写着"同一个工具连续失败两次就停下来"。但那是**请求**，
不是**保证** —— prompt 是概率性的，runtime 是确定性的。凡是"必须每次都成立"
的约束，只写在 prompt 里就等于没写：模型不听话时没有任何兜底，空转烧 token。

这一层就是那条纪律的强制执行。

## 两种拦截

1. **同参数重复调用** —— 模型陷入循环，反复要同一份数据。
   比较参数时要做规范化（键排序 + 去空白），否则 `{"a":1,"b":2}` 和
   `{"b":2,"a":1}` 会被当成两次不同的调用，守卫形同虚设。
2. **同工具连续失败** —— 上游挂了，模型换个参数继续撞同一个工具。
   连续失败到阈值就熔断，逼它去用别的工具或直接回答。

注意"连续"两个字：中间有一次成功就清零。否则一个查了 5 只股票、中间失败
2 次的正常流程会被误伤。

## 拦截不能只是"拒绝"

assistant 消息已经声明了 tool_calls，transcript 里必须给**每一个** call
一条对应结果，否则下一次请求协议非法（OpenAI 会 400，Anthropic 更直接）。
所以拦截时照样回灌一条消息，内容写成"别再试了，改走别的路"。

## 状态属于谁

守卫是**一次 run 一个实例**，不放在 `ToolRegistry` 里。工具注册表通常是长生命
周期对象，把调用计数塞进去会跨 run 污染（AgentForge 的模块级单例 dict 就是这个
毛病，永不清理）。这也符合"Runtime 全部靠构造注入"的约定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..llm.types import ToolCall

DEFAULT_MAX_CONSECUTIVE_FAILURES = 2


@dataclass(slots=True)
class Breaker:
    """一次被拦下的调用。

    `message` 是**回灌给模型**的文本（不是给人看的日志）—— 它必须写成
    "接下来该怎么做"，否则模型会换个姿势再撞一次同一件事。
    """

    name: str
    reason: str  # duplicate_call | consecutive_failures
    message: str


@dataclass(slots=True)
class ToolGuard:
    """一次 run 内的调用守卫。"""

    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    _failures: dict[str, int] = field(default_factory=dict)
    _done: set[tuple[str, str]] = field(default_factory=set)
    # 被拦下的调用（供 UI / trace 展示）
    breaks: list[Breaker] = field(default_factory=list)

    def check(self, call: ToolCall) -> Breaker | None:
        """返回拦截原因；`None` 表示放行。

        放行时**立刻登记**这次调用 —— 登记发生在执行之前，因为要防的是
        "同一个调用被执行两次"，而不是"被执行两次后才发现"。
        """
        key = (call.name, normalize_arguments(call.arguments))

        if key in self._done:
            return self._break(
                call,
                reason="duplicate_call",
                message=(
                    f"完全相同的调用已经执行过了（`{call.name}`，参数 "
                    f"`{_short(call.arguments)}`）。结果就在上面的对话里，"
                    "请直接使用它。如果那次结果不够用，请**改变参数**"
                    "（例如缩小范围、换周期）后再调用，不要重复同样的请求。"
                ),
            )

        failures = self._failures.get(call.name, 0)
        if failures >= self.max_consecutive_failures:
            return self._break(
                call,
                reason="consecutive_failures",
                message=(
                    f"`{call.name}` 已经连续失败 {failures} 次，已被运行时熔断。"
                    "不要再调用它了 —— 反复重试同一个工具不会让上游恢复。"
                    "请改用其他工具，或者基于已有信息直接回答用户并说明缺什么。"
                ),
            )

        self._done.add(key)
        return None

    def _break(self, call: ToolCall, *, reason: str, message: str) -> Breaker:
        breaker = Breaker(name=call.name, reason=reason, message=message)
        self.breaks.append(breaker)
        return breaker

    def record(self, call: ToolCall, *, ok: bool) -> None:
        """记录执行结果，维护"连续失败"计数。"""
        if ok:
            self._failures.pop(call.name, None)
        else:
            self._failures[call.name] = self._failures.get(call.name, 0) + 1

    def forget(self, call: ToolCall) -> None:
        """把一次调用从"已执行"里撤掉（执行被取消/中断时用）。"""
        self._done.discard((call.name, normalize_arguments(call.arguments)))


def normalize_arguments(raw: str) -> str:
    """把参数规范化成可比较的字符串。

    解析失败就退回去空白 —— 那种调用反正会被参数校验挡下，
    这里只要求两个"长得一样"的坏参数能被认成同一个。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        return text


def _short(raw: str, limit: int = 120) -> str:
    text = (raw or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "Breaker",
    "ToolGuard",
    "normalize_arguments",
]
