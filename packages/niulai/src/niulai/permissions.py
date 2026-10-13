"""权限引擎：工具只声明能力，能不能用由这一层判定。

三层模型：能力声明（工具）/ 策略判定（本模块）/ 审批通道（外壳）。
被审对象不能当法官：一个外部 MCP server 说"我很安全"是不算数的。

- 判定三态：allow（直接放行）/ deny（硬拒绝，问都不问）/ ask（发
  permission_request 事件，等待外壳答复）；
- 审批是事件，不是同步 if —— 内核 await 一个答复，CLI 弹 y/n、Web 弹窗、
  无人值守走预授权，各外壳共用同一份内核代码；
- fail-safe：没有审批通道（--json / 测试）= 拒绝；Web 审批超时 = 拒绝。
  宁可拦住一次正常操作，不可放过一次越权；
- 拒绝要回灌，不是终止 —— 转成一条"别再试了"的 tool 消息（错误字符串本身
  就是 prompt）；
- "总是允许"按会话记忆，粒度是「工具 + 目标目录」，只活在进程里 ——
  用户要的是临时授权，重启即忘，不落盘。

判定规则（只对写类工具生效；只读工具一律放行，见 decide()）：

| 目标 | 判定 | 理由 |
| --- | --- | --- |
| 数据目录之外 | deny | 围栏：程序只碰自己家目录 |
| 会话目录内的新文件 | allow | 报告落地的主路径 |
| 会话目录内的已存在文件 | ask | 覆盖 = 修改，用户的东西不能默默改 |
| 数据目录内、会话目录之外 | ask | 跨目录写要显式授权 |
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from .events import EventEmitter, EventType
from .llm.types import ToolCall
from .logs import get_logger
from .paths import data_dir

_log = get_logger("permissions")

Decision = Literal["allow", "deny", "ask"]

ALLOW: Decision = "allow"
DENY: Decision = "deny"
ASK: Decision = "ask"

# 会写本机文件的能力。只读（read / network）不触发任何审批。
WRITE_CAPABILITY = "write_local"


@dataclass(frozen=True, slots=True)
class Verdict:
    """策略判定结果。

    `summary` 是 ask 时给人看的一句话；`detail` 是 deny 时回灌给模型的
    可恢复指引。两者分开：前者要短，后者要讲清"接下来该怎么办"。
    """

    decision: Decision
    reason: str = ""       # outside_data_root | modify_existing | outside_scope
    summary: str = ""      # 人看：要干什么
    target: str = ""       # 规范化后的目标（相对数据根；围栏外是原始输入）


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """一次待审批的请求。`request_id` 是幂等键。"""

    request_id: str
    run_id: str
    session_id: str | None
    tool: str
    summary: str
    reason: str
    arguments: str
    always_key: str


@dataclass(frozen=True, slots=True)
class Approval:
    """外壳给的答复。`by` 记录是谁批的 —— 回放时要看得见。"""

    granted: bool
    always: bool = False
    by: str = "user"  # user | timeout | no_channel | cancelled


@runtime_checkable
class ApprovalChannel(Protocol):
    """审批通道（外壳实现）。超时语义由通道自己定：CLI 交互式不设超时，
    Web 通道默认 120 秒后按拒绝处理。"""

    async def request(self, req: PermissionRequest) -> Approval: ...


class ApprovalMemory:
    """会话级的"总是允许"记忆。

    进程内、不落盘：用户说的是"此会话临时设置为一直许可"，重启即忘。
    键的粒度是「工具 + 目标目录」—— 只记工具名等于"允许写任意文件"。
    """

    def __init__(self) -> None:
        self._keys: dict[str, set[str]] = {}

    def hit(self, session_id: str | None, key: str) -> bool:
        return key in self._keys.get(session_id or "", set())

    def remember(self, session_id: str | None, key: str) -> None:
        self._keys.setdefault(session_id or "", set()).add(key)

    def forget_session(self, session_id: str | None) -> None:
        self._keys.pop(session_id or "", None)


def new_request_id() -> str:
    return secrets.token_hex(8)


def session_scope_dir(session_id: str | None, run_id: str) -> str:
    """当前会话的输出目录（相对数据根）。无会话的一次性提问退化成 run 目录。"""
    return f"sessions/{session_id or run_id}"


def decide(
    call: ToolCall,
    capabilities: frozenset[str],
    *,
    data_root: Path,
    scope_dir: str,
) -> Verdict:
    """默认策略。纯函数（不碰事件、不等人），方便单测。

    只读工具直接放行 —— 审批的意义在于"别默默动用户的东西"，
    读自己落盘的数据不在此列。
    """
    if WRITE_CAPABILITY not in capabilities:
        return Verdict(ALLOW)

    raw = _path_arg(call)
    if not raw:
        return Verdict(ALLOW)  # 缺路径是参数问题，交给工具自己结构化报错

    root = data_root.resolve()
    resolved = (root / raw).resolve()

    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        return Verdict(
            DENY,
            reason="outside_data_root",
            summary=f"写入数据目录之外：{raw}",
            target=raw,
        )

    scope = (root / scope_dir).resolve()
    try:
        resolved.relative_to(scope)
    except ValueError:
        return Verdict(
            ASK,
            reason="outside_scope",
            summary=f"写入当前会话目录之外：{rel}",
            target=rel,
        )

    if resolved.exists():
        return Verdict(
            ASK,
            reason="modify_existing",
            summary=f"覆盖已存在的文件：{rel}",
            target=rel,
        )

    return Verdict(ALLOW, target=rel)


class PermissionGate:
    """一次 run 的审批门。

    生命周期与 ToolGuard 一致：一次 run 一个实例，状态不放在长生命周期的
    注册表里。always 记忆是例外 —— 它按会话跨 run 有效，所以住在外壳持有的
    `ApprovalMemory` 里，门只是使用者。
    """

    def __init__(
        self,
        *,
        emitter: EventEmitter,
        channel: ApprovalChannel | None,
        memory: ApprovalMemory,
        session_id: str | None,
        run_id: str,
        data_root: Path | None = None,
    ) -> None:
        self._emitter = emitter
        self._channel = channel
        self._memory = memory
        self._session_id = session_id
        self._run_id = run_id
        self._data_root = data_root or data_dir()
        self._scope_dir = session_scope_dir(session_id, run_id)

    async def check(self, call: ToolCall, capabilities: frozenset[str], *, turn: int) -> str | None:
        """判定一次调用。返回 None = 放行；否则 = 回灌给模型的说明（不执行）。"""
        verdict = decide(
            call, capabilities, data_root=self._data_root, scope_dir=self._scope_dir
        )
        if verdict.decision == ALLOW:
            return None

        always_key = self._always_key(call, verdict)

        if verdict.decision == ASK and self._memory.hit(self._session_id, always_key):
            # 本会话已授权过同一（工具, 目录）：不再打扰用户
            self._emit_decision(
                call, turn=turn, request_id="", granted=True, always=False, by="always_memory"
            )
            return None

        if verdict.decision == DENY:
            self._emit_decision(
                call, turn=turn, request_id="", granted=False, always=False, by="policy"
            )
            return _denied_message(call, verdict, self._data_root)

        req = PermissionRequest(
            request_id=new_request_id(),
            run_id=self._run_id,
            session_id=self._session_id,
            tool=call.name,
            summary=verdict.summary,
            reason=verdict.reason,
            arguments=_short(call.arguments),
            always_key=always_key,
        )
        self._emitter.emit(
            EventType.PERMISSION_REQUEST,
            turn=turn,
            request_id=req.request_id,
            tool=req.tool,
            summary=req.summary,
            reason=req.reason,
            arguments=req.arguments,
        )

        approval = await self._ask(req)

        self._emitter.emit(
            EventType.PERMISSION_DECISION,
            turn=turn,
            request_id=req.request_id,
            tool=req.tool,
            granted=approval.granted,
            always=approval.always,
            by=approval.by,
        )

        if approval.granted:
            if approval.always:
                self._memory.remember(self._session_id, always_key)
            return None
        return _rejected_message(call, req, approval.by)

    # ------------------------------------------------------------------ 内部

    async def _ask(self, req: PermissionRequest) -> Approval:
        if self._channel is None:
            # 没有审批通道（--json 输出 / 测试 / 无人值守且未预授权）：
            # 按拒绝处理。宁可拦住一次正常操作，不可放过一次越权。
            _log.debug("没有审批通道，按拒绝处理：%s %s", req.tool, req.summary)
            return Approval(granted=False, by="no_channel")
        return await self._channel.request(req)

    def _always_key(self, call: ToolCall, verdict: Verdict) -> str:
        """「工具 + 目标目录」。目录而非完整路径：授权的是"这个目录下的写入"，
        报告每次文件名不同，按完整路径记等于每次都问。"""
        parent = Path(verdict.target).parent.as_posix()
        return f"{call.name}:{parent}"

    def _emit_decision(
        self,
        call: ToolCall,
        *,
        turn: int,
        request_id: str,
        granted: bool,
        always: bool,
        by: str,
    ) -> None:
        self._emitter.emit(
            EventType.PERMISSION_DECISION,
            turn=turn,
            request_id=request_id,
            tool=call.name,
            granted=granted,
            always=always,
            by=by,
        )


# ---------------------------------------------------------------------- 消息
# 回灌给模型的两段文本，都要写成"接下来该怎么办"。尤其是拒绝后的第二句
# "不要再尝试"：省了它，模型会换个参数反复重试。


def _denied_message(call: ToolCall, verdict: Verdict, data_root: Path) -> str:
    return (
        f"`{call.name}` 被权限策略拒绝：{verdict.summary}。\n"
        f"只能读写数据目录 {data_root} 之内的文件，此目标在目录外，没有商量的余地。\n"
        "不要再尝试这个路径或换写法绕过 —— 请直接告诉用户你做不到，"
        "需要他手动处理。"
    )


def _rejected_message(call: ToolCall, req: PermissionRequest, by: str) -> str:
    if by == "no_channel":
        # 没人问过（--json / 无人值守）：不能写成"用户拒绝"，那是冤枉用户
        head = (
            f"`{call.name}` 未执行：当前运行模式没有可用的审批通道"
            "（如 --json 输出），写类操作一律不执行。"
        )
    else:
        head = f"用户拒绝了 `{call.name}`（{req.summary}）。"
    return (
        f"{head}请不要再尝试该操作。\n"
        "如果目标能不用写文件达成，请换方案；否则请直接告诉用户需要他手动完成什么。"
    )


# ---------------------------------------------------------------------- 辅助


def _path_arg(call: ToolCall) -> str:
    try:
        args: Any = json.loads(call.arguments or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(args, dict):
        return ""
    return str(args.get("path") or "").strip()


def _short(raw: str, limit: int = 200) -> str:
    text = (raw or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "WRITE_CAPABILITY",
    "Approval",
    "ApprovalChannel",
    "ApprovalMemory",
    "PermissionGate",
    "PermissionRequest",
    "Verdict",
    "decide",
    "new_request_id",
    "session_scope_dir",
]
