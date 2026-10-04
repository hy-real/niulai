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
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .cli import _assemble
from .config import Settings
from .errors import ConfigError
from .events import EventEmitter, new_run_id
from .session import SessionError, SessionStore
from .trace import TraceWriter

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
    不同会话可以并行跑。数据源限流是基础设施问题，不在此处设卡；
    后续由全局限流层（P3）统一治理。
    """

    def __init__(self) -> None:
        self._by_run: dict[str, tuple[str, asyncio.Task[Any]]] = {}
        self._by_session: dict[str, str] = {}  # session_id -> run_id

    def session_busy(self, session_id: str | None) -> bool:
        return session_id in self._by_session

    def start(self, session_id: str, run_id: str, task: asyncio.Task[Any]) -> None:
        self._by_run[run_id] = (session_id, task)
        self._by_session[session_id] = run_id

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
    token_budget: int | None = None
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
    app = FastAPI(title="Niulai", docs_url=None, redoc_url=None)
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
        asm = _assemble(Settings.load(require_key=False))
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
        routing = {
            cap: [s.id for s in asm.service.sources_for(cap)]  # type: ignore[arg-type]
            for cap in ("quote", "kline", "financial")
        }
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

    @app.get("/api/config")
    def api_get_config():
        """当前生效的配置（env 与 config.toml 合并后的视图，Key 只回尾号）。"""
        settings = Settings.load(require_key=False)
        return {
            "protocol": settings.protocol,
            "base_url": settings.base_url,
            "model": settings.model,
            "max_turns": settings.max_turns,
            "token_budget": settings.token_budget,
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

        if _RUNS.session_busy(session.id):
            raise HTTPException(status_code=409, detail="该会话已有一个正在运行的任务")

        run_id = new_run_id()
        asm = _assemble(settings)

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
            _RUNS.start(session.id, run_id, run_task)
            run_task.add_done_callback(lambda _t: queue.put_nowait(_DONE))

            try:
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    # 每帧注入 session_id：前端据此把事件路由到对应会话的视图
                    yield _sse_frame(item.type.value, {**item.to_dict(), "session_id": session.id})

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
                # 浏览器断开 / 服务端异常时，别让内核任务悬着
                if not run_task.done():
                    run_task.cancel()
                _RUNS.finish(run_id)
                await asm.llm.aclose()
                await asm.service.aclose()

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
