"""事件流：内核与外界之间唯一的沟通方式。

内核只负责"发事件"，谁订阅（CLI 打印 / SSE 推给浏览器 / 落 trace）是外部的事。
这条边界就是"内核不知道 UI 存在"的落点 —— 也是将来能一份内核四种外壳的原因。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    RUN_START = "run_start"
    TURN_START = "turn_start"
    # TEXT_DELTA 是纯 UI 事件：逐字流给前端用。它不进 trace（否则一个 JSONL 几十万行）。
    # 每一轮的完整回复由 ASSISTANT_MESSAGE 记录。
    TEXT_DELTA = "text_delta"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # 运行时熔断：拦下一次重复 / 连续失败的调用。
    # 单独成一个事件，是为了让"agent 试图空转"这件事在 UI 和 trace 里都看得见。
    CIRCUIT_BREAK = "circuit_break"
    # 权限审批（7.12 / 7.14）：写类工具命中"要问"的判定时发出，内核 await
    # 外壳答复（CLI y/n、Web 弹窗）。两个事件都进 trace —— 回放时要看得见
    # "用户当时批了什么、谁批的"（超时拒绝也要留痕）。
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DECISION = "permission_decision"
    # 大结果已落盘，上下文里只留摘要 + 路径
    ARTIFACT = "artifact"
    USAGE = "usage"
    FINAL = "final"
    ERROR = "error"
    RUN_END = "run_end"
    # 子代理（sub-agent）：内核把一项任务委派给嵌套的 run（如报告撰写）。
    # 子 run 不另起 trace 文件 —— 它的事件带 `sub_agent` 标记汇入父 run 同一条流，
    # seq 继续编号（SubAgentEmitter 转发时沿用父的），回放时层级可分辨。
    SUB_RUN_START = "sub_run_start"
    SUB_RUN_END = "sub_run_end"


# 不写进 trace 的事件类型（高频、可由其他事件重建）
UI_ONLY_EVENTS: frozenset[EventType] = frozenset({EventType.TEXT_DELTA})


@dataclass(slots=True)
class Event:
    type: EventType
    run_id: str
    seq: int
    ts: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "run_id": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            **self.data,
        }


Sink = Callable[[Event], None]


def now_iso() -> str:
    """本地时区的 ISO8601（带毫秒）。"""
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_run_id() -> str:
    """形如 `20260921T213045-a1b2`，按时间可排序。"""
    import secrets

    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


class EventEmitter:
    """给事件编号、打时间戳，然后扇出给所有 sink。"""

    def __init__(self, run_id: str, sinks: Sequence[Sink] = ()) -> None:
        self.run_id = run_id
        self._seq = 0
        self._sinks: list[Sink] = list(sinks)

    def subscribe(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def emit(self, type_: EventType, **data: Any) -> Event:
        self._seq += 1
        event = Event(type=type_, run_id=self.run_id, seq=self._seq, ts=now_iso(), data=data)
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:
                # sink 是旁路（打印 / 落盘），它失败不能把 agent 跑挂。
                # 落盘类 sink 自己负责把错误暴露出来，见 trace.TraceWriter。
                pass
        return event


class SubAgentEmitter:
    """子 run 的事件出口：转发给父 emitter，只给每个事件打 `sub_agent` 标。

    **为什么需要它**：子 run 与父 run 共用同一条事件流 / 同一个 trace 文件
    （一份运行、一个 journal），但两条隐忧要解决：

    1. **seq 必须连续** —— 另起一个 EventEmitter 会从 1 重新编号，同一个
       run_id 下出现重复 seq，回放对账就乱了。所以这里不持有 seq，全部
       经父 emitter 发出；
    2. **层级必须可分辨** —— 前端 / trace 要靠 `sub_agent` 标记区分
       "这是子代理在调工具"而不是父代理。

    它不是 EventEmitter 的子类（没有自己的 seq / sinks），只是 loop 所需的
    最小接口：`run_id` 属性 + `emit()`。
    """

    def __init__(self, parent: EventEmitter, name: str) -> None:
        self._parent = parent
        self._name = name

    @property
    def run_id(self) -> str:
        return self._parent.run_id

    def emit(self, type_: EventType, **data: Any) -> Event:
        # 以本 wrapper 的标为准（调用方也传 sub_agent 时覆盖它）——
        # 父子都用同一个键，谁生效必须只有一个答案。
        data["sub_agent"] = self._name
        return self._parent.emit(type_, **data)
