"""报告子代理：把"写报告"从主对话里剥出去，委派给一个嵌套的 run。

值得一个子代理（而非主代理 inline 写）的理由：

1. 上下文卫生。inline 写时整篇报告以 tool_call.arguments 的形式永远留在
   主 transcript 里，之后每轮 fit_context 都要带着它走。委派后主会话只留
   一句"已写入 path（N 字符）"，报告本体在磁盘上，要读用 read_file。
2. prompt 独立版本化。报告的规矩（结构、引用、命名、读回自查）单独一份
   system_report_v1.md，可独立调优、独立进 eval。
3. 预算与权限隔离。子 run 有自己的轮数上限，工具面收窄为「只读数据工具
   + read_file + write_file」，每次 write_file 都过同一道权限门。

这不是"多 Agent 编排"：一个固定用途、同步调用、工具形态的子代理，编排
逻辑就是循环里的一次分发。同一个委派机制将来可服务盘后复盘等场景，
所以 ReportDelegate 的形态保持通用：一份 prompt + 一个工具工厂 + 一个
预算。

数据交接：brief 优先，缺的子代理自己取。它拿得到只读数据工具，prompt
规定"不得与 brief 矛盾、补取的数据要注明工具名与取数时间"，报告的
"数据来源"一节因此能带真实的 as_of。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..artifacts import ArtifactStore
from ..config import DEFAULT_REPORT_MAX_TURNS, Settings
from ..events import EventEmitter, SubAgentEmitter
from ..llm import LLMClient, Usage
from ..llm.types import ToolSpec
from ..paths import data_dir
from ..permissions import ApprovalChannel, ApprovalMemory
from ..tools import Tool, ToolRegistry
from .prompt import REPORT_PROMPT, load_prompt

if TYPE_CHECKING:  # 延迟导入：loop 反向依赖本模块（分发 write_report），模块级互导会成环
    from .loop import RunResult

REPORT_TOOL_NAME = "write_report"
REPORT_AGENT_NAME = "report"

# 子代理最终是写文件的，所以父层的权限门按 write_local 判定（路径来自参数）。
REPORT_CAPABILITIES = frozenset({"write_local"})

REPORT_TOOL_SPEC = ToolSpec(
    name=REPORT_TOOL_NAME,
    description=(
        "把当前分析整理成正式 Markdown 报告并落盘（由报告子代理执行，"
        "主对话只收回路径与小结，报告全文不进上下文）。\n"
        "适用：用户明确要求「总结一下 / 生成报告 / 整理成报告 / 存档」，"
        "或一次较厚的分析结束后用户确认要报告。\n"
        "不适用：日常问答与一句话总结——那些直接回答，不要走这个工具。\n"
        "参数：\n"
        "- brief：把本次分析的结论、关键数据（带数值与时间）、多视角要点"
        "完整写进来。子代理以 brief 为唯一事实来源，brief 没写的它不知道。\n"
        "- path：目标文件，相对数据根，形如 "
        "`sessions/<会话id>/<标的>-分析-<日期>.md`，"
        "用运行时上下文给出的输出目录做前缀。\n"
        "返回：路径、字符数、一句话小结。之后要读报告内容用 read_file。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "brief": {
                "type": "string",
                "description": (
                    "本次分析的完整结论：核心判断、各视角要点、关键数据"
                    "（数值 + 时间）、风险与反方证据。子代理只基于它写作。"
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "报告目标路径，相对数据目录，如 "
                    "`sessions/<会话id>/贵州茅台-分析-20261013.md`。"
                ),
            },
        },
        "required": ["brief", "path"],
        "additionalProperties": False,
    },
)


@dataclass(slots=True)
class ReportDelegate:
    """主 run 持有的委派器。

    `build_tools` 是工具工厂而不是现成列表：子代理的工具面要按调用时的
    数据目录构造（WriteFileTool 的围栏根在构造时解析），且每次委派都该拿
    全新实例 —— 工具对象不该跨 run 复用（状态污染）。
    """

    build_tools: Callable[[Path], list[Tool]]
    llm: LLMClient
    max_turns: int = DEFAULT_REPORT_MAX_TURNS

    def spec(self) -> ToolSpec:
        return REPORT_TOOL_SPEC


async def run_report(
    delegate: ReportDelegate,
    *,
    brief: str,
    path: str,
    emitter: EventEmitter,
    session_id: str | None,
    approval_channel: ApprovalChannel | None,
    approval_memory: ApprovalMemory,
    settings: Settings,
) -> tuple[str, Usage]:
    """跑一次报告子代理，返回（回灌给主代理的短结果, 子 run 用量）。

    子 run 与父 run 共用同一个 emitter（经 SubAgentEmitter 打标）——
    trace 是一条流，seq 连续，层级靠 `sub_agent` 标记分辨。
    """
    from .loop import AgentRuntime  # 延迟导入，见模块头 TYPE_CHECKING 注释

    data_root = data_dir()
    # 子代理自己的一份 store：root 在调用时解析（与父同级目录）。数据工具的
    # 大结果照旧落盘 + read_file 读回——子代理不享受"截断"待遇。
    store = ArtifactStore()
    tools = ToolRegistry(delegate.build_tools(data_root), artifact_store=store)
    runtime = AgentRuntime(
        settings=replace(settings, max_turns=delegate.max_turns),
        llm=delegate.llm,
        tools=tools,
        system_prompt=load_prompt(REPORT_PROMPT),
        approval_channel=approval_channel,
        approval_memory=approval_memory,
    )

    task = (
        "请根据下面的分析结论撰写一份 Markdown 报告，并写入指定路径。\n\n"
        f"目标路径（只能写这一个文件）：{path}\n\n"
        f"分析结论：\n{brief}"
    )

    result = await runtime.run(
        task,
        emitter=SubAgentEmitter(emitter, REPORT_AGENT_NAME),
        session_id=session_id,
        sub_agent=REPORT_AGENT_NAME,
    )

    return _parent_facing_result(path, data_root, result), result.usage


def _parent_facing_result(path: str, data_root: Path, result: RunResult) -> str:
    """主代理看到的结果。刻意短：报告全文已经在文件里，不回灌。"""
    target = (data_root / path).resolve()
    if target.is_file():
        size = f"{len(target.read_text(encoding='utf-8', errors='replace')):,} 字符"
    else:
        # 子代理可能没来得及写（轮数用完 / 被拦）。照实说，不假装成功。
        size = f"未确认落盘（子代理结束原因：{result.stop_reason}）"

    summary = " ".join((result.text or "").split())[:200]
    return (
        f"报告子代理已完成：{path}（{size}）。\n"
        f"子代理小结：{summary or '（无）'}\n"
        f'报告全文不在对话里，需要时用 read_file(path="{path}") 读回。'
    )


__all__ = [
    "REPORT_AGENT_NAME",
    "REPORT_CAPABILITIES",
    "REPORT_TOOL_NAME",
    "REPORT_TOOL_SPEC",
    "ReportDelegate",
    "run_report",
]
