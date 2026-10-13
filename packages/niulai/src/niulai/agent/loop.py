"""手写 ReAct 内核。

这是整个项目的核心，也是"为什么值得手写"的答案：
循环本身只有几十行，但预算、取消、截断、错误回灌这些细节全在这里。
上了框架，这些恰好会被藏起来 —— 而它们正是面试会被追问的东西。

四个停止条件，缺一不可：

1. 模型不再要求调用工具 → 正常结束
2. 达到轮数上限        → 强制收敛（最后一轮不让调工具）
3. 外部取消            → 取消也是**收尾**：返回 stop_reason=cancelled 的半成品结果
4. 运行时熔断          → 重复 / 连续失败的调用被拦下，逼模型收敛

第 4 条不自己造"取消标志位"：`asyncio` 的任务取消会逐层透传，
自己加一套标志反而最容易漏掉某一层。

## 权限门：写类工具过闸，只读过站

`_run_tools` 的阶段一是三道串行检查：熔断（guard）→ 权限（gate）→ 执行。
权限门对只读工具一律放行；写类工具命中"要问"时发出 `permission_request`
事件并 await 外壳答复（CLI 弹 y/n、Web 弹窗）—— 审批是事件不是同步 if，
所以无人值守时 fail-safe 默认拒绝。被拦下的调用照样回灌一条 tool 消息，
内容写成"不要再尝试"（见 permissions.py 与 architecture.md 7.14）。

## 子代理：write_report 是内核级虚拟工具

报告委派**不走 ToolRegistry**：`write_report` 的 spec 由循环动态注入给模型
（装配了 ReportDelegate 才注入），调用由循环分发到 `agent/report.py` 的嵌套
run。这样注册表保持"数据工具执行器"的单一职责，而委派所需的每次 run 状态
（emitter / session_id / 审批通道）天然就在循环手里。子 run 的事件经
SubAgentEmitter 打标汇入同一条流，seq 连续；用量合并回父 run 的统计。

## transcript 是入口，不是内部细节

`run()` 接受一份已有的 messages 并在后面追加，而不是每次从零构建。
这一个改动同时买到两件事：

- **多轮会话**：用户说"它今天成交多少"能指代上一轮那只股票；
- **断点恢复**：崩溃后接着跑同一条 run。

两者共用同一个入口，所以恢复能力不用再实现第二遍。

（命名注意：这里的 `turn` 是 **ReAct 轮** —— 一次 LLM 调用算一轮。
对话语境里的"一轮"通常指用户的一次提问，那对应一次 `run`。
两者数量完全不同（一次提问可能跑 5 个 ReAct 轮），混用会让预算统计全乱。）
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ..config import Settings
from ..context import fit_context
from ..events import EventEmitter, EventType
from ..llm import LLMClient, LLMResponse, Message, Usage
from ..llm.types import ToolCall
from ..permissions import (
    ApprovalChannel,
    ApprovalMemory,
    PermissionGate,
    session_scope_dir,
)
from ..tools import ToolRegistry, ToolResult
from ..tools.base import _missing_required, _parse_arguments
from .guard import ToolGuard
from .prompt import load_prompt
from .report import (
    REPORT_CAPABILITIES,
    REPORT_TOOL_NAME,
    REPORT_TOOL_SPEC,
    ReportDelegate,
    run_report,
)

FORCE_ANSWER_PROMPT = (
    "本轮的工具调用额度已经用完。请立刻基于上面已经获得的信息回答用户，不要再调用工具。"
    "如果信息不足以回答，就直接说明还缺什么。"
)

# 工具结果写进事件流时的预览长度（完整内容仍然在 messages 里）
RESULT_PREVIEW_CHARS = 600

# 中断后补的那条 tool 结果。**不能假定它成功或失败** ——
# 工具可能已经发出了请求、只是结果没回来。对只读行情无所谓，
# 对写类工具这就是事故（见 architecture.md 7.13 的幂等一节）。
INTERRUPTED_RESULT = (
    "[这次工具调用被中断了，结果未知。不要假定它成功或失败 —— "
    "如果这份数据仍然需要，请重新调用一次。]"
)

# 同一轮多个 tool_calls 的并发执行上限。数据源（东财）有限流，不能无脑并发；
# 偶发 429 由数据源层的限流重试兜住，这里只负责"别自己打自己"。
MAX_PARALLEL_TOOL_CALLS = 4


@dataclass(slots=True)
class RunResult:
    text: str
    turns: int
    usage: Usage
    stop_reason: str  # done | max_turns | cancelled
    messages: list[Message] = field(default_factory=list)
    # 被运行时拦下的调用次数（熔断）。>0 说明模型试图空转。
    breaks: int = 0


class AgentRuntime:
    """一次运行的完整编排：消息、循环、预算、事件。"""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMClient,
        tools: ToolRegistry,
        system_prompt: str | None = None,
        approval_channel: ApprovalChannel | None = None,
        approval_memory: ApprovalMemory | None = None,
        report_delegate: ReportDelegate | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._tools = tools
        self._system_prompt = system_prompt if system_prompt is not None else load_prompt()
        # 审批通道（外壳提供）。None = 无人可问，命中 ask 一律按拒绝处理
        # （fail-safe，见 permissions.py）。CLI 在构造时给；Web 的通道是
        # 每 run 一个，走 run() 的 approval_channel 参数。
        self._approval_channel = approval_channel
        # "总是允许"的会话级记忆：按会话隔离、跨 run 有效、重启即忘
        self._approval_memory = approval_memory or ApprovalMemory()
        # 报告子代理委派器。None = 未装配（测试 / 极简用法）：模型看不到
        # write_report，也不会被分发 —— 功能是"装配即有"，不是永远在。
        self._report_delegate = report_delegate

    async def run(
        self,
        task: str,
        *,
        emitter: EventEmitter,
        messages: Sequence[Message] | None = None,
        session_id: str | None = None,
        approval_channel: ApprovalChannel | None = None,
        sub_agent: str | None = None,
    ) -> RunResult:
        """跑一次任务。

        `messages` 是**已有的对话历史**（不含 system）。传进来就续写，
        不传就是一次全新任务。调用方拿 `RunResult.messages` 作为下一次的输入。

        `approval_channel` 是**本 run 专用**的审批通道（Web 外壳每 run 建一个，
        因为 SSE 连接是每 run 一条）；不传则用构造时给的（CLI 的交互通道）。

        `sub_agent` 非空表示这是**子 run**（被父 run 委派）：生命周期事件
        从 RUN_START / RUN_END 换成 SUB_RUN_START / SUB_RUN_END——
        一条 trace 里只能有一个 run 的开始和结束，子 run 是其中的一段。
        """
        transcript = list(messages) if messages else []
        resumed = bool(transcript)
        # 运行时上下文（会话 id / 输出目录）每轮注入 system：会话里存的是
        # 完整 transcript，system 不落库，所以这里注入不会污染历史
        _ensure_system(
            transcript,
            self._system_prompt,
            note=_runtime_note(session_id, emitter.run_id),
        )
        repaired = _repair_transcript(transcript)
        transcript.append(Message(role="user", content=task))

        usage = Usage()
        final_text = ""
        stop_reason = "done"
        turn = 0
        omitted_total = 0
        guard = ToolGuard(max_consecutive_failures=self._settings.max_consecutive_failures)
        channel = approval_channel or self._approval_channel
        gate = PermissionGate(
            emitter=emitter,
            channel=channel,
            memory=self._approval_memory,
            session_id=session_id,
            run_id=emitter.run_id,
        )

        # 子代理用量记账：列表而不是返回值，因为委派发生在 _run_tools 内部的
        # 并行任务里；单个 run 内只有委派任务会追加，无并发写问题。
        sub_usage: list[Usage] = []

        report_runner: Callable[[ToolCall], Awaitable[ToolResult]] | None = None
        if self._report_delegate is not None:
            delegate = self._report_delegate

            async def report_runner(call: ToolCall) -> ToolResult:
                # 参数校验在派发点做（与 ToolRegistry.execute 同一套 helper、
                # 同一种可恢复错误文案）—— 模型缺 brief / path 时立刻拿到
                # 结构化反馈，而不是进了子代理才失败。
                args, parse_error = _parse_arguments(call.arguments)
                if parse_error is not None:
                    return ToolResult(ok=False, content=parse_error)
                missing = _missing_required(REPORT_TOOL_SPEC.parameters, args)
                if missing:
                    schema = json.dumps(REPORT_TOOL_SPEC.parameters, ensure_ascii=False)
                    return ToolResult(
                        ok=False,
                        content=(
                            f"调用 `{REPORT_TOOL_NAME}` 缺少必填参数：{'、'.join(missing)}。"
                            f"该工具的参数定义：{schema}"
                        ),
                    )
                content, extra = await run_report(
                    delegate,
                    brief=str(args.get("brief") or ""),
                    path=str(args.get("path") or ""),
                    emitter=emitter,
                    session_id=session_id,
                    approval_channel=channel,
                    approval_memory=self._approval_memory,
                    settings=self._settings,
                )
                sub_usage.append(extra)
                return ToolResult(ok=True, content=content, meta={"content_chars": len(content)})

        if sub_agent is None:
            emitter.emit(
                EventType.RUN_START,
                task=task,
                model=self._settings.model,
                protocol=self._settings.protocol,
                tool_names=self._tools.names(),
                session_id=session_id,
                resumed=resumed,
                repaired_messages=repaired,
            )
        else:
            emitter.emit(
                EventType.SUB_RUN_START,
                task=task,
                sub_agent=sub_agent,
                model=self._settings.model,
                session_id=session_id,
                tool_names=self._tools.names(),
            )
        streamed: list[str] = []       # 本轮流式输出的累计 —— 取消时用它留住半截回答
        assistant_recorded = False     # 本轮 assistant 消息是否已写入 transcript

        try:
            for turn in range(1, self._settings.max_turns + 1):
                force_answer = turn == self._settings.max_turns
                streamed.clear()
                assistant_recorded = False

                # 发出去的是一份裁剪视图；transcript 本身始终完整。
                view, context_stats = fit_context(
                    transcript,
                    keep_recent_tool_results=self._settings.keep_recent_tool_results,
                )
                omitted_total += context_stats.omitted

                emitter.emit(
                    EventType.TURN_START,
                    turn=turn,
                    tools_enabled=not force_answer,
                    context_omitted=context_stats.omitted,
                )

                if force_answer:
                    # 收口指令只追加在最后一轮，不污染正常轮次
                    view = view + [Message(role="user", content=FORCE_ANSWER_PROMPT)]

                response = await self._call_llm(
                    view, emitter, allow_tools=not force_answer, text_sink=streamed
                )
                usage = usage.merged(response.usage)
                emitter.emit(EventType.USAGE, turn=turn, cumulative=usage.to_dict())

                if force_answer:
                    # 收口轮不记录 tool_calls，**也不透传 raw_blocks**：
                    # transcript 不允许以"未执行的工具调用"结尾，否则续跑时协议不合法。
                    # Anthropic 的 raw_blocks 里就含着 tool_use 块，一并丢掉才对。
                    transcript.append(Message(role="assistant", content=response.text))
                    assistant_recorded = True
                    emitter.emit(EventType.ASSISTANT_MESSAGE, turn=turn, text=response.text)
                    final_text = response.text
                    stop_reason = "max_turns"
                    break

                transcript.append(
                    Message(
                        role="assistant",
                        content=response.text,
                        tool_calls=response.tool_calls or None,
                        # Anthropic 的 thinking / redacted_thinking 块必须原样回传，
                        # 否则续跑时 400。内核不解读它，只是搬一次 —— 这一句是
                        # "两套协议一套内核"能成立的关键：多协议的差异全在客户端，
                        # 内核只负责别把它弄丢。
                        raw_blocks=response.raw_blocks,
                    )
                )
                assistant_recorded = True
                emitter.emit(
                    EventType.ASSISTANT_MESSAGE,
                    turn=turn,
                    text=response.text,
                    # id 必须进 journal：恢复时靠它把 tool_call 与结果对上账
                    tool_calls=[
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                )

                if not response.wants_tools:
                    final_text = response.text
                    stop_reason = "done"
                    break

                await self._run_tools(
                    response,
                    transcript,
                    emitter,
                    turn=turn,
                    guard=guard,
                    gate=gate,
                    report_runner=report_runner,
                )
                for extra in sub_usage:
                    usage = usage.merged(extra)
                sub_usage.clear()

            emitter.emit(EventType.FINAL, text=final_text, stop_reason=stop_reason)
            return RunResult(
                text=final_text,
                turns=turn,
                usage=usage,
                stop_reason=stop_reason,
                messages=transcript,
                breaks=len(guard.breaks),
            )

        except asyncio.CancelledError:
            # 取消也是一种**正常收尾**，不再向上抛：调用方（webapi / CLI）拿到
            # 半成品 RunResult 才能把"用户已经看到一半的对话"落进会话。
            # 取消的传播机制不变 —— 仍是 asyncio 任务取消逐层打断 LLM 流与工具，
            # 只是终点从"异常退出"改成"带结果的退出"。
            stop_reason = "cancelled"
            emitter.emit(EventType.ERROR, kind="CancelledError", message="运行被取消")
            partial = "".join(streamed)
            if partial and not assistant_recorded:
                # 正在流式输出的那句话还没入 transcript：把半截留住，
                # 否则下次打开会话它就消失了（用户亲眼看过它）。
                transcript.append(Message(role="assistant", content=partial))
                emitter.emit(EventType.ASSISTANT_MESSAGE, turn=turn, text=partial)
            emitter.emit(EventType.FINAL, text=partial, stop_reason=stop_reason)
            return RunResult(
                text=partial,
                turns=turn,
                usage=usage,
                stop_reason=stop_reason,
                messages=transcript,
                breaks=len(guard.breaks),
            )

        finally:
            if sub_agent is None:
                emitter.emit(
                    EventType.RUN_END,
                    stop_reason=stop_reason,
                    turns=turn,
                    usage=usage.to_dict(),
                    context_omitted=omitted_total,
                    breaks=len(guard.breaks),
                )
            else:
                emitter.emit(
                    EventType.SUB_RUN_END,
                    sub_agent=sub_agent,
                    stop_reason=stop_reason,
                    turns=turn,
                    usage=usage.to_dict(),
                )

    async def _run_tools(
        self,
        response: LLMResponse,
        messages: list[Message],
        emitter: EventEmitter,
        *,
        turn: int,
        guard: ToolGuard,
        gate: PermissionGate,
        report_runner: Callable[[ToolCall], Awaitable[ToolResult]] | None = None,
    ) -> None:
        """执行这一轮声明的全部 tool_calls，并把结果回灌进 messages。

        **检查串行、执行并行**：先按声明顺序逐个过熔断（guard 的状态是顺序
        敏感的 —— 同一批里完全重复的调用，第二个就该被拦下），再过权限门
        （ask 要等人答复，天然只能一个一个来），放行的调用经信号量限流后
        并发执行（asyncio.gather）。

        `write_report` 是唯一的非数据工具：它不过 ToolRegistry，由循环分发到
        报告子代理（report_runner）。子代理内部是一个完整的嵌套 run，慢
        （多轮 LLM + 写文件），所以**不吃** MAX_PARALLEL_TOOL_CALLS 信号量
        —— 那把伞是给数据源的限流重试配的，罩不住一个子代理。

        结果消息的回灌顺序只由**模型声明的顺序**决定，与完成先后无关：
        TOOL_RESULT 事件随完成实时发出（前端按 tool_call_id 归位），
        transcript 在全部落定后按声明顺序统一追加。

        **每个 tool_call 都必须回灌一条消息**，哪怕是拦下来的 ——
        assistant 已经声明了这些调用，少一条就是协议非法。
        """
        semaphore = asyncio.Semaphore(MAX_PARALLEL_TOOL_CALLS)

        async def _execute(call: ToolCall):
            async with semaphore:
                result = await self._tools.execute(
                    call, timeout_s=self._settings.tool_timeout_s, run_id=emitter.run_id
                )
            # record 只改内存 dict、无 await，事件循环内天然原子，不会丢更新。
            # 并行批内同工具"一败一成"时计数按完成顺序落 —— 并发下"连续"
            # 本无全序，偏差方向只会偏保守（早拦一次），不会偏失控（见 guard.py）
            guard.record(call, ok=result.ok)
            _emit_tool_result(emitter, turn=turn, call=call, result=result)
            return result

        async def _execute_report(call: ToolCall) -> ToolResult:
            assert report_runner is not None  # 仅在派发点判定后才被创建
            result = await report_runner(call)
            guard.record(call, ok=result.ok)
            _emit_tool_result(emitter, turn=turn, call=call, result=result)
            return result

        # 阶段一（串行）：声明顺序过熔断、过权限门，放行的立刻起任务。
        # 这里可能有 await（审批等人），取消时要把已起的任务收掉，
        # 否则它们会变成脱离本 run 的孤儿。
        plans: list[tuple[ToolCall, asyncio.Task | None, Message | None]] = []
        try:
            for call in response.tool_calls:
                emitter.emit(
                    EventType.TOOL_CALL, turn=turn, name=call.name, arguments=call.arguments,
                    tool_call_id=call.id,
                )

                breaker = guard.check(call)
                if breaker is not None:
                    # 熔断：不执行，但照样回灌。内容写成"别再试了，改走别的路"。
                    emitter.emit(
                        EventType.CIRCUIT_BREAK,
                        turn=turn,
                        name=call.name,
                        reason=breaker.reason,
                        message=breaker.message,
                        tool_call_id=call.id,
                    )
                    plans.append((
                        call,
                        None,
                        Message(
                            role="tool",
                            content=breaker.message,
                            tool_call_id=call.id,
                            name=call.name,
                            is_error=True,
                        ),
                    ))
                    continue

                # write_report 是内核级虚拟工具，不在 registry 里 —— 它的
                # 能力声明（write_local）由循环直接给出，否则 capabilities()
                # 会按"查无此工具"退化成只读，权限门将永远放行。
                capabilities = (
                    REPORT_CAPABILITIES
                    if call.name == REPORT_TOOL_NAME
                    else self._tools.capabilities(call.name)
                )
                blocked = await gate.check(call, capabilities, turn=turn)
                if blocked is not None:
                    # 被权限门拦下（deny / 用户拒绝）：不执行，照样回灌。
                    # 内容写成"不要再尝试"—— 否则模型会换个参数反复重试。
                    plans.append((
                        call,
                        None,
                        Message(
                            role="tool",
                            content=blocked,
                            tool_call_id=call.id,
                            name=call.name,
                            is_error=True,
                        ),
                    ))
                    continue

                if call.name == REPORT_TOOL_NAME and report_runner is not None:
                    # 委派报告子代理（内核级虚拟工具，不进 ToolRegistry）
                    plans.append((call, asyncio.create_task(_execute_report(call)), None))
                    continue

                plans.append((call, asyncio.create_task(_execute(call)), None))
        except asyncio.CancelledError:
            for _, task, _ in plans:
                if task is not None:
                    task.cancel()
            raise

        # 阶段二（并行）：等全部放行的调用落定。execute() 内部已把工具错误
        # 转成 ok=False 的结果，能从这里冒出来的异常都是内部错误 —— 语义与
        # 串行版一致（终止 run）。先等全部子任务结束再抛，不留孤儿任务。
        running = [task for _, task, _ in plans if task is not None]
        if running:
            outcomes = await asyncio.gather(*running, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException) and not isinstance(
                    outcome, asyncio.CancelledError
                ):
                    raise outcome

        # 阶段三：按声明顺序回灌，协议合法且 transcript 可回放、可读
        for call, task, blocked in plans:
            if blocked is not None:
                messages.append(blocked)
                continue
            result = task.result()
            messages.append(
                Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=not result.ok,
                )
            )

    async def _call_llm(
        self,
        messages: list[Message],
        emitter: EventEmitter,
        *,
        allow_tools: bool,
        text_sink: list[str] | None = None,
    ) -> LLMResponse:
        specs = self._tools.specs() if allow_tools else []
        if allow_tools and self._report_delegate is not None:
            # write_report 的 spec 由循环动态注入（它不在 registry 里）——
            # 没装配 ReportDelegate 时模型根本看不到这个工具
            specs = [*specs, REPORT_TOOL_SPEC]

        def on_text(chunk: str) -> None:
            emitter.emit(EventType.TEXT_DELTA, text=chunk)
            if text_sink is not None:
                text_sink.append(chunk)  # 取消时要能找回已流出的半截回答

        try:
            return await self._llm.stream_chat(messages, specs, on_text=on_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emitter.emit(EventType.ERROR, kind=type(exc).__name__, message=str(exc))
            raise


# ---------------------------------------------------------------------- 辅助


def _emit_tool_result(emitter: EventEmitter, *, turn: int, call: ToolCall, result) -> None:
    """ARTIFACT + TOOL_RESULT 两个事件。

    完整结果原文进 journal（恢复对账用）；SSE 帧会把 content 剔掉（webapi），
    前端只用 preview。落盘时"进上下文的字符数"远小于原始规模 —— 两者都要报，
    否则看 trace 的人会以为"这个工具只返回了几百字符"。
    """
    artifact = result.meta.get("artifact")
    if artifact:
        # 完整内容已落盘，上下文里只有摘要 + 路径
        emitter.emit(
            EventType.ARTIFACT,
            turn=turn,
            name=call.name,
            path=artifact,
            content_chars=result.meta.get("content_chars", 0),
        )

    emitter.emit(
        EventType.TOOL_RESULT,
        turn=turn,
        name=call.name,
        ok=result.ok,
        content_chars=len(result.content),
        original_chars=result.meta.get("content_chars", len(result.content)),
        artifact=artifact,
        preview=result.content[:RESULT_PREVIEW_CHARS],
        tool_call_id=call.id,
        content=result.content,
    )


def _ensure_system(messages: list[Message], system_prompt: str, note: str = "") -> None:
    """保证首条是 system。

    会话里**不存 system 消息**，每次由 runtime 注入最新的那份 ——
    这样改了 prompt 立刻对老会话生效，prompt 版本化才有意义。

    `note` 是运行时上下文（当前会话 id / 输出目录），跟在正式 prompt 后面。
    它也必须每轮重算：同一会话的下一轮 run 拿到的还是这份，但 run 目录
    （无会话时）每次都不同。
    """
    content = f"{system_prompt}\n\n{note}" if note else system_prompt
    if messages and messages[0].role == "system":
        messages[0] = Message(role="system", content=content)
        return
    messages.insert(0, Message(role="system", content=content))


def _runtime_note(session_id: str | None, run_id: str) -> str:
    """每轮注入给模型的运行时上下文。

    输出目录必须由系统给出、不让模型猜：写文件的路径判定（放行 / 询问 /
    拒绝）是确定性的，模型把目录猜错 = 白白弹一次审批。
    """
    scope = session_scope_dir(session_id, run_id)
    who = session_id or "无（一次性提问）"
    return (
        "# 运行时上下文（系统每轮注入，不是用户说的话）\n"
        f"- 当前会话 id：{who}\n"
        f"- 文件输出目录：{scope}/ —— 用 write_file / write_report 落文件时"
        "用它做路径前缀，例如 "
        f"`{scope}/报告.md`"
    )


def _repair_transcript(messages: list[Message]) -> int:
    """把被中断的 transcript 修回协议合法的状态，返回补了几条。

    破损只有一种形态：最后一条 assistant 声明了 tool_calls，但对应的 tool
    结果不全（进程被杀 / 用户取消 / 异常中断）。两个协议都不接受这种结尾，
    所以续跑前必须补齐。

    关键在补齐的**内容**：判定为"结果未知"，而不是"失败"。工具可能已经
    把请求发出去了 —— 对只读行情无所谓，对写类工具，盲目重试就是事故。
    """
    last_assistant: int | None = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant":
            last_assistant = index
            break

    if last_assistant is None:
        return 0

    calls = messages[last_assistant].tool_calls or []
    if not calls:
        return 0

    answered = {
        m.tool_call_id
        for m in messages[last_assistant + 1 :]
        if m.role == "tool" and m.tool_call_id
    }
    missing = [call for call in calls if call.id not in answered]
    if not missing:
        return 0

    # 补在最后：位置不影响协议合法性，但按顺序追更容易读
    for call in missing:
        messages.append(
            Message(
                role="tool",
                content=INTERRUPTED_RESULT,
                tool_call_id=call.id,
                name=call.name,
                is_error=True,
            )
        )
    return len(missing)
