"""本地 Web 服务层：把内核事件流变成 HTTP + SSE。

与 cli.py 完全同构的"外壳"：装配 → 开 run → 把 EventEmitter 的事件
转成自己的输出格式（CLI 转 ANSI 文本，这里转 SSE 帧）。
内核不 import FastAPI —— "内核不知道 UI 存在"（architecture.md 7.7）在这里兑现。

## 协议

- 事件帧：`event: <事件类型>` + `data: <Event.to_dict() 的 JSON>`，
  seq / run_id 原样透传（前端断线重连、与 trace 对账都靠它们）；
- 合成帧：`run_result`（stop_reason / usage / session_id）——
  前端靠它收尾，不必自己拼装十几种事件；
- 断开连接 = 取消 run：与 CLI 的 Ctrl-C 走同一条内核链路
  （CancelledError 透传 → ERROR/RUN_END 事件照发）。

## 并发与安全

- **按会话隔离并发**：同一会话同时只允许一个 run（409），不同会话可并行；
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
from .cli import Assembly, _assemble
from .config import Settings
from .datasource.base import Capability
from .errors import ConfigError
from .events import EventEmitter, EventType, new_run_id
from .llm import Usage
from .paths import trace_dir
from .session import SessionError, SessionStore
from .trace import TraceResumeError, TraceWriter, load_transcript, read_trace

DEFAULT_PORT = 8710

_DONE = object()  # 队列哨兵：run 任务已结束（区别于任何 Event）


def web_dir() -> Path | None:
    """前端静态文件目录。NIULAI_WEB_DIR 可覆盖；默认自动定位仓库里的 apps/desktop/web。

    可编辑安装下 __file__ 指向真实源码，向上 5 级即仓库根；
    正式打包（P6）后路径由安装器布局决定，届时再改成资源目录。
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
    """该会话最近一次**未收尾** run 的 journal（§7.13 恢复数据源）。

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


class ConfigUpdate(BaseModel):
    """设置面板提交的字段。None / 空串 = 不修改该项（api_key 同理，防止误清空）。"""

    protocol: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
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
    # 装配缓存：key 是不可变的 Settings 值。配置没变 → 复用同一份装配
    # （LLM 的 httpx 连接池、将来 P3 的限流器 / 结果缓存，都是只有跨请求
    # 共享才成立的全局状态——每请求新建等于让它们失效）；配置变了（PUT
    # /api/config 或 env）→ 新 key 自然 miss，重建即可，"保存后即生效"
    # 的动态刷新语义不受影响。缓存放 create_app 里而不是模块级：
    # ArtifactStore 的数据目录在构造时解析，模块级会抢在测试设置
    # NIULAI_HOME 之前定型（SessionStore 同理，见下）。
    _asm_cache: dict[Settings, Assembly] = {}

    def _get_asm(settings: Settings) -> Assembly:
        asm = _asm_cache.get(settings)
        if asm is None:
            asm = _assemble(settings)
            _asm_cache[settings] = asm
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
        caps: tuple[Capability, ...] = ("quote", "kline", "financial")
        routing = {cap: [s.id for s in asm.service.sources_for(cap)] for cap in caps}
        return {"sources": asm.service.source_ids, "routing": routing, "tools": out_tools}

    @app.get("/api/sessions")
    def api_sessions(limit: int = 20):
        return [
            {
                "id": s.id,
                "title": s.title or "(无标题)",
                "updated_at": s.updated_at,
                "turns": s.turns,
                "total_tokens": s.usage.total_tokens,
            }
            for s in store.list_ids(limit=limit)
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
        session_file = store.directory / f"{session.id}.json"
        if session_file.exists() and path.stat().st_mtime < session_file.stat().st_mtime:
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
        return {
            "protocol": settings.protocol,
            "base_url": settings.base_url,
            "model": settings.model,
            "max_turns": settings.max_turns,
            "tool_timeout_s": settings.tool_timeout_s,
            "keep_recent_tool_results": settings.keep_recent_tool_results,
            "has_key": bool(settings.api_key),
            "key_hint": ("已配置（尾号 " + settings.api_key[-4:] + "）") if settings.api_key else "未配置",
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
        if key is not None and key.strip():
            llm["api_key"] = key.strip()
        for field, value in updates.items():
            if isinstance(value, str) and not value.strip():
                continue  # 空串 = 不修改
            llm[field] = value
        data["llm"] = llm

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

            run_task = asyncio.create_task(
                asm.runtime.run(
                    task_text,
                    emitter=emitter,
                    messages=session.messages,
                    session_id=session.id,
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

    return app


def start_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    """阻塞式启动（内部起事件循环）。给 `niulai serve` 和桌面壳调用。"""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


__all__ = ["DEFAULT_PORT", "RunRequest", "create_app", "start_server", "web_dir"]
