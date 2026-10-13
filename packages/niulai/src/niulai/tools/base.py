"""工具的定义与执行。

这一层负责五件事，缺任何一件 agent 都会变得不可靠：

1. 定义（schema）—— 描述的质量直接决定模型能不能选对工具；
2. 校验 —— 参数不合法要结构化报错，让模型自己改，而不是直接抛异常；
3. 超时 —— 一个卡住的工具能把整轮拖死；
4. 大结果落盘 —— 超过阈值就写文件，上下文里只留「摘要 + 路径」；
5. 能力声明 —— 工具自己声明它要碰什么（读 / 写 / 网络），但不判定。

第 4 条最容易被做错：只在"显示给用户"时截断，全量结果照样进 messages，
几轮之后上下文就爆了；更糟的是模型不知道自己拿到的是残数据，会基于
残缺内容给出看起来正确的结论。所以这里不截断，落盘。

第 5 条的设计要点是"声明与判定分离"：能不能用由 Registry 之外的策略层
决定（见 permissions.py）。被审对象不能当法官：一个外部 MCP server 说
"我很安全"是不算数的。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..artifacts import DEFAULT_INLINE_LIMIT, Artifact, ArtifactError, ArtifactStore
from ..llm.types import ToolCall, ToolSpec
from ..logs import get_logger

_log = get_logger("tools")

# 单个工具结果进入上下文的字符上限。超过就落盘成 artifact。
MAX_RESULT_CHARS = DEFAULT_INLINE_LIMIT

# 能力取值（工具自己声明，策略层判定）：
#   read        读本地数据（行情、文件）
#   network     发起出站请求 —— 没有域名白名单就是数据外泄通道
#   write_local 写本机文件
#   exec        执行命令
Capability = str
READ_ONLY: frozenset[str] = frozenset({"read", "network"})


@dataclass(slots=True)
class ToolResult:
    ok: bool
    content: str
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec
    # 见模块开头第 5 条。不强制所有工具都实现（缺省按只读对待），
    # 但凡是会写盘 / 出网的工具都必须如实声明。
    capabilities: frozenset[str]

    async def run(self, args: dict[str, Any]) -> str: ...


class ToolRegistry:
    """工具注册表 + 执行器。"""

    def __init__(
        self,
        tools: Iterable[Tool] = (),
        *,
        artifact_store: ArtifactStore | None = None,
        inline_limit: int = MAX_RESULT_CHARS,
    ) -> None:
        self._tools: dict[str, Tool] = {tool.spec.name: tool for tool in tools}
        self._artifacts = artifact_store
        self._inline_limit = inline_limit

    def add(self, tool: Tool) -> None:
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def capabilities(self, name: str) -> frozenset[str]:
        tool = self._tools.get(name)
        return frozenset(getattr(tool, "capabilities", None) or READ_ONLY)

    async def execute(self, call: ToolCall, *, timeout_s: float, run_id: str = "shared") -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                ok=False,
                content=(
                    f"没有名为 `{call.name}` 的工具。可用工具：{'、'.join(self.names())}。"
                    "请改用其中一个的准确名称重新调用。"
                ),
            )

        args, parse_error = _parse_arguments(call.arguments)
        if parse_error is not None:
            _log.debug("工具 %s 参数不合法：%s", call.name, parse_error)
            return ToolResult(ok=False, content=parse_error)

        missing = _missing_required(tool.spec.parameters, args)
        if missing:
            return ToolResult(
                ok=False,
                content=(
                    f"调用 `{call.name}` 缺少必填参数：{'、'.join(missing)}。"
                    f"该工具的参数定义：{json.dumps(tool.spec.parameters, ensure_ascii=False)}"
                ),
            )

        try:
            raw = await asyncio.wait_for(tool.run(args), timeout=timeout_s)
        except TimeoutError:
            _log.warning("工具 %s 超时（%.1f 秒）", call.name, timeout_s)
            return ToolResult(
                ok=False,
                content=(
                    f"工具 `{call.name}` 超过 {timeout_s:g} 秒未返回，已中止。"
                    "请缩小查询范围，或换用其他工具。"
                ),
            )
        except Exception as exc:  # noqa: BLE001 —— 工具异常必须转成可读提示，不能让 agent 崩掉
            _log.warning("工具 %s 异常：%s: %s", call.name, type(exc).__name__, exc)
            return ToolResult(
                ok=False,
                content=(
                    f"工具 `{call.name}` 执行出错：{type(exc).__name__}: {exc}。"
                    "请检查参数是否合理，或改用其他工具。"
                ),
            )

        content, artifact = self._prepare_result(raw, call.name, run_id=run_id)
        return ToolResult(
            ok=True,
            content=content,
            meta={
                "content_chars": len(raw),
                "artifact": artifact.ref if artifact else None,
            },
        )

    # ------------------------------------------------------------------ 内部

    def _prepare_result(
        self, raw: str, tool_name: str, *, run_id: str
    ) -> tuple[str, Artifact | None]:
        """结果太大就落盘，只把摘要送进上下文。"""
        if len(raw) <= self._inline_limit:
            return raw, None

        if self._artifacts is None:
            # 没有配置 store（极简用法 / 单测）：退回截断，但要显式标注
            text, _ = _truncate(raw, self._inline_limit)
            return text, None

        try:
            artifact = self._artifacts.save(raw, name=tool_name, run_id=run_id)
        except ArtifactError as exc:
            text, _ = _truncate(raw, self._inline_limit)
            return f"{text}\n（尝试落盘失败，已退回截断：{exc}）", None

        return _summarize_artifact(artifact, tool_name), artifact


def _summarize_artifact(artifact: Artifact, tool_name: str) -> str:
    head = _read_head(artifact.path, chars=400)
    return (
        f"`{tool_name}` 的结果过大（{artifact.chars:,} 字符 / {artifact.lines:,} 行），"
        "已完整保存为文件，**没有全文放进上下文**。\n"
        f"文件：{artifact.ref}\n"
        "开头预览（只是被截断的开头，不代表全部数据）：\n"
        f"---\n{head}\n---\n"
        f'如需完整内容，用 read_file(path="{artifact.ref}") 读取，'
        "可用 offset / limit 分页。"
    )


def _read_head(path: Path, *, chars: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(chars + 1)
    except OSError:
        return "(预览读取失败，请用 read_file 打开)"
    if len(text) > chars:
        return text[:chars] + "…"
    return text


def _parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """解析模型给的参数。失败时返回一段可恢复的指引，而不是抛异常。"""
    if not raw or not raw.strip():
        return {}, None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, (
            f"工具参数不是合法 JSON（{exc.msg}，位置 {exc.pos}）。"
            f"你给的是：{raw[:300]}。请重新生成一个合法的 JSON 对象。"
        )

    if not isinstance(parsed, dict):
        return {}, f"工具参数必须是 JSON 对象，收到的是 {type(parsed).__name__}。"
    return parsed, None


def _missing_required(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    required = schema.get("required") or []
    return [key for key in required if key not in args or args[key] in (None, "")]


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return (
        text[:limit] + f"\n\n[结果已截断：原始 {len(text):,} 字符，仅保留前 {limit:,} 字符。"
        "如需完整数据，请缩小查询范围。]",
        True,
    )
