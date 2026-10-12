"""命令行入口。

CLI 的定位是**内核的测试夹具 + 多轮体验的最小载体**，不是最终产品形态。
最终形态是 P5 的本地 Web UI 和 P6 的桌面壳，但三种外壳吃的是**同一条事件流**，
所以这里做的东西不会白做 —— 换外壳时内核一行都不用改。

（"CLI 是测试夹具"这句在有了 `chat` 之后要打个补丁：
多轮 + 会话持久化是真的在用，不是测试代码。但它仍然不承担最终 UI 的职责。）

子命令：

    niulai ask "茅台现在多少钱"      一次性提问
    niulai chat                      多轮对话（会记住上下文）
    niulai replay latest             回放一次运行的轨迹
    niulai sessions                  列出历史会话
    niulai tools                     列出工具与数据源
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import __version__
from .agent import AgentRuntime, RunResult
from .agent.loop import _repair_transcript
from .artifacts import ArtifactStore
from .config import Settings, read_config_file, read_ths_api_key
from .datasource import build_default_service
from .errors import ConfigError, NiulaiError
from .events import Event, EventEmitter, EventType, new_run_id
from .llm import Usage, build_client
from .logs import setup_logging
from .permissions import Approval, ApprovalChannel, ApprovalMemory, PermissionRequest
from .session import Session, SessionStore
from .tools import (
    ReadFileTool,
    ToolRegistry,
    WriteFileTool,
    build_market_tools,
    build_stock_tools,
)
from .trace import TraceResumeError, TraceWriter, load_transcript, read_trace, resolve_run

STOP_REASON_LABEL = {
    "done": "正常结束",
    "max_turns": "达到轮数上限，已强制收敛",
    "cancelled": "被取消",
}


# --------------------------------------------------------------------------
# 终端输出
# --------------------------------------------------------------------------


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)


class ConsoleRenderer:
    """把事件流渲染成人能看的样子。

    它只是个 sink。内核完全不知道有没有终端存在。
    """

    def __init__(self, color: Palette, *, verbose: bool = False) -> None:
        self.c = color
        self.verbose = verbose
        self._inline = False

    def reset(self) -> None:
        self._inline = False

    def __call__(self, event: Event) -> None:
        data = event.data

        if event.type is EventType.TURN_START:
            self._newline()
            suffix = "" if data.get("tools_enabled", True) else "（收口：不再调用工具）"
            omitted = data.get("context_omitted") or 0
            if omitted:
                suffix += f"（已压缩 {omitted} 条历史工具结果）"
            print(self.c.dim(f"· 第 {data['turn']} 轮{suffix}"))

        elif event.type is EventType.TEXT_DELTA:
            sys.stdout.write(data.get("text", ""))
            sys.stdout.flush()
            self._inline = True

        elif event.type is EventType.TOOL_CALL:
            self._newline()
            print(self.c.cyan(f"  ⚙ {data.get('name')}({data.get('arguments', '')})"))

        elif event.type is EventType.CIRCUIT_BREAK:
            self._newline()
            kind = "重复调用" if data.get("reason") == "duplicate_call" else "连续失败"
            print(self.c.yellow(f"  ⛔ 熔断（{kind}）：{data.get('name')} 已被拦下，未执行"))

        elif event.type is EventType.PERMISSION_REQUEST:
            # 交互提示由 CliApprovalChannel 自己打印（它要读 stdin），
            # 这里只负责把"被策略硬拒绝"（没有 request）的结果显示出来。
            pass

        elif event.type is EventType.PERMISSION_DECISION:
            self._newline()
            if data.get("granted"):
                note = "（本会话不再问）" if data.get("always") else ""
                print(self.c.green(f"  ✓ 已许可{note}"))
            else:
                by = data.get("by") or "user"
                print(self.c.red(f"  ✗ 未许可（{by}）"))

        elif event.type is EventType.ARTIFACT:
            self._newline()
            print(
                self.c.dim(
                    f"  📄 结果过大已落盘：{data.get('path')}"
                    f"（{data.get('content_chars', 0):,} 字符，可用 read_file 读取）"
                )
            )

        elif event.type is EventType.TOOL_RESULT:
            self._newline()
            ok = data.get("ok")
            mark = self.c.green("✓") if ok else self.c.red("✗")
            detail = f"{data.get('content_chars', 0)} 字符"
            if data.get("artifact"):
                detail += "，已落盘"
            print(self.c.dim(f"  {mark} 返回 {detail}"))

        elif event.type is EventType.ERROR:
            self._newline()
            print(self.c.red(f"  ! {data.get('kind')}: {data.get('message')}"))

        elif event.type is EventType.FINAL:
            self._newline()

    def _newline(self) -> None:
        if self._inline:
            print()
            self._inline = False


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("NIULAI_COLOR", "").lower() in ("always", "1", "true"):
        return True
    return sys.stdout.isatty()


def _enable_windows_ansi() -> None:
    """Windows 老终端默认不解析 ANSI 转义。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 审批通道（终端）
