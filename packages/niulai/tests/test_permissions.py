"""权限引擎测试：策略判定 + 审批门 + always 记忆。

全部无网络、无 Key。核心证明三件事：
1. 策略判定是确定性的（同一输入永远同一判定，不靠模型自觉）；
2. 审批走事件 + await，拒绝回灌的是"不要再尝试"而不是终止；
3. fail-safe：没有通道 = 拒绝。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from niulai.events import EventEmitter, EventType
from niulai.llm.types import ToolCall
from niulai.permissions import (
    ALLOW,
    ASK,
    DENY,
    Approval,
    ApprovalMemory,
    PermissionGate,
    PermissionRequest,
    decide,
    session_scope_dir,
)

WRITE_CAPS = frozenset({"write_local"})
READ_CAPS = frozenset({"read", "network"})


def _write_call(path: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(
        id=call_id, name="write_file", arguments=f'{{"path":"{path}","content":"x"}}'
    )


def _gate(tmp_path: Path, *, channel=None, memory=None, session_id="sess-1"):
    emitter = EventEmitter(run_id="test-run")
    events: list = []
    emitter.subscribe(events.append)
    gate = PermissionGate(
        emitter=emitter,
        channel=channel,
        memory=memory or ApprovalMemory(),
        session_id=session_id,
        run_id="test-run",
        data_root=tmp_path,
    )
    return gate, events


# --------------------------------------------------------------------------
# 策略判定（纯函数）
# --------------------------------------------------------------------------


def test_只读工具一律放行():
    verdict = decide(
        _write_call("任何路径.html"),
        READ_CAPS,
        data_root=Path("/data"),
        scope_dir="sessions/s1",
    )
    assert verdict.decision == ALLOW


def test_会话目录内的新文件直接放行(tmp_path):
    verdict = decide(
        _write_call("sessions/sess-1/茅台-分析.md"),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == ALLOW


def test_覆盖会话目录内已存在文件要问(tmp_path):
    target = tmp_path / "sessions" / "sess-1" / "旧报告.md"
    target.parent.mkdir(parents=True)
    target.write_text("上一版", encoding="utf-8")

    verdict = decide(
        _write_call("sessions/sess-1/旧报告.md"),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == ASK
    assert verdict.reason == "modify_existing"


def test_写数据目录之内但会话目录之外要问(tmp_path):
    verdict = decide(
        _write_call("config.toml"),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == ASK
    assert verdict.reason == "outside_scope"


def test_会话目录按会话隔离_别的会话的目录也算外面(tmp_path):
    verdict = decide(
        _write_call("sessions/sess-2/x.md"),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == ASK
    assert verdict.reason == "outside_scope"


def test_写数据目录之外硬拒绝(tmp_path):
    verdict = decide(
        _write_call("../外面.txt"),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == DENY
    assert verdict.reason == "outside_data_root"


def test_绝对路径逃逸也拒绝(tmp_path):
    # as_posix：Windows 反斜杠进 JSON 参数会是非法转义，模型也不会那么传
    outside = (tmp_path.parent / "别处.txt").as_posix()
    verdict = decide(
        _write_call(outside),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == DENY


def test_目录内绝对路径放行(tmp_path):
    verdict = decide(
        _write_call((tmp_path / "sessions" / "sess-1" / "a.md").as_posix()),
        WRITE_CAPS,
        data_root=tmp_path,
        scope_dir="sessions/sess-1",
    )
    assert verdict.decision == ALLOW


def test_无会话时输出目录退化为run目录():
    assert session_scope_dir(None, "run-1") == "sessions/run-1"
    assert session_scope_dir("sess-1", "run-1") == "sessions/sess-1"


# --------------------------------------------------------------------------
# 审批门
# --------------------------------------------------------------------------


class _Channel:
    def __init__(self, approval: Approval) -> None:
        self.approval = approval
        self.seen: list[PermissionRequest] = []

    async def request(self, req: PermissionRequest) -> Approval:
        self.seen.append(req)
        return self.approval


async def test_问一次_允许后放行(tmp_path):
    channel = _Channel(Approval(granted=True))
    gate, events = _gate(tmp_path, channel=channel)

    blocked = await gate.check(_write_call("config.toml"), WRITE_CAPS, turn=1)

    assert blocked is None
    assert len(channel.seen) == 1
    requests = [e for e in events if e.type is EventType.PERMISSION_REQUEST]
    decisions = [e for e in events if e.type is EventType.PERMISSION_DECISION]
    assert len(requests) == 1 and len(decisions) == 1
    assert requests[0].data["reason"] == "outside_scope"
    assert decisions[0].data["granted"] is True


async def test_拒绝后回灌的是可恢复指引(tmp_path):
    channel = _Channel(Approval(granted=False))
    gate, events = _gate(tmp_path, channel=channel)

    blocked = await gate.check(_write_call("config.toml"), WRITE_CAPS, turn=1)

    assert blocked is not None
    assert "用户拒绝" in blocked
    # 第二句不能省：否则模型会换个参数反复重试
    assert "不要再尝试" in blocked
    decisions = [e for e in events if e.type is EventType.PERMISSION_DECISION]
    assert decisions[0].data["granted"] is False
    assert decisions[0].data["by"] == "user"


async def test_没有审批通道时按拒绝处理(tmp_path):
    """--json / 测试 / 无人值守：没人能回答 y/n，必须 fail-safe。"""
    gate, events = _gate(tmp_path, channel=None)

    blocked = await gate.check(_write_call("config.toml"), WRITE_CAPS, turn=1)

    assert blocked is not None
    # 没人问过，不能冤枉用户
    assert "用户拒绝" not in blocked
    assert "没有可用的审批通道" in blocked
    assert "不要再尝试" in blocked
    decisions = [e for e in events if e.type is EventType.PERMISSION_DECISION]
    assert decisions[0].data["by"] == "no_channel"


async def test_硬拒绝不发request直接回灌(tmp_path):
    gate, events = _gate(tmp_path, channel=_Channel(Approval(granted=True)))

    blocked = await gate.check(_write_call("../外面.txt"), WRITE_CAPS, turn=1)

    assert blocked is not None
    assert "权限策略拒绝" in blocked
    assert "不要再尝试" in blocked
    assert not [e for e in events if e.type is EventType.PERMISSION_REQUEST]


async def test_总是允许后同目录不再问(tmp_path):
    channel = _Channel(Approval(granted=True, always=True))
    memory = ApprovalMemory()
    gate, _ = _gate(tmp_path, channel=channel, memory=memory)

    first = await gate.check(_write_call("其他目录/a.md"), WRITE_CAPS, turn=1)
    assert first is None
    assert len(channel.seen) == 1

    # 同一个（工具, 目录）的第二次调用：直接放行，不再打扰用户
    gate2, events2 = _gate(tmp_path, channel=channel, memory=memory)
    second = await gate2.check(_write_call("其他目录/b.md"), WRITE_CAPS, turn=2)

    assert second is None
    assert len(channel.seen) == 1  # 没有第二次询问
    decisions = [e for e in events2 if e.type is EventType.PERMISSION_DECISION]
    assert decisions[0].data["by"] == "always_memory"


async def test_总是允许按会话隔离(tmp_path):
    channel = _Channel(Approval(granted=True, always=True))
    memory = ApprovalMemory()
    gate, _ = _gate(tmp_path, channel=channel, memory=memory, session_id="sess-1")
    await gate.check(_write_call("其他目录/a.md"), WRITE_CAPS, turn=1)

    gate2, _ = _gate(tmp_path, channel=channel, memory=memory, session_id="sess-2")
    await gate2.check(_write_call("其他目录/a.md"), WRITE_CAPS, turn=2)

    assert len(channel.seen) == 2  # 另一个会话照问


async def test_只读调用完全不触发审批(tmp_path):
    channel = _Channel(Approval(granted=False))
    gate, events = _gate(tmp_path, channel=channel)

    blocked = await gate.check(
        ToolCall(id="c1", name="stock_quote", arguments='{"symbol":"600519"}'),
        READ_CAPS,
        turn=1,
    )

    assert blocked is None
    assert channel.seen == []
    assert not [e for e in events if e.type is EventType.PERMISSION_REQUEST]


async def test_审批途中取消不炸(tmp_path):
    """用户点停止：CancelledError 要从审批等待里透出去（由内核收尾）。"""

    class _HangingChannel:
        async def request(self, req: PermissionRequest) -> Approval:
            await asyncio.sleep(10)
            return Approval(granted=True)

    gate, _ = _gate(tmp_path, channel=_HangingChannel())
    task = asyncio.create_task(
        gate.check(_write_call("config.toml"), WRITE_CAPS, turn=1)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------
# ApprovalMemory
# --------------------------------------------------------------------------


def test_记忆按会话隔离且可整会话清除():
    memory = ApprovalMemory()
    memory.remember("sess-1", "write_file:sessions/sess-1")
    assert memory.hit("sess-1", "write_file:sessions/sess-1")
    assert not memory.hit("sess-2", "write_file:sessions/sess-1")
    memory.forget_session("sess-1")
    assert not memory.hit("sess-1", "write_file:sessions/sess-1")
