"""写文件工具：报告与产出的落地口。

契约与 read_file 一致：一个根、一种形式 —— `path` 相对数据根 `~/.niulai/`。
当前会话的输出目录（sessions/<会话id>/）由运行时每轮注入系统提示，模型
照抄前缀即可，不需要理解任何内部目录结构。

围栏在工具里也有一份：策略门（permissions.py）先判定放行 / 询问 / 拒绝，
这里是第二道 —— 不信任调用方：直接调 run() 也写不出去，越界返回一段
可恢复的错误说明，由模型转告用户"做不到"，而不是把异常抛穿。

覆盖即修改。已存在的文件会被直接覆盖 —— 要不要覆盖由策略门去问用户
（modify_existing），工具不重复判定，只保证"被允许的事一定做得到"。

能力声明是 `write_local`，不含 read / network —— 它不出网、不读盘。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..llm.types import ToolSpec
from ..paths import data_dir

WRITE_FILE_TOOL_NAME = "write_file"

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "目标文件，相对数据目录的路径，形如 "
                "`sessions/<会话id>/贵州茅台-分析-20261012.md`。"
                "当前会话的输出目录前缀每轮由系统给出，直接照抄；"
                "父目录不存在会自动创建。只能写数据目录之内的路径。"
            ),
        },
        "content": {
            "type": "string",
            "description": "要写入的完整内容（Markdown 报告或任何文本）。",
        },
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


class WriteFileTool:
    """把内容写进数据目录（默认是当前会话目录）。"""

    capabilities = frozenset({"write_local"})

    def __init__(self, *, data_root: Path | None = None) -> None:
        # 与 ReadFileTool 同一个注入点：默认 ~/.niulai，测试可指到临时目录
        self._data_root = data_root or data_dir()
        self.spec = ToolSpec(
            name=WRITE_FILE_TOOL_NAME,
            description=(
                "把文本内容写入数据目录内的文件（自动创建父目录）。\n"
                "适用：① 生成分析报告并存档；② 保存任何需要留底的文本产出。\n"
                "路径用相对数据目录的形式，当前会话的输出目录前缀"
                "（sessions/<会话id>/）以系统每轮给出的为准。\n"
                "注意：覆盖已存在的文件、写到会话目录之外，都会先征得用户同意；"
                "数据目录之外的路径一律拒绝。被用户拒绝时不要重试，"
                "直接把内容贴给用户让他手动保存。"
            ),
            parameters=_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        raw = str(args.get("path") or "").strip()
        content = args.get("content")

        if not raw:
            return "缺少 path 参数。请给出相对数据目录的路径，如 `sessions/<会话id>/报告.md`。"
        if not isinstance(content, str) or not content:
            return "缺少 content 参数（要写入的文本）。报告正文放在 content 里，path 只放路径。"

        root = self._data_root.resolve()
        resolved = (root / raw).resolve()

        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError:
            return (
                f"拒绝写入 `{raw}`：只能写数据目录 {root} 之内的文件。\n"
                "目录之外的写入需要用户逐次确认，该能力尚未实现——"
                "请直接告诉用户你做不到，不要换路径重试。"
            )

        if resolved.is_dir():
            return f"`{rel}` 已存在且是目录，不能当文件写入。请换一个文件名。"

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"写入 `{rel}` 失败：{exc}。请检查路径是否合法，或换一个文件名重试。"

        return (
            f"已写入 {rel}（{len(content):,} 字符）。\n"
            f'需要读回时用 read_file(path="{rel}")。'
        )


__all__ = ["WRITE_FILE_TOOL_NAME", "WriteFileTool"]
