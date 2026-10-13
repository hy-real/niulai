"""Run trace：每行一个事件的 JSONL。

值得单独做一层的原因：

1. 调试 —— 出问题不用重跑，直接看轨迹；
2. 演示 —— 不填 API Key 也能回放一次真实运行；
3. 恢复 —— 它同时是 run journal：进程被杀后从这里重建 transcript 续跑。

trace 里绝不能出现 API Key：build 事件时只放模型名与用量，不放请求头。

## `seq` 会有空洞，这是对的

`seq` 是事件流里的全局序号，在 EventEmitter.emit 那一刻分配。而 trace 是
这条流的一个投影 —— text_delta 这类纯 UI 事件被过滤掉了，所以文件里的
seq 可能是 `1, 2, 4, 5, …`。

不要为了"看起来整齐"去重新编号：那样 trace 的 seq 就和实时订阅者（CLI /
SSE 客户端）看到的对不上了，两边日志没法对照着查问题。判断完整性看
seq 严格递增且无重复，而不是看它连不连续。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import NiulaiError
from .events import UI_ONLY_EVENTS, Event
from .llm.types import Message, ToolCall
from .paths import trace_dir


class TraceResumeError(NiulaiError):
    """journal 不具备恢复条件（如旧格式缺对账字段）。"""


@dataclass(slots=True)
class ResumePlan:
    """从 journal 重建出的恢复计划。"""

    run_id: str
    messages: list[Message] = field(default_factory=list)  # 不含 system，同 run() 入口契约
    interrupted: bool = True  # journal 里没有 run_end 记录 = 未正常收尾


class TraceWriter:
    """把事件追加写入 JSONL。可以直接当 sink 挂到 EventEmitter 上。

    每次 append 后立即关闭文件句柄：崩溃 / Ctrl-C 时已写内容不丢。
    """

    def __init__(self, run_id: str, directory: Path | None = None) -> None:
        self.run_id = run_id
        self._dir = Path(directory) if directory else trace_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        self.path = self._dir / f"{run_id}.jsonl"
        self._write_failed = False

    def __call__(self, event: Event) -> None:
        if event.type in UI_ONLY_EVENTS:
            return  # 逐字增量不进 trace，最终内容由 assistant_message / final 记录
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            # EventEmitter 会吞掉 sink 的异常（旁路不能拖垮 agent），所以这里
            # 如果也不吭声，磁盘满 / 目录被删时 trace 会静默全丢 —— 排查问题时
            # 才发现什么都没记下来。至少喊一声，且只喊一次，别刷屏。
            if not self._write_failed:
                self._write_failed = True
                print(f"警告：trace 写入失败，本次运行的轨迹不会落盘：{exc}", file=sys.stderr)


def read_trace(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读回一个 trace 文件。

    进程被杀时最后一行可能只写了一半，所以残缺行要跳过而不是让整个回放
    崩掉 —— 与 session.py 读取会话同一个原则。
    """
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # 半行就是损坏尾行，后面的（如果有）仍是完整 JSON


def find_latest(directory: Path | None = None) -> Path | None:
    d = Path(directory) if directory else trace_dir()
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def resolve_run(ref: str, directory: Path | None = None) -> Path:
    """`latest` / run_id / 文件路径 都能解析成一个 trace 文件。"""
    if ref in ("latest", "last"):
        path = find_latest(directory)
        if path is None:
            raise FileNotFoundError("还没有任何 trace。先跑一次 `niulai ask \"...\"`。")
        return path

    direct = Path(ref)
    if direct.exists():
        return direct

    d = Path(directory) if directory else trace_dir()
    matches = sorted(d.glob(f"*{ref}*.jsonl"))
    if not matches:
        raise FileNotFoundError(f"找不到 trace：{ref}（目录 {d}）")
    return matches[0]


# ---------------------------------------------------------------------- 恢复


def load_transcript(path: Path) -> ResumePlan:
    """从 journal 重建一次（可能被中断的）run 的 transcript（恢复数据源）。

    重建规则：journal 是事件流的忠实投影（每条消息 append 与 emit 同步发生），
    所以按事件顺序拼回即得原 transcript。四种事件有用：

    - run_start        → user 消息（task）
    - assistant_message → assistant 消息（带 tool_calls 时的 id 是对账键）
    - tool_result      → tool 消息（content 是完整结果，恢复必需）
    - circuit_break    → tool 消息（被拦下的调用也回灌了结果，同样要拼回）

    两个协议不接受"声明了 tool_call 却没有结果"的结尾。进程死在执行中途时，
    重建出的 transcript 尾部就会有这种悬空 —— 不在这里猜结果，交给
    `_repair_transcript`（loop.py）按"结果未知"补齐，调用方拿到即可直接续跑。
    """
    messages: list[Message] = []
    run_id = ""
    interrupted = True

    for record in read_trace(path):
        kind = record.get("type")

        if kind == "run_start":
            run_id = str(record.get("run_id") or path.stem)
            messages.append(Message(role="user", content=str(record.get("task") or "")))

        elif kind == "assistant_message":
            raw_calls = record.get("tool_calls") or []
            if any(not c.get("id") for c in raw_calls):
                raise TraceResumeError(
                    f"{Path(path).name} 是旧格式（tool_call 缺 id，无法与结果对账），不能恢复。"
                    "跑一次新版本后产生的 trace 才支持 resume。"
                )
            tool_calls = [
                ToolCall(
                    id=str(c["id"]),
                    name=str(c.get("name") or ""),
                    arguments=str(c.get("arguments") or ""),
                )
                for c in raw_calls
            ]
            messages.append(
                Message(
                    role="assistant",
                    content=record.get("text") or None,
                    tool_calls=tool_calls or None,
                )
            )

        elif kind == "tool_result":
            call_id = record.get("tool_call_id")
            if not call_id:
                continue  # 旧格式缺对账键：丢掉，尾部由 repair 补"结果未知"
            messages.append(
                Message(
                    role="tool",
                    content=str(record.get("content") or record.get("preview") or ""),
                    tool_call_id=str(call_id),
                    name=str(record.get("name") or "") or None,
                    is_error=not record.get("ok", True),
                )
            )

        elif kind == "circuit_break":
            call_id = record.get("tool_call_id")
            if not call_id:
                continue
            messages.append(
                Message(
                    role="tool",
                    content=str(record.get("message") or ""),
                    tool_call_id=str(call_id),
                    name=str(record.get("name") or "") or None,
                    is_error=True,
                )
            )

        elif kind == "run_end":
            interrupted = False

    return ResumePlan(run_id=run_id or Path(path).stem, messages=messages, interrupted=interrupted)
