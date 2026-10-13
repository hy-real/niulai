"""本地 Web 服务层：把内核事件流变成 HTTP + SSE。

与 cli.py 完全同构的"外壳"：装配 → 开 run → 把 EventEmitter 的事件转成
自己的输出格式（CLI 转 ANSI 文本，这里转 SSE 帧）。内核不 import FastAPI
—— "内核不知道 UI 存在"在这里兑现。

## 协议

- 事件帧：`event: <事件类型>` + `data: <Event.to_dict() 的 JSON>`，
  seq / run_id 原样透传（前端断线重连、与 trace 对账都靠它们）；
- 合成帧：`run_result`（stop_reason / usage / session_id）——
  前端靠它收尾，不必自己拼装十几种事件；
- 断开连接 = 取消 run：与 CLI 的 Ctrl-C 走同一条内核链路
  （CancelledError 透传 → ERROR/RUN_END 事件照发）。

## 并发与安全

- 按会话隔离并发：同一会话同时只允许一个 run（409），不同会话可并行；
- 每帧事件注入 session_id，前端据此路由到对应会话的视图；
- 只绑 127.0.0.1，并校验 Host 头（防 DNS rebinding）；
- API Key 不经 HTTP 传输——服务端从 env / config.toml 自己读。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .agent.loop import _repair_transcript
from .artifacts import ArtifactError
from .cli import Assembly, _assemble
from .config import Settings, read_ths_api_key
from .datasource.base import Capability
from .errors import ConfigError
from .events import EventEmitter, EventType, new_run_id
from .llm import Usage
from .paths import data_dir, trace_dir
from .permissions import Approval, ApprovalMemory, PermissionRequest
from .session import TITLE_CHARS, SessionError, SessionStore, _ts_ms
from .tools.files import read_display_text, resolve_in_data_root
from .trace import TraceResumeError, TraceWriter, load_transcript, read_trace

DEFAULT_PORT = 8710

_DONE = object()  # 队列哨兵：run 任务已结束（区别于任何 Event）


class WebApprovalChannel:
    """Web 审批通道：`permission_request` 事件已随 SSE 推给浏览器，这里等
    前端 POST 答复（见 /api/runs/{run_id}/permissions）。

    **超时在通道里施加**（默认拒绝，fail-safe）：没人盯着的弹窗不该把
    agent 永久挂起。超时决定与用户拒绝一样进 trace（by="timeout"）。

    **幂等**：同一 request_id 只接受第一个答复，重复 POST 返回 False
    （用户点两次"批准"不能执行两次）。
    """

    def __init__(self, timeout_s: float = 120.0) -> None:
        self._timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future[Approval]] = {}

    async def request(self, req: PermissionRequest) -> Approval:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Approval] = loop.create_future()
        self._pending[req.request_id] = future
        try:
            return await asyncio.wait_for(future, timeout=self._timeout_s)
        except TimeoutError:
            return Approval(granted=False, by="timeout")
        finally:
            self._pending.pop(req.request_id, None)

    def resolve(self, request_id: str, approval: Approval) -> bool:
        """递一个答复进去。已答复 / 已超时 / 不认识 → False（调用方转 409）。"""
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(approval)
        return True


# 进行中 run 的审批通道：run_id -> channel。POST 端点据此找到要答复的弹窗。
_APPROVALS: dict[str, WebApprovalChannel] = {}


def web_dir() -> Path | None:
    """前端静态文件目录。NIULAI_WEB_DIR 可覆盖；默认自动定位仓库里的
    apps/desktop/web。

    可编辑安装下 __file__ 指向真实源码，向上 5 级即仓库根；正式打包后
    路径由安装器布局决定，届时再改成资源目录。
    """
    override = os.environ.get("NIULAI_WEB_DIR", "").strip()
    if override:
        path = Path(override)
        return path if path.is_dir() else None
    candidate = Path(__file__).resolve().parents[4] / "apps" / "desktop" / "web"
    return candidate if candidate.is_dir() else None


class _RunRegistry:
    """进行中任务的登记表。

    并发模型：**按会话隔离**——同一会话同时只允许一个 run（对话本该串行），
    不同会话可以并行跑。

    占位必须是**同步原子**的（reserve）：如果 handler 里只做 check、
    登记留给 StreamingResponse 生成器去做，两个并发请求会在生成器
    启动前都通过检查 —— 登记与检查之间隔着真实的异步间隙。
    asyncio 单线程内没有 await 的代码段天然原子，check+set 收进
    同一个同步函数即可；task 由生成器启动后再 attach 挂上。
    """

    def __init__(self) -> None:
        self._by_run: dict[str, tuple[str, asyncio.Task[Any] | None]] = {}
        self._by_session: dict[str, str] = {}  # session_id -> run_id

    def reserve(self, session_id: str, run_id: str) -> bool:
        """同步占位：同一会话的第二个请求拿到 False（→ 409），不可能都进来。"""
        if session_id in self._by_session:
            return False
        self._by_session[session_id] = run_id
        self._by_run[run_id] = (session_id, None)
        return True

    def attach(self, run_id: str, task: asyncio.Task[Any]) -> None:
        """run 任务真正创建后挂上（供 cancel 端点找到它）。"""
        entry = self._by_run.get(run_id)
        if entry is not None:
            self._by_run[run_id] = (entry[0], task)

    def finish(self, run_id: str) -> None:
        entry = self._by_run.pop(run_id, None)
        if entry and self._by_session.get(entry[0]) == run_id:
            del self._by_session[entry[0]]

    def task_for(self, run_id: str) -> asyncio.Task[Any] | None:
        entry = self._by_run.get(run_id)
        return entry[1] if entry else None


_RUNS = _RunRegistry()
# SessionStore 在 create_app() 里创建（而不是模块级）：
# 它的目录在构造时确定，模块级实例会抢在测试设置 NIULAI_HOME 之前定型

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver", "testserver.local"}
# testserver 是 fastapi TestClient 的默认 Host，单测要用


def _find_interrupted_journal(session_id: str) -> tuple[Path, dict[str, Any]] | None:
    """该会话最近一次未收尾 run 的 journal（恢复数据源）。

    只看最新一条匹配的 run：如果它已正常收尾，更早的中断也视为过期 ——
    恢复必须发生在对话末尾，往历史中间插消息会破坏顺序。
    """
    d = trace_dir()
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:100]:  # 本地单用户，扫最近 100 条足够
        try:
            with path.open("r", encoding="utf-8") as fh:
                head = json.loads(fh.readline())
        except (OSError, json.JSONDecodeError):
            continue
        if head.get("type") != "run_start" or head.get("session_id") != session_id:
            continue
        if any(r.get("type") == "run_end" for r in read_trace(path)):
            return None
        return path, head
    return None


class RunRequest(BaseModel):
    task: str
    session_id: str | None = None


class PermissionDecision(BaseModel):
    """审批弹窗的答复。request_id 是幂等键，重复答复只认第一个。"""

    request_id: str
    granted: bool
    always: bool = False


class ConfigUpdate(BaseModel):
    """设置面板提交的字段。None / 空串 = 不修改该项（api_key 同理，防止误清空）。"""

    protocol: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    ths_api_key: str | None = None
    max_turns: int | None = None
    tool_timeout_s: float | None = None
    keep_recent_tool_results: int | None = None


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    """一条 SSE 帧。data 是单行 JSON —— Event.to_dict() 本来就是扁平的。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _history_view(messages: list[Any]) -> list[dict[str, Any]]:
    """会话历史的界面视图：不含 system，内容完整返回。

    本地单用户 + 会话已在 _history_view 序列化时扣掉 system，不做长度截断——
    界面要能完整看到 agent 说过的每一句话（用户明确要求"长点就长点"）。
    大结果的真实数据本来就在 artifacts 落盘，transcript 里只有摘要 + 路径，
    所以单条消息的体积天然有上界，不用担心这个接口膨胀。
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        item: dict[str, Any] = {"role": m.role}
        item["content"] = m.content or ""
        if m.role == "assistant" and m.tool_calls:
            item["tool_calls"] = [
                {"name": c.name, "arguments": c.arguments} for c in m.tool_calls
            ]
        if m.role == "tool":
            item["name"] = m.name
            item["is_error"] = m.is_error
        out.append(item)
    return out


def create_app() -> FastAPI:
    # 装配缓存：key 是 (Settings, 同花顺 Key)。配置没变 → 复用同一份装配
    # （LLM 的 httpx 连接池、限流器 / 结果缓存，都是只有跨请求共享才成立的
    # 全局状态——每请求新建等于让它们失效）；配置变了（PUT
    # /api/config 或 env）→ 新 key 自然 miss，重建即可，"保存后即生效"
    # 的动态刷新语义不受影响。同花顺 Key 不在 Settings 里（它决定数据源
    # 装配，而不是 LLM 装配），所以要单独进缓存键 —— 否则界面里配了 Key
    # 也不会生效。缓存放 create_app 里而不是模块级：
    # ArtifactStore 的数据目录在构造时解析，模块级会抢在测试设置
    # NIULAI_HOME 之前定型（SessionStore 同理，见下）。
    _asm_cache: dict[tuple[Settings, str], Assembly] = {}
    # "总是允许"的会话级记忆跨装配共享：改一次配置会重建 runtime，
    # 但用户在本会话里批过的"不再问"不该因此失效
    _approval_memory = ApprovalMemory()

    def _get_asm(settings: Settings) -> Assembly:
        cache_key = (settings, read_ths_api_key())
        asm = _asm_cache.get(cache_key)
        if asm is None:
            asm = _assemble(settings, approval_memory=_approval_memory)
            _asm_cache[cache_key] = asm
        return asm

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        # 旧装配不在"配置变更"时关闭——可能有 run 正在用它的连接——
        # 统一留给进程收尾；改一次配置最多闲置一份连接，本地可接受。
        for asm in _asm_cache.values():
            await asm.llm.aclose()
            await asm.service.aclose()
        _asm_cache.clear()

    app = FastAPI(title="Niulai", docs_url=None, redoc_url=None, lifespan=_lifespan)
    store = SessionStore()

    @app.middleware("http")
    async def _guard_host(request: Request, call_next: Any):
        host = (request.headers.get("host") or "").split(":", 1)[0].lower()
        if host not in _ALLOWED_HOSTS:
            return HTMLResponse("Niulai 只允许本机访问", status_code=403)
        return await call_next(request)

    @app.get("/")
    def index():
        base = web_dir()
        if base and (base / "index.html").is_file():
            return FileResponse(base / "index.html")
        return HTMLResponse(
            "<h1>Niulai API</h1><p>未找到前端页面。请把 index.html 放入 apps/desktop/web/，"
            "或用 NIULAI_WEB_DIR / --web-dir 指定静态目录。</p>"
        )

    @app.get("/api/tools")
    def api_tools():
        """工具与数据源清单（不需要 Key）。"""
        asm = _get_asm(Settings.load(require_key=False))
        tools = asm.tools
        out_tools = []
        for name in tools.names():
            tool = tools.get(name)
            out_tools.append(
                {
                    "name": name,
                    "description": (tool.spec.description or "").splitlines()[0] if tool else "",
                    "capabilities": sorted(tools.capabilities(name)),
                }
            )
        # cap 显式标注为 Capability：写错能力名（如 "k_lines"）会在类型检查阶段报错，
        # 而不是静默返回空列表让前端对着一张空的 routing 表猜原因。
        caps: tuple[Capability, ...] = (
            "quote", "kline", "financial", "moneyflow",
            "valuation", "hotlist", "limitup", "dragontiger", "anomaly",
        )
        routing = {cap: [s.id for s in asm.service.sources_for(cap)] for cap in caps}
        return {"sources": asm.service.source_ids, "routing": routing, "tools": out_tools}

    @app.get("/api/sessions")
    def api_sessions(limit: int = 20, offset: int = 0):
        """会话列表（按更新时间倒序）。

        limit/offset 供前端滚动分页：一页 limit 条，翻页传 offset；
        返回条数 < limit 即没有更多。只影响"列表视图"——
        按 id 打开 / 恢复会话不受此窗口限制。
        """
        return [
            {
                "id": s.id,
                "title": s.title or "(无标题)",
                "updated_at": s.updated_at,
                "turns": s.turns,
                "total_tokens": s.usage.total_tokens,
            }
            for s in store.list_ids(limit=limit, offset=offset)
        ]

    @app.get("/api/sessions/{session_id}")
    def api_session_detail(session_id: str):
        try:
            session = store.load(session_id)
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "id": session.id,
            "title": session.title or "(无标题)",
            "messages": _history_view(session.messages),
        }

    def _recoverable(session) -> tuple[Path, dict[str, Any]] | None:
        """可恢复的现场 = 未收尾 run，且比会话最后一次落盘新。

        第二个条件挡住过期残留：中断之后若又跑完过新 run，把旧现场恢复
        回来等于往对话历史中间插消息。GET 检测与 POST 恢复必须用同一份
        判定 —— 之前只挡 GET，结果"界面说没有、接口却能恢复"。
        """
        found = _find_interrupted_journal(session.id)
        if found is None:
            return None
        path, head = found
        # 现场比会话最后一次落盘旧 → 过期残留（会话的 updated_at 即最后活动时间）
        saved_sec = _ts_ms(session.updated_at) / 1000
        if saved_sec and path.stat().st_mtime < saved_sec:
            return None
        return path, head

    @app.get("/api/sessions/{session_id}/interrupted")
    def api_interrupted(session_id: str):
        """该会话是否有未收尾的 run（断线 / 崩溃 / 关窗口留下的现场）。"""
        try:
            session = store.load(session_id)
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        found = _recoverable(session)
        if found is None:
            return {"interrupted": False}
        _path, head = found
        return {"interrupted": True, "run_id": head.get("run_id"), "task": head.get("task")}

    @app.post("/api/sessions/{session_id}/resume")
    def api_resume_session(session_id: str):
        """把被中断的 run 恢复进会话：journal 重建 → 补「结果未知」→ 存盘。"""
        try:
            session = store.load(session_id)
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        found = _recoverable(session)
        if found is None:
            raise HTTPException(status_code=404, detail="该会话没有可恢复的中断任务")
        path, _head = found
        try:
            plan = load_transcript(path)
        except TraceResumeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not plan.interrupted:
            raise HTTPException(status_code=409, detail="该 run 已正常结束，无需恢复")

        repaired = _repair_transcript(plan.messages)
        session.messages = [*session.messages, *plan.messages]
        session.note_run(plan.run_id, Usage())
        store.save(session)
        return {
            "ok": True,
            "run_id": plan.run_id,
            "repaired": repaired,
            "messages": _history_view(session.messages),
        }

    @app.get("/api/config")
    def api_get_config():
        """当前生效的配置（env 与 config.toml 合并后的视图，Key 只回尾号）。"""
        settings = Settings.load(require_key=False)
        ths_key = read_ths_api_key()
        return {
            "protocol": settings.protocol,
            "base_url": settings.base_url,
            "model": settings.model,
            "max_turns": settings.max_turns,
            "tool_timeout_s": settings.tool_timeout_s,
            "keep_recent_tool_results": settings.keep_recent_tool_results,
            "has_key": bool(settings.api_key),
            "key_hint": ("已配置（尾号 " + settings.api_key[-4:] + "）") if settings.api_key else "未配置",
            "has_ths_key": bool(ths_key),
            "ths_key_hint": ("已配置（尾号 " + ths_key[-4:] + "）") if ths_key else "未配置",
        }

    @app.put("/api/config")
    def api_put_config(req: ConfigUpdate):
        """写入 ~/.niulai/config.toml 的 [llm] 节。

        注意：这里只写文件——env 优先级高于文件（config.py 的铁律），所以
        如果终端设了 NIULAI_MODEL 之类，文件改动不会体现在 GET 的合并视图里。
        内核每次提问都重新 Settings.load()，因此保存后无需重启。
        """
        if req.protocol is not None and req.protocol not in ("openai", "anthropic"):
            raise HTTPException(status_code=422, detail="protocol 只能是 openai 或 anthropic")

        from .config import config_file_path, read_config_file

        data = read_config_file()
        llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}

        updates = req.model_dump(exclude_none=True)
        key = updates.pop("api_key", None)
        ths_key = updates.pop("ths_api_key", None)
        if key is not None and key.strip():
            llm["api_key"] = key.strip()
        for field, value in updates.items():
            if isinstance(value, str) and not value.strip():
                continue  # 空串 = 不修改
            llm[field] = value
        data["llm"] = llm

        # 同花顺 Key 单独进 [datasource] 节 —— env NIULAI_THS_API_KEY 优先于文件
        if ths_key is not None and ths_key.strip():
            datasource = data.get("datasource") if isinstance(data.get("datasource"), dict) else {}
            datasource["ths_api_key"] = ths_key.strip()
            data["datasource"] = datasource

        path = config_file_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            import tomli_w

            path.write_text(tomli_w.dumps(data), encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"写入配置文件失败：{exc}") from exc
        return {"ok": True, "path": str(path), "note": "下一条提问即生效"}

    @app.post("/api/runs")
    async def api_create_run(req: RunRequest):
        task_text = req.task.strip()
        if not task_text:
            raise HTTPException(status_code=422, detail="问题不能为空")

        try:
            settings = Settings.load()
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            session = store.load(req.session_id) if req.session_id else store.create()
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # 新会话**立即落库**（标题取首条提问，截断规则与 _derive_title 一致）：
        # 前端首帧拿到 session_id 刷新侧栏时就能看到这条会话，不必等 run 结束。
        # run 结束时 note_run 只在标题为空时才改写，不会覆盖这里的标题。
        if not session.title:
            session.title = (
                task_text if len(task_text) <= TITLE_CHARS else task_text[:TITLE_CHARS] + "…"
            )
            store.save(session)

        # 先同步占位再返回响应：check 与 set 之间没有 await，
        # 两个并发请求不可能同时占住同一个会话（→ 409）。
        run_id = new_run_id()
        if not _RUNS.reserve(session.id, run_id):
            raise HTTPException(status_code=409, detail="该会话已有一个正在运行的任务")

        asm = _get_asm(settings)

        async def event_stream():
            # 与 _run_turn（cli.py）同构：trace 照落，事件扇出给一个队列桥
            emitter = EventEmitter(run_id)
            emitter.subscribe(TraceWriter(run_id))
            queue: asyncio.Queue[Any] = asyncio.Queue()
            emitter.subscribe(queue.put_nowait)

            # 审批通道是**每 run 一个**：弹窗挂在一条 SSE 连接上，通道跟着
            # 连接走。超时值走 Settings（config.toml [llm] 可调）。
            channel = WebApprovalChannel(timeout_s=settings.approval_timeout_s)
            _APPROVALS[run_id] = channel

            run_task = asyncio.create_task(
                asm.runtime.run(
                    task_text,
                    emitter=emitter,
                    messages=session.messages,
                    session_id=session.id,
                    approval_channel=channel,
                )
            )
            _RUNS.attach(run_id, run_task)
            run_task.add_done_callback(lambda _t: queue.put_nowait(_DONE))

            try:
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    payload = item.to_dict()
                    if item.type is EventType.TOOL_RESULT:
                        # 完整结果只进 journal（恢复用）；SSE 帧给 preview 就够
                        payload.pop("content", None)
                    # 每帧注入 session_id：前端据此把事件路由到对应会话的视图
                    yield _sse_frame(item.type.value, {**payload, "session_id": session.id})

                try:
                    result = run_task.result()
                except asyncio.CancelledError:
                    # 防御分支：内核现在把取消收尾成 stop_reason=cancelled 的正常
                    # 返回值（走下方 else 分支落会话），理论上到不了这里。
                    yield _sse_frame(
                        "run_result",
                        {"run_id": run_id, "session_id": session.id, "stop_reason": "cancelled"},
                    )
                except Exception as failure:  # 内核已发过 ERROR 事件，这里补终态帧
                    yield _sse_frame(
                        "run_result",
                        {
                            "run_id": run_id,
                            "session_id": session.id,
                            "stop_reason": "error",
                            "error": f"{type(failure).__name__}: {failure}",
                        },
                    )
                else:
                    # 与 cli 相同的边界：system 不进会话，run 结束后由外层合并
                    session.messages = [m for m in result.messages if m.role != "system"]
                    session.note_run(run_id, result.usage)
                    store.save(session)
                    yield _sse_frame(
                        "run_result",
                        {
                            "run_id": run_id,
                            "session_id": session.id,
                            "stop_reason": result.stop_reason,
                            "turns": result.turns,
                            "usage": result.usage.to_dict(),
                            "breaks": result.breaks,
                        },
                    )
            finally:
                # 浏览器断开 / 服务端异常时，别让内核任务悬着。
                # llm / service 是共享装配，不在这里关——lifespan 统一收尾。
                if not run_task.done():
                    run_task.cancel()
                _APPROVALS.pop(run_id, None)
                _RUNS.finish(run_id)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def api_cancel_run(run_id: str):
        task = _RUNS.task_for(run_id)
        if task is None or task.done():
            raise HTTPException(status_code=404, detail="没有正在运行的该任务")
        task.cancel()
        return {"ok": True}

    @app.post("/api/runs/{run_id}/permissions")
    async def api_permission(run_id: str, body: PermissionDecision):
        """答复一个等待中的审批（前端审批弹窗的"允许 / 总是允许 / 拒绝"）。

        409 = 已答复过或已超时——重复 POST 不执行第二次（幂等）。
        """
        channel = _APPROVALS.get(run_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="没有等待审批的任务")
        ok = channel.resolve(
            body.request_id,
            Approval(granted=body.granted, always=body.always, by="user"),
        )
        if not ok:
            raise HTTPException(status_code=409, detail="该审批已处理或已超时")
        return {"ok": True}

    # ------------------------------------------------------------ 文件面板
    # 右侧栏浏览"这个会话产出过什么文件"。列表只看会话自己的目录
    # （sessions/<id>/），内容读走 read_file 同一道围栏。

    @app.get("/api/sessions/{session_id}/files")
    def api_session_files(session_id: str):
        """列出会话产出目录（sessions/<id>/）下的文件，按修改时间倒序。"""
        try:
            session = store.load(session_id)
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        root = data_dir().resolve()
        folder = root / "sessions" / session.id
        # 围栏：session.id 虽来自库内主键，路径仍要真验一次（防 id 里带穿越字符）
        try:
            folder.resolve().relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法会话目录") from exc

        items: list[dict[str, Any]] = []
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                if not path.is_file():
                    continue
                stat = path.stat()
                items.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "name": path.name,
                        "size": stat.st_size,
                        "mtime": int(stat.st_mtime * 1000),
                    }
                )
        items.sort(key=lambda item: item["mtime"], reverse=True)
        return {"dir": f"sessions/{session.id}", "files": items}

    @app.get("/api/files/content")
    def api_file_content(path: str):
        """读数据目录内的文件内容（文件面板预览）。围栏与 read_file 同一份。"""
        try:
            resolved = resolve_in_data_root(path)
        except ArtifactError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail=f"找不到文件：{path}")

        content = read_display_text(resolved)
        root = data_dir().resolve()
        return {
            "path": resolved.relative_to(root).as_posix(),
            "name": resolved.name,
            "chars": len(content),
            "content": content,
        }

    return app


def start_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    """阻塞式启动（内部起事件循环）。给 `niulai serve` 和桌面壳调用。"""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


__all__ = ["DEFAULT_PORT", "RunRequest", "create_app", "start_server", "web_dir"]
