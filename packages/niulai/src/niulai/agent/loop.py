"""手写 ReAct 内核。

这是整个项目的核心，也是"为什么值得手写"的答案：
循环本身只有几十行，但预算、取消、截断、错误回灌这些细节全在这里。
上了框架，这些恰好会被藏起来 —— 而它们正是面试会被追问的东西。

五个停止条件，缺一不可：

1. 模型不再要求调用工具 → 正常结束
2. 达到轮数上限        → 强制收敛（最后一轮不让调工具）
3. 超出 token 预算     → 强制收敛
4. 外部取消            → `asyncio.CancelledError` 向上传播
5. 运行时熔断          → 重复 / 连续失败的调用被拦下，逼模型收敛

第 4 条不自己造"取消标志位"：`asyncio` 的任务取消会逐层透传，
自己加一套标志反而最容易漏掉某一层。

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
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import Settings
from ..context import fit_context
from ..events import EventEmitter, EventType
from ..llm import LLMClient, LLMResponse, Message, Usage
from ..tools import ToolRegistry
from .guard import ToolGuard
from .prompt import load_prompt

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


@dataclass(slots=True)
class RunResult:
    text: str
    turns: int
    usage: Usage
    stop_reason: str  # done | max_turns | token_budget | cancelled
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
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._tools = tools
        self._system_prompt = system_prompt if system_prompt is not None else load_prompt()

    async def run(
        self,
        task: str,
        *,
        emitter: EventEmitter,
        messages: Sequence[Message] | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        """跑一次任务。

        `messages` 是**已有的对话历史**（不含 system）。传进来就续写，
        不传就是一次全新任务。调用方拿 `RunResult.messages` 作为下一次的输入。
        """
        transcript = list(messages) if messages else []
        resumed = bool(transcript)
        _ensure_system(transcript, self._system_prompt)
        repaired = _repair_transcript(transcript)
        transcript.append(Message(role="user", content=task))

        usage = Usage()
        final_text = ""
        stop_reason = "done"
        turn = 0
        omitted_total = 0
        guard = ToolGuard(max_consecutive_failures=self._settings.max_consecutive_failures)

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

        try:
            for turn in range(1, self._settings.max_turns + 1):
                budget_left = max(0, self._settings.token_budget - usage.total_tokens)
                force_answer = budget_left <= 0 or turn == self._settings.max_turns

                # 发出去的是一份裁剪视图；transcript 本身始终完整。
                view, context_stats = fit_context(
                    transcript,
                    keep_recent_tool_results=self._settings.keep_recent_tool_results,
                )
                omitted_total += context_stats.omitted

                emitter.emit(
                    EventType.TURN_START,
                    turn=turn,
                    budget_left=budget_left,
                    tools_enabled=not force_answer,
                    context_omitted=context_stats.omitted,
                )

                if force_answer:
                    # 收口指令只追加在最后一轮，不污染正常轮次
                    view = view + [Message(role="user", content=FORCE_ANSWER_PROMPT)]

                response = await self._call_llm(view, emitter, allow_tools=not force_answer)
                usage = usage.merged(response.usage)
                emitter.emit(EventType.USAGE, turn=turn, cumulative=usage.to_dict())

                if force_answer:
                    # 收口轮不记录 tool_calls，**也不透传 raw_blocks**：
                    # transcript 不允许以"未执行的工具调用"结尾，否则续跑时协议不合法。
                    # Anthropic 的 raw_blocks 里就含着 tool_use 块，一并丢掉才对。
                    transcript.append(Message(role="assistant", content=response.text))
                    emitter.emit(EventType.ASSISTANT_MESSAGE, turn=turn, text=response.text)
                    final_text = response.text
                    stop_reason = "token_budget" if budget_left <= 0 else "max_turns"
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
                emitter.emit(
                    EventType.ASSISTANT_MESSAGE,
                    turn=turn,
                    text=response.text,
                    tool_calls=[
                        {"name": call.name, "arguments": call.arguments}
                        for call in response.tool_calls
                    ],
                )

                if not response.wants_tools:
                    final_text = response.text
                    stop_reason = "done"
                    break

                await self._run_tools(response, transcript, emitter, turn=turn, guard=guard)

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
            stop_reason = "cancelled"
            emitter.emit(EventType.ERROR, kind="CancelledError", message="运行被取消")
            raise

        finally:
            emitter.emit(
                EventType.RUN_END,
                stop_reason=stop_reason,
                turns=turn,
                usage=usage.to_dict(),
                context_omitted=omitted_total,
                breaks=len(guard.breaks),
            )

    async def _run_tools(
        self,
        response: LLMResponse,
        messages: list[Message],
        emitter: EventEmitter,
        *,
        turn: int,
        guard: ToolGuard,
    ) -> None:
        """逐个执行工具，并把结果回灌进 messages。

        串行执行：多个工具同时打同一个数据源容易触发限流。
        等 P3 后半加了限流与并发控制，再改成受控并发。

        **每个 tool_call 都必须回灌一条消息**，哪怕是拦下来的 ——
        assistant 已经声明了这些调用，少一条就是协议非法。
        """
        for call in response.tool_calls:
            emitter.emit(EventType.TOOL_CALL, turn=turn, name=call.name, arguments=call.arguments)

            breaker = guard.check(call)
            if breaker is not None:
                # 熔断：不执行，但照样回灌。内容写成"别再试了，改走别的路"。
                emitter.emit(
                    EventType.CIRCUIT_BREAK,
                    turn=turn,
                    name=call.name,
                    reason=breaker.reason,
                    message=breaker.message,
                )
                messages.append(
                    Message(
                        role="tool",
                        content=breaker.message,
                        tool_call_id=call.id,
                        name=call.name,
                        is_error=True,
                    )
                )
                continue

            result = await self._tools.execute(
                call, timeout_s=self._settings.tool_timeout_s, run_id=emitter.run_id
            )
            guard.record(call, ok=result.ok)

            messages.append(
                Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=not result.ok,
                )
            )

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
                # 进上下文的字符数。落盘时它远小于原始规模 —— 两者都要报，
                # 否则看 trace 的人会以为"这个工具只返回了几百字符"。
                content_chars=len(result.content),
                original_chars=result.meta.get("content_chars", len(result.content)),
                artifact=artifact,
                preview=result.content[:RESULT_PREVIEW_CHARS],
            )

    async def _call_llm(
        self,
        messages: list[Message],
        emitter: EventEmitter,
        *,
        allow_tools: bool,
    ) -> LLMResponse:
        specs = self._tools.specs() if allow_tools else []

        def on_text(chunk: str) -> None:
            emitter.emit(EventType.TEXT_DELTA, text=chunk)

        try:
            return await self._llm.stream_chat(messages, specs, on_text=on_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            emitter.emit(EventType.ERROR, kind=type(exc).__name__, message=str(exc))
            raise


# ---------------------------------------------------------------------- 辅助


def _ensure_system(messages: list[Message], system_prompt: str) -> None:
    """保证首条是 system。

    会话里**不存 system 消息**，每次由 runtime 注入最新的那份 ——
    这样改了 prompt 立刻对老会话生效，prompt 版本化才有意义。
    """
    if messages and messages[0].role == "system":
        messages[0] = Message(role="system", content=system_prompt)
        return
    messages.insert(0, Message(role="system", content=system_prompt))


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