# --------------------------------------------------------------------------


class CliApprovalChannel:
    """终端审批通道：弹 y/n/a，交互式，**不设超时**。

    不设超时的理由：用户就在终端前，等待本身就是交互的一部分；要走开 /
    无人值守的场景本来就不该用 CLI。Ctrl-C 取消整轮 = 拒绝（CancelledError
    透传到内核，与点停止同一条链路）。

    `always`（本会话不再问）的记忆不在通道里 —— 它按会话跨 run 有效，
    住在 runtime 持有的 ApprovalMemory 中（见 permissions.py）。
    """

    def __init__(self, color: Palette) -> None:
        self._color = color

    async def request(self, req: PermissionRequest) -> Approval:
        print()
        print(self._color.yellow(f"  ⚠ 需要你的许可：{req.tool}"))
        print(self._color.dim(f"    {req.summary}"))
        try:
            line = await asyncio.to_thread(
                input, self._color.cyan("    允许？[y]es / [n]o / [a]lways（本会话不再问）› ")
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return Approval(granted=False, by="cancelled")
        answer = line.strip().lower()
        if answer in ("a", "always"):
            return Approval(granted=True, always=True)
        if answer in ("n", "no"):
            return Approval(granted=False)
        return Approval(granted=True)  # y / yes / 直接回车


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Assembly:
    """一次命令要用到的东西，集中装配，方便换掉其中一件（测试时最有用）。"""

    runtime: AgentRuntime
    llm: Any
    service: Any
    # Web 服务层（webapi.py）还需要这两件：工具清单接口、artifact 根目录
    tools: Any = None
    store: Any = None


def _assemble(
    settings: Settings,
    *,
    approval_channel: ApprovalChannel | None = None,
    approval_memory: ApprovalMemory | None = None,
) -> Assembly:
    store = ArtifactStore()
    service = build_default_service(ths_api_key=read_ths_api_key())
    tools = ToolRegistry(
        [
            *build_stock_tools(service),
            *build_market_tools(service),
            ReadFileTool(store),
            WriteFileTool(),
        ],
        artifact_store=store,
    )
    llm = build_client(settings)
    runtime = AgentRuntime(
        settings=settings,
        llm=llm,
        tools=tools,
        approval_channel=approval_channel,
        approval_memory=approval_memory,
    )
    return Assembly(runtime=runtime, llm=llm, service=service, tools=tools, store=store)


@dataclass(slots=True)
class TurnOutcome:
    result: RunResult
    run_id: str
    trace_path: Any = None


async def _run_turn(
    asm: Assembly,
    task: str,
    *,
    settings: Settings,
    session: Session | None = None,
    json_out: bool = False,
    renderer: ConsoleRenderer | None = None,
    write_trace: bool = True,
    approval_channel: ApprovalChannel | None = None,
) -> TurnOutcome:
    """跑一轮（一次 run），并把结果并回会话。

    `session` 传了就续写历史 —— 这正是"内核负责一次 run、会话负责多轮"的边界：
    内核不知道有没有会话，它只是接受一份 transcript。

    `approval_channel` 不传（如 `--json` 模式）时，命中审批的工具调用按
    拒绝处理（fail-safe）—— 管道消费方没人能回答 y/n。
    """
    run_id = new_run_id()
    emitter = EventEmitter(run_id)

    tracer: TraceWriter | None = None
    if write_trace:
        tracer = TraceWriter(run_id)
        emitter.subscribe(tracer)

    if json_out:
        emitter.subscribe(
            lambda ev: print(json.dumps(ev.to_dict(), ensure_ascii=False), flush=True)
        )
    else:
        assert renderer is not None
        renderer.reset()
        emitter.subscribe(renderer)

    result = await asm.runtime.run(
        task,
        emitter=emitter,
        messages=session.messages if session else None,
        session_id=session.id if session else None,
        approval_channel=approval_channel,
    )

    if session is not None:
        # system 不进会话：它由 runtime 每次注入最新版本，
        # 这样改了 prompt 立刻对老会话生效（prompt 版本化才有意义）。
        session.messages = [m for m in result.messages if m.role != "system"]
        session.note_run(run_id, result.usage)

    return TurnOutcome(result=result, run_id=run_id, trace_path=tracer.path if tracer else None)


def _print_summary(
    outcome: TurnOutcome, color: Palette, *, session: Session | None = None
) -> None:
    result = outcome.result
    usage = result.usage
    label = STOP_REASON_LABEL.get(result.stop_reason, result.stop_reason)

    print()
    print(
        color.dim(
            f"· {label}　{result.turns} 轮　"
            f"tokens {usage.total_tokens:,}"
            f"（输入 {usage.prompt_tokens:,} / 输出 {usage.completion_tokens:,}）"
            + (f"　缓存命中 {usage.cache_read_tokens:,}" if usage.cache_read_tokens else "")
            + (f"　熔断 {result.breaks} 次" if result.breaks else "")
        )
    )
    if session is not None and session.turns > 1:
        print(
            color.dim(
                f"· 本会话累计 {session.turns} 次提问　tokens {session.usage.total_tokens:,}"
            )
        )
    if outcome.trace_path is not None:
        print(color.dim(f"· trace 已写入 {outcome.trace_path}"))
        print(color.dim(f"· 回放：niulai replay {outcome.run_id}"))


# --------------------------------------------------------------------------
# ask
# --------------------------------------------------------------------------


async def _cmd_ask(args: argparse.Namespace) -> int:
    settings = Settings.from_env(protocol=args.protocol).with_overrides(
        model=args.model,
        base_url=args.base_url,
        max_turns=args.max_turns,
    )
    color = Palette(use_color())
    # --json 是管道模式：没人能回答 y/n，审批一律按拒绝处理（fail-safe）
    channel = None if args.json else CliApprovalChannel(color)
    asm = _assemble(settings, approval_channel=channel)

    session: Session | None = None
    store = SessionStore()
    if args.session is not None:
        session = store.load(args.session) if args.session else store.create()
        if not args.json:
            print(color.dim(f"▸ 续接会话 {session.id}（{session.turns} 次提问）"))

    try:
        outcome = await _run_turn(
            asm,
            " ".join(args.task),
            settings=settings,
            session=session,
            json_out=args.json,
            renderer=ConsoleRenderer(color),
            write_trace=not args.no_trace,
        )
    finally:
        await asm.llm.aclose()
        await asm.service.aclose()

    if session is not None:
        path = store.save(session)
        if not args.json:
            print(color.dim(f"· 会话已保存 {path}"))
    if not args.json:
        _print_summary(outcome, color, session=session)

    return 0


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------

_BANNER = """Niulai —— 本地优先的 A 股助手
问点什么吧。命令：/help 看帮助，/new 开新会话，/exit 退出。"""

_HELP = """可用命令：
  /help        显示这份帮助
  /new         结束当前会话，开一段新的
  /history     看当前会话都聊过什么
  /usage       看当前会话的累计用量
  /exit        退出（Ctrl-C / Ctrl-D 也一样）"""


async def _cmd_chat(args: argparse.Namespace) -> int:
    settings = Settings.from_env(protocol=args.protocol).with_overrides(
        model=args.model,
        base_url=args.base_url,
        max_turns=args.max_turns,
    )
    color = Palette(use_color())
    asm = _assemble(settings, approval_channel=CliApprovalChannel(color))
    store = SessionStore()
    renderer = ConsoleRenderer(color)

    session = store.load(args.resume) if args.resume else store.create()
    print(color.bold(f"Niulai 会话 {session.id}") if session.turns == 0 else color.bold(f"续接会话 {session.id}"))
    print(color.dim(f"模型 {settings.model}（{settings.protocol}）　数据源 {'、'.join(asm.service.source_ids)}"))
    print()
    print(color.dim(_BANNER if not session.turns else "接着聊。命令：/help /new /history /usage /exit"))
    print()

    try:
        while True:
            try:
                line = await asyncio.to_thread(input, color.cyan("你 › "))
            except (EOFError, KeyboardInterrupt):
                print()
                break

            task = line.strip()
            if not task:
                continue

            if task.startswith("/"):
                command = task.lower()
                if command in ("/exit", "/quit", "/q"):
                    break
                if command == "/help":
                    print(color.dim(_HELP))
                    continue
                if command == "/new":
                    print(color.dim(f"· 已保存 {store.save(session)}"))
                    session = store.create()
                    print(color.dim(f"· 新会话 {session.id}"))
                    continue
                if command == "/history":
                    _print_history(session, color)
                    continue
                if command == "/usage":
                    usage = session.usage
                    print(
                        color.dim(
                            f"· {session.turns} 次提问　{len(session.runs)} 次运行　"
                            f"tokens {usage.total_tokens:,}"
                            f"（输入 {usage.prompt_tokens:,} / 输出 {usage.completion_tokens:,}）"
                        )
                    )
                    continue
                print(color.dim("· 未知命令，/help 看可用命令"))
                continue

            try:
                outcome = await _run_turn(
                    asm,
                    task,
                    settings=settings,
                    session=session,
                    renderer=renderer,
                    write_trace=not args.no_trace,
                )
            except NiulaiError as exc:
                print(color.red(f"· 这轮失败了：{exc}"))
                continue
            except KeyboardInterrupt:
                print()
                print(color.dim("· 已取消这一轮（历史里会标成「结果未知」）"))
                continue

            _print_summary(outcome, color, session=session)
            print()
    finally:
        if session.messages:
            print(color.dim(f"· 会话已保存 {store.save(session)}"))
        await asm.llm.aclose()
        await asm.service.aclose()

    return 0


def _print_history(session: Session, color: Palette) -> None:
    if not session.messages:
        print(color.dim("· 还没有聊过什么"))
        return
    print(color.dim(f"· 会话 {session.id}（{session.turns} 次提问，{len(session.messages)} 条消息）"))
    for message in session.messages:
        text = " ".join((message.content or "").split())
        if len(text) > 70:
            text = text[:70] + "…"
        if message.role == "user":
            print(f"  你  {text}")
        elif message.role == "assistant" and text:
            print(color.dim(f"  它  {text}"))
        elif message.role == "tool":
            mark = "✗" if message.is_error else "✓"
            print(color.dim(f"  {mark} {message.name}（{len(message.content or '')} 字符）"))


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def _cmd_sessions(args: argparse.Namespace) -> int:
    color = Palette(use_color())
    store = SessionStore()
    items = store.list_ids(limit=args.limit)
    if not items:
        print(color.dim("还没有任何会话。用 `niulai chat` 开一个。"))
        return 0

    print(color.dim(f"共 {len(items)} 条（按最近使用排序）："))
    for item in items:
        updated = (item.updated_at or "")[:16].replace("T", " ")
        title = item.title or "(无标题)"
        print(f"  {item.id}　{updated}　{item.usage.total_tokens:>8,} tokens　{title}")
    print()
    print(color.dim("续接：niulai chat --resume <id>　或　niulai ask --session <id> \"...\""))
    return 0


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def _cmd_replay(args: argparse.Namespace) -> int:
    path = resolve_run(args.run)
    color = Palette(use_color())

    print(color.dim(f"▸ 回放 {path}"))
    print()

    usage_total = 0
    turns = 0
    stop_reason = ""
    final_text = ""

    for record in read_trace(path):
        _render_record(record, color)
        kind = record.get("type")
        if kind == "usage":
            usage_total = (record.get("cumulative") or {}).get("total_tokens", usage_total)
        elif kind == "run_end":
            turns = record.get("turns", turns)
            stop_reason = record.get("stop_reason", stop_reason)
            final_text = record.get("text", final_text) or final_text

    print()
    print(
        color.dim(
            f"▸ {STOP_REASON_LABEL.get(stop_reason, stop_reason)}　{turns} 轮　"
            f"tokens {usage_total:,}"
        )
    )
    return 0


def _render_record(record: dict[str, Any], color: Palette) -> None:
    kind = record.get("type")
    ts = (record.get("ts") or "")[11:19]

    if kind == "run_start":
        tool_names = "、".join(record.get("tool_names") or []) or "无"
        protocol = record.get("protocol") or "openai"
        print(color.bold(f"[{ts}] 任务：{record.get('task')}"))
        print(
            color.dim(
                f"         模型 {record.get('model')}（{protocol}）　工具 {tool_names}"
            )
        )
        if record.get("session_id"):
            print(color.dim(f"         会话 {record['session_id']}"))
        if record.get("resumed"):
            repaired = record.get("repaired_messages") or 0
            note = f"，补回 {repaired} 条被中断的工具结果" if repaired else ""
            print(color.dim(f"         续接已有历史{note}"))

    elif kind == "turn_start":
        extra = ""
        if record.get("context_omitted"):
            extra = f"　已压缩 {record['context_omitted']} 条历史工具结果"
        print(color.dim(f"[{ts}] 第 {record.get('turn')} 轮{extra}"))

    elif kind == "assistant_message":
        text = (record.get("text") or "").strip()
        if text:
            print(color.cyan(f"[{ts}] 模型：{text}"))
        for call in record.get("tool_calls") or []:
            print(color.dim(f"          ↳ 请求调用 {call.get('name')}({call.get('arguments')})"))

    elif kind == "tool_call":
        print(color.yellow(f"[{ts}] 工具 {record.get('name')} 参数 {record.get('arguments')}"))

    elif kind == "circuit_break":
        reason = "重复调用" if record.get("reason") == "duplicate_call" else "连续失败"
        print(color.yellow(f"[{ts}] ⛔ 熔断（{reason}）{record.get('name')}"))

    elif kind == "artifact":
        print(
            color.dim(
                f"[{ts}] 📄 {record.get('name')} 结果落盘 → {record.get('path')}"
                f"（{record.get('content_chars', 0):,} 字符）"
            )
        )

    elif kind == "tool_result":
        mark = "✓" if record.get("ok") else "✗"
        line = f"[{ts}] {mark} {record.get('name')} 返回 {record.get('content_chars')} 字符"
        print(color.green(line) if record.get("ok") else color.red(line))
        print(_indent(record.get("preview") or "", "        "))

    elif kind == "usage":
        cumulative = record.get("cumulative") or {}
        print(color.dim(f"[{ts}] 用量累计 {cumulative.get('total_tokens', 0):,} tokens"))

    elif kind == "error":
        print(color.red(f"[{ts}] 错误 {record.get('kind')}: {record.get('message')}"))

    elif kind == "final":
        print(color.bold(f"[{ts}] 最终回答（{record.get('stop_reason')}）"))
        print(_indent(record.get("text") or "", "    "))


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


def _cmd_tools() -> int:
    store = ArtifactStore()
    service = build_default_service(ths_api_key=read_ths_api_key())
    tools = ToolRegistry(
        [
            *build_stock_tools(service),
            *build_market_tools(service),
            ReadFileTool(store),
            WriteFileTool(),
        ],
        artifact_store=store,
    )

    print(f"数据源：{'、'.join(service.source_ids)}")
    for capability in (
        "quote", "kline", "financial", "moneyflow",
        "valuation", "hotlist", "limitup", "dragontiger", "anomaly",
    ):
        sources = [s.id for s in service.sources_for(capability)]  # type: ignore[arg-type]
        print(f"  · {capability:<10} {'、'.join(sources) or '（无）'}")

    print()
    print(f"工具（{len(tools.names())} 个）：")
    for name in tools.names():
        tool = tools.get(name)
        first_line = (tool.spec.description if tool else "").splitlines()[0]
        caps = "、".join(sorted(tools.capabilities(name), key=_capability_order))
        print(f"  · {name}　[{caps}]")
        print(f"      {first_line}")

    print()
    print(f"落盘目录：{store.root}")
    print()
    print("能力是工具自己声明的，能不能用由策略层判定 —— 见 docs/architecture.md 7.12。")
    return 0


def _capability_order(capability: str) -> int:
    """按固定顺序显示能力，别让中文排序决定 `read` 排在 `network` 后面。"""
    order = ["read", "network", "write_local", "exec"]
    return order.index(capability) if capability in order else len(order)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def _cmd_serve(args: argparse.Namespace) -> int:
    """启动本地 Web 服务（浏览器 / 将来的桌面壳共用同一个入口）。"""
    try:
        from .webapi import start_server
    except ImportError as exc:
        print(
            "缺少 Web 依赖（FastAPI / uvicorn）。请先安装：\n"
            '  pip install -e "packages/niulai[web]"\n'
            f"（原始错误：{exc}）",
            file=sys.stderr,
        )
        return 2
    if args.web_dir:
        os.environ["NIULAI_WEB_DIR"] = args.web_dir
    print(f"Niulai 服务已启动：http://{args.host}:{args.port}（Ctrl-C 退出）")
    start_server(host=args.host, port=args.port)
    return 0


# --------------------------------------------------------------------------
# resume
# --------------------------------------------------------------------------


def _cmd_resume(args: argparse.Namespace) -> int:
    """从被中断的 run 恢复：journal → transcript → 可续聊的会话。

    v1 边界（诚实挂账，见 architecture.md 7.13）：usage 不跨恢复累计、
    续跑用的是**当前**配置而非崩溃时的快照 —— 这两项等 SQLite 迁移一起做。
    """
    color = Palette(use_color())
    try:
        plan = load_transcript(resolve_run(args.run))
    except FileNotFoundError as exc:
        print(f"找不到 trace：{exc}", file=sys.stderr)
        return 1
    except TraceResumeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not plan.interrupted:
        print(color.dim(f"run {plan.run_id} 已正常结束，无需恢复。"
                        "要继续对话请用 `niulai chat --resume <session_id>`。"))
        return 0

    store = SessionStore()
    session = store.create()
    session.messages = plan.messages
    session.note_run(plan.run_id, Usage())
    saved = store.save(session)

    repaired = _repair_transcript(session.messages)
    note = f"，其中 {repaired} 条中断的工具结果按「结果未知」补回" if repaired else ""
    print(color.bold(f"已从 run {plan.run_id} 恢复"))
    print(color.dim(f"· 重建 {len(plan.messages)} 条消息{note}"))
    print(color.dim(f"· 会话已保存 {saved}"))
    print(color.dim(f"· 继续对话：niulai chat --resume {session.id}"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="niulai", description="本地优先的 A 股 AI 助手")
    parser.add_argument("--version", action="version", version=f"niulai {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="问一个问题，走完整的 agent 循环")
    ask.add_argument("task", nargs="+", help="要问的内容")
    ask.add_argument("--json", action="store_true", help="逐行输出事件 JSON（不渲染文本）")
    ask.add_argument(
        "--protocol",
        choices=["openai", "anthropic"],
        help="协议（默认 openai；自动跟随 NIULAI_BASE_URL 推断）",
    )
    ask.add_argument("--model", help="覆盖模型名（默认取 NIULAI_MODEL）")
    ask.add_argument("--base-url", help="覆盖 API 接入点（默认取 NIULAI_BASE_URL）")
    ask.add_argument("--max-turns", type=int, help="最大轮数")
    ask.add_argument(
        "--session",
        nargs="?",
        const="",
        help="存成/续接一个会话（不带值 = 新建，带 id 或 latest = 续接）",
    )
    ask.add_argument("--no-trace", action="store_true", help="不写 trace 文件")

    chat = sub.add_parser("chat", help="多轮对话（会记住上下文）")
    chat.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        help="续接会话：不带值续接最近一条，或指定 id",
    )
    chat.add_argument(
        "--protocol",
        choices=["openai", "anthropic"],
        help="协议（默认 openai；自动跟随 NIULAI_BASE_URL 推断）",
    )
    chat.add_argument("--model", help="覆盖模型名（默认取 NIULAI_MODEL）")
    chat.add_argument("--base-url", help="覆盖 API 接入点（默认取 NIULAI_BASE_URL）")
    chat.add_argument("--max-turns", type=int, help="单次提问的最大轮数")
    chat.add_argument("--no-trace", action="store_true", help="不写 trace 文件")

    replay = sub.add_parser("replay", help="回放一次运行的轨迹")
    replay.add_argument("run", help="run_id / trace 文件路径 / latest")

    resume = sub.add_parser("resume", help="从被中断的 run 恢复出可续聊的会话")
    resume.add_argument("run", nargs="?", default="latest", help="run_id / trace 路径 / latest（默认最近一次）")

    sessions = sub.add_parser("sessions", help="列出历史会话")
    sessions.add_argument("--limit", type=int, default=20, help="最多列几条")

    sub.add_parser("tools", help="列出当前可用工具与数据源")

    serve = sub.add_parser("serve", help="启动本地 Web 服务（浏览器 / 桌面壳共用）")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认只绑本机）")
    serve.add_argument("--port", type=int, default=8710, help="监听端口")
    serve.add_argument("--web-dir", help="前端静态文件目录（默认自动定位 apps/desktop/web）")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _enable_windows_ansi()
    # 日志级别：env NIULAI_LOG_LEVEL > config.toml 的 [log].level > warning。
    # 必须在任何子命令跑之前——config.toml 坏了要能看见警告。
    file_cfg = read_config_file()
    log_section = file_cfg.get("log") if isinstance(file_cfg.get("log"), dict) else {}
    setup_logging(
        level=os.environ.get("NIULAI_LOG_LEVEL")
        or (log_section.get("level") if isinstance(log_section.get("level"), str) else None)
    )
    args = build_parser().parse_args(argv)

    try:
        if args.command == "ask":
            return asyncio.run(_cmd_ask(args))
        if args.command == "resume":
            return _cmd_resume(args)
        if args.command == "chat":
            return asyncio.run(_cmd_chat(args))
        if args.command == "replay":
            return _cmd_replay(args)
        if args.command == "sessions":
            return _cmd_sessions(args)
        if args.command == "tools":
            return _cmd_tools()
        if args.command == "serve":
            return _cmd_serve(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except NiulaiError as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130

    print("未知命令。", file=sys.stderr)
    return 1
