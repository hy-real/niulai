"""Run trace：每行一个事件的 JSONL。

为什么值得单独做一层：

1. **调试** —— 出问题不用重跑，直接看轨迹；
2. **演示** —— 不填 API Key 也能回放一次真实运行（README 的 GIF 就是这么录的）；
3. **面试** —— 能拿出真实运行记录，讲清楚每一步为什么这么走。

trace 里**绝不能出现 API Key**：build 事件时只放模型名与用量，不放请求头。

## `seq` 会有空洞，这是对的

`seq` 是**事件流里的全局序号**，在 `EventEmitter.emit` 那一刻分配。
而 trace 是这条流的一个**投影** —— `text_delta` 这类纯 UI 事件被过滤掉了，
所以文件里的 seq 可能是 `1, 2, 4, 5, …`。

不要为了"看起来整齐"去重新编号：那样 trace 的 seq 就和实时订阅者
（CLI / SSE 客户端）看到的对不上了，两边日志没法对照着查问题。
判断完整性看 **seq 严格递增且无重复**，而不是看它连不连续。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .events import UI_ONLY_EVENTS, Event
from .paths import trace_dir


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

    进程被杀时最后一行可能只写了一半（见 architecture.md 7.13），
    所以残缺行要跳过而不是让整个回放崩掉 —— 与 session.py 读取会话同一个原则。
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
