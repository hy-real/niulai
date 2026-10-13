"""读文件工具：给路径，分页读回。

契约：path 用相对数据根 ~/.niulai/ 的形式（如
artifacts/<run_id>/stock_kline-3f9c1a2b.csv）。系统发给模型的所有文件
引用都是这个形式，模型照抄即可，无需理解内部目录结构。目录内的绝对路径
也接受（归一化后同样过围栏）。相对而非绝对：引用会存进会话 JSON，相对
形式在数据目录搬家后依然有效，也不会把用户名写进 trace。

边界 = 数据目录。目录之内是程序自己生成的文件，读它不需要审批；目录之外
属于用户的其他数据，一律拒绝（那类请求走审批通道，见 permissions.py）。

ArtifactStore 在这里是旧引用兼容层：旧会话里存的引用是
<run_id>/<文件名>（相对 artifacts 子目录），只有 store 认得。对模型
不可见。

能力声明是 read，不含 network —— 它不出网、不写盘。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactError, ArtifactStore
from ..config import CONFIG_FILE_NAME
from ..llm.types import ToolSpec
from ..paths import data_dir

READ_FILE_TOOL_NAME = "read_file"

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 2000
_MAX_CHARS = 8_000_000

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "要读的文件，相对数据目录的路径——工具结果里给出的引用"
                "（形如 `artifacts/20260922T213045-a1b2/stock_kline-3f9c1a2b.csv`）"
                "照抄即可；也可以读 traces/、sessions/、logs/、config.toml。"
                "不传则列出落盘的文件。"
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


def resolve_in_data_root(raw: str, data_root: Path | None = None) -> Path:
    """把引用解析成真实路径：相对数据根（或目录内绝对路径）→ 过围栏。

    越界抛 `ArtifactError`。**read_file 工具与 Web 文件面板共用这一份围栏** ——
    同一道边界写两遍，迟早有一处漏。
    """
    root = (data_root or data_dir()).resolve()
    resolved = (root / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ArtifactError(
            f"拒绝访问 `{raw}`：只能读写数据目录 {root} 之内的文件"
        ) from exc
    return resolved


def read_display_text(path: Path, *, max_chars: int = 1_000_000) -> str:
    """读文本用于界面展示：封顶体量 + config.toml 自动脱敏。

    Key 脱敏在这里再做一次（read_file 里已有一份）：界面预览是另一条出口，
    规矩不因为"只是看看"而放松。
    """
    text = _read_capped(path, max_chars=max_chars)
    if path.name == CONFIG_FILE_NAME:
        text = _mask_api_key(text)
    return text


class ReadFileTool:
    """读取数据目录内的文件（落盘结果 / trace / 会话 / 日志），支持按行分页。"""

    capabilities = frozenset({"read"})

    def __init__(self, store: ArtifactStore, *, data_root: Path | None = None) -> None:
        self._store = store
        # 边界注入点：默认 ~/.niulai；将来策略引擎接管判定时，这里是
        # "转成 permission_request 事件"的挂钩位（见 permissions.py）。
        self._data_root = data_root or data_dir()
        self.spec = ToolSpec(
            name=READ_FILE_TOOL_NAME,
            description=(
                "读取本程序自己生成的文件，支持按行分页。\n"
                "适用：① 工具返回里出现「结果过大，已完整保存为文件」时，"
                "把给出的路径原样传入；② 读数据目录内的其他文件"
                "（`traces/` 运行轨迹、`sessions/` 会话、`logs/`、`config.toml`"
                "——其中 api_key 会自动脱敏）。\n"
                "不适用：读用户电脑上的任意文件 —— 数据目录**之外**一律拒绝"
                "（用户让你看别的文件时，直接说明做不到）。\n"
                "分页建议：先读前 200 行判断结构，再按需要跳读；"
                "不要一次把几万行灌进上下文。"
            ),
            parameters=_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        path = str(args.get("path") or "").strip()

        if not path:
            return self._render_index()

        offset = _as_int(args.get("offset"), default=1, low=1, high=10_000_000)
        limit = _as_int(args.get("limit"), default=_DEFAULT_LIMIT, low=1, high=_MAX_LIMIT)

        return self._read(path, offset=offset, limit=limit)

    # ------------------------------------------------------------------ 内部

    def _read(self, raw: str, *, offset: int, limit: int) -> str:
        """规范路径解析：相对数据根（或目录内绝对路径）→ 围栏 → 分页读。

        唯一的非规范入口是老会话里的旧引用（`<run_id>/<文件名>`，相对
        artifacts 子目录）——交给 store 兜底，对模型不可见，仅作过渡。
        """
        try:
            # 先归一化再进围栏：绝对路径若恰好落在目录内也放行
            resolved = resolve_in_data_root(raw, self._data_root)
        except ArtifactError:
            # 围栏拒绝：明确边界 + 给出路。目录外的读取将来走审批通道，现在没有
            # 这条路，所以如实地告诉模型"做不到"。
            return (
                f"拒绝访问 `{raw}`：只能读取数据目录 {self._data_root.resolve()} 内的文件"
                "（程序自己生成的 trace / 会话 / 落盘结果 / 日志）。\n"
                "目录之外的文件需要用户逐次确认，该能力尚未实现——"
                "请直接说明做不到，不要换路径重试。\n"
                "（可以调用 read_file 不传 path 来列出落盘的文件。）"
            )

        if resolved.is_file():
            text = _read_capped(resolved)
            if "\x00" in text[:1000]:
                return f"`{raw}` 看起来是二进制文件，不支持以文本方式读取。"
            if resolved.name == CONFIG_FILE_NAME:
                # config.toml 可能存着 api_key（webapi 设置面板会写入）。
                # 工具结果会进入模型上下文、随下一次请求发到模型厂商 ——
                # 明文 Key 绝不能走这条路。只替换值本身，其余配置照常可读。
                text = _mask_api_key(text)
            chunk, total, start = _paginate(text, offset, limit)
            return self._render(raw, chunk, total, start)

        # 规范形式没找到 → 老引用兼容（store 的解析含松散匹配，仅此一处）
        try:
            text, total, start = self._store.read(raw, offset=offset, limit=limit)
            return self._render(raw, text, total, start)
        except ArtifactError:
            pass

        return (
            f"找不到文件：`{raw}`。\n"
            "要用相对路径（相对数据目录），如 `traces/xxx.jsonl`、`sessions/xxx.json`；"
            "落盘引用直接照抄工具结果里给出的那个。\n"
            "（可以调用 read_file 不传 path 来列出落盘的文件。）"
        )

    def _render(self, ref: str, text: str, total: int, start: int) -> str:
        end = min(start + text.count("\n"), total) if text else start
        header = f"{ref}　第 {start}–{end} 行 / 共 {total} 行"
        if end < total:
            header += f"　（还有 {total - end} 行未显示，用 offset={end + 1} 继续读）"
        return f"{header}\n{text}"

    def _render_index(self) -> str:
        items = self._store.list_all()
        if not items:
            return (
                "目前没有落盘的文件。"
                "（只有当某个工具结果超过体积阈值时才会落盘，届时路径会写在工具返回里。）\n"
                "也可以按相对路径直接读数据目录内的其他文件："
                "traces/（运行轨迹）、sessions/（会话）、config.toml。"
            )
        lines = [f"可读文件共 {len(items)} 个（按修改时间倒序）："]
        lines.extend(f"  · {item.describe()}" for item in items)
        lines.append('用 read_file(path="上面某个路径") 打开。')
        lines.append(
            "其他数据文件也可按相对路径读取：traces/、sessions/、logs/、config.toml。"
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------- 辅助


def _paginate(text: str, offset: int, limit: int) -> tuple[str, int, int]:
    """行分页。返回 (本次内容, 总行数, 起始行)。与 ArtifactStore.read 同一语义。"""
    lines = text.splitlines()
    total = len(lines)
    start = max(0, offset - 1)
    start = min(start, max(0, total - 1)) if total else 0
    chunk = lines[start : start + max(1, limit)]
    return "\n".join(chunk), total, start + 1


def _read_capped(path: Path, *, max_chars: int = _MAX_CHARS) -> str:
    """读文本、封顶体量。坏字节用替换符而不是抛错——磁盘上的字节不归我们保证。"""
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return fh.read(max_chars)


# api_key = "sk-..." / api_key = 'sk-...' → 值打码，行结构不动
_SECRET_RE = re.compile(r"(?i)(api_key\s*=\s*)(['\"])([^'\"]*)\2")


def _mask_api_key(text: str) -> str:
    return _SECRET_RE.sub(r"\1\2***已脱敏***\2", text)


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


__all__ = ["READ_FILE_TOOL_NAME", "ReadFileTool", "read_display_text", "resolve_in_data_root"]
