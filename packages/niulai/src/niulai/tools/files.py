"""读文件工具。

它存在的理由只有一个：**大结果落盘之后必须能读回来。**

只落盘不配读，数据就进了黑洞 —— 模型知道"有个文件里有完整数据"，
但拿不到，于是只能基于 400 字的预览下结论。这比不落盘还糟。

**这是全项目第一个带边界限制的工具**，也是权限控制（architecture.md 7.12）
的第一块落地：它只能读 artifacts 目录里的文件，越界一律拒绝。
将来加写文件、执行命令的工具时，走的是同一套「工具声明能力、策略层判定」的模式。

注意它的能力声明是 `read`，**不含 `network`** —— 它不出网。
"""

from __future__ import annotations

from typing import Any

from ..artifacts import ArtifactError, ArtifactStore
from ..llm.types import ToolSpec

READ_FILE_TOOL_NAME = "read_file"

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 2000

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "要读的文件路径，通常是工具结果里给出的那个引用（形如 "
                "`20260922T213045-a1b2/stock_kline-3f9c1a2b.csv`）。"
                "不传这个参数则列出当前所有可读文件。"
            ),
        },
        "offset": {
            "type": "integer",
            "description": "从第几行开始读（从 1 开始计数），默认 1。用于分页。",
        },
        "limit": {
            "type": "integer",
            "description": f"最多读多少行，默认 {_DEFAULT_LIMIT}，最大 {_MAX_LIMIT}。",
        },
    },
    "additionalProperties": False,
}


class ReadFileTool:
    """读取之前落盘的大结果，支持按行分页。"""

    capabilities = frozenset({"read"})

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self.spec = ToolSpec(
            name=READ_FILE_TOOL_NAME,
            description=(
                "读取之前**因体积过大而落盘**的工具结果，支持按行分页。\n"
                "适用：工具返回里出现「结果过大，已完整保存为文件」并给了路径时，"
                "用它取完整数据。也可不传 path 列出全部可读文件。\n"
                "不适用：读用户电脑上的任意文件 —— 本工具**只能读本程序自己落盘的结果**，"
                "路径越界会被拒绝。用户让你看别的文件时，直接说明做不到。\n"
                "分页建议：先读前 200 行判断结构，再按需要跳读；"
                "不要一次把几万行灌进上下文，那会把落盘省下的空间又还回去。"
            ),
            parameters=_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        path = str(args.get("path") or "").strip()

        if not path:
            return self._render_index()

        offset = _as_int(args.get("offset"), default=1, low=1, high=10_000_000)
        limit = _as_int(args.get("limit"), default=_DEFAULT_LIMIT, low=1, high=_MAX_LIMIT)

        try:
            text, total, start = self._store.read(path, offset=offset, limit=limit)
        except ArtifactError as exc:
            # 路径越界 / 找不到：回灌成可恢复的指引，让模型自己改
            return f"{exc}\n（可以调用 read_file 不传 path 来列出全部可读文件。）"

        end = min(start + text.count("\n"), total) if text else start
        header = f"{path}　第 {start}–{end} 行 / 共 {total} 行"
        if end < total:
            header += f"　（还有 {total - end} 行未显示，用 offset={end + 1} 继续读）"
        return f"{header}\n{text}"

    def _render_index(self) -> str:
        items = self._store.list_all()
        if not items:
            return (
                "目前没有落盘的文件。"
                "（只有当某个工具结果超过体积阈值时才会落盘，届时路径会写在工具返回里。）"
            )
        lines = [f"可读文件共 {len(items)} 个（按修改时间倒序）："]
        lines.extend(f"  · {item.describe()}" for item in items)
        lines.append('用 read_file(path="上面某个路径") 打开。')
        return "\n".join(lines)


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


__all__ = ["READ_FILE_TOOL_NAME", "ReadFileTool"]
