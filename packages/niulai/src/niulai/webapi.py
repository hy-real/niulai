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

- 同一时刻只允许一个 run（单用户本地产品），后来者拿 409；
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

# 会话历史接口里单条消息内容截断长度（完整内容在会话 JSON 文件里）
_HISTORY_CONTENT_CHARS = 400

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


class _RunSlot:
    """当前 run 的占位。单用户产品，同一时刻只允许一个 run。"""

    def __init__(self) -> None:
        self.task: asyncio.Task[Any] | None = None
        self.run_id: str | None = None

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def release(self, run_id: str) -> None:
        if self.run_id == run_id:
            self.task = None
            self.run_id = None


_RUN_SLOT = _RunSlot()
# SessionStore 在 create_app() 里创建（而不是模块级）：
# 它的目录在构造时确定，模块级实例会抢在测试设置 NIULAI_HOME 之前定型

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "testserver", "testserver.local"}
# testserver 是 fastapi TestClient 的默认 Host，单测要用


class RunRequest(BaseModel):
    task: str
    session_id: str | None = None


def _sse_frame(event: str, data: dict[str, Any]) -> str:
    """一条 SSE 帧。data 是单行 JSON —— Event.to_dict() 本来就是扁平的。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _history_view(messages: list[Any]) -> list[dict[str, Any]]:
    """会话历史的界面视图：不含 system，长内容截断（完整原文在会话文件里）。"""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        item: dict[str, Any] = {"role": m.role}
        content = m.content or ""
        item["content"] = content[:_HISTORY_CONTENT_CHARS] + ("…" if len(content) > _HISTORY_CONTENT_CHARS else "")
        if m.role == "assistant" and m.tool_calls:
            item["tool_calls"] = [
                {"name": c.name, "arguments": c.arguments} for c in m.tool_calls
            ]
        if m.role == "tool":
            item["name"] = m.name
            item["is_error"] = m.is_error
        out.append(item)
    return out


# 内置极简聊天页：桌面壳与无前端的浏览器访问都吃它。单文件、零构建。
# 渲染逻辑与 cli.py 的 ConsoleRenderer 同一张"事件 → 视觉"对照表；
# 将来做正式前端（apps/desktop/web）后它自动让位——web_dir() 存在即不启用。
_FALLBACK_UI = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Niulai</title>
<style>
  :root { --bg:#0f1115; --panel:#1c2130; --border:#262c3a; --text:#d7dce6; --muted:#7d8799;
          --accent:#4f8cff; --green:#3fb27f; --red:#e0566a; --yellow:#d9a53f; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--text); height:100vh; display:flex; flex-direction:column;
         font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }
  header { padding:10px 18px; border-bottom:1px solid var(--border); color:var(--muted);
           font-size:13px; display:flex; justify-content:space-between; }
  #chat { flex:1; overflow-y:auto; padding:18px 22px; display:flex; flex-direction:column; gap:12px; }
  .user { align-self:flex-end; background:#2b4a8f; padding:9px 14px; border-radius:14px 14px 3px 14px;
          max-width:78%; white-space:pre-wrap; }
  .assistant { align-self:flex-start; background:var(--panel); padding:10px 14px;
               border-radius:14px 14px 14px 3px; max-width:86%; white-space:pre-wrap; }
  .line { align-self:flex-start; color:var(--muted); font-size:12.5px; padding-left:4px; }
  .tool { align-self:flex-start; background:#141821; border:1px solid var(--border); border-radius:8px;
          padding:7px 11px; font-family:Consolas,Menlo,monospace; font-size:12.5px; max-width:86%; }
  .tool .head { color:var(--accent); }
  .tool .prev { color:var(--muted); margin-top:5px; white-space:pre-wrap; word-break:break-all; }
  .ok { color:var(--green); } .err { color:var(--red); } .warn { color:var(--yellow); }
  #composer { display:flex; gap:10px; padding:12px 18px; border-top:1px solid var(--border); }
  #input { flex:1; resize:none; background:var(--panel); color:var(--text); border:1px solid var(--border);
           border-radius:10px; padding:10px 13px; font:inherit; outline:none; height:52px; }
  #input:focus { border-color:var(--accent); }
  button { background:var(--accent); color:#fff; border:0; border-radius:10px; padding:0 22px;
           font:inherit; cursor:pointer; }
  button:disabled { opacity:.45; cursor:default; }
  #stop { background:var(--panel); border:1px solid var(--border); display:none; }
</style></head>
<body>
<header><div><b style="color:var(--text)">Niulai</b> · 本地 A 股助手</div><div id="info"></div></header>
<div id="chat"></div>
<form id="composer">
  <textarea id="input" placeholder="问点什么，例如：贵州茅台现在多少钱　（Enter 发送 / Shift+Enter 换行）"></textarea>
  <button id="send" type="submit">发送</button>
  <button id="stop" type="button">■ 停止</button>
</form>
<script>
'use strict';
const chat = document.getElementById('chat'), input = document.getElementById('input');
const sendBtn = document.getElementById('send'), stopBtn = document.getElementById('stop');
let busy = false, aborter = null, runId = null, sessionId = null, bubble = null, lastCard = null;

fetch('/api/tools').then(r => r.json()).then(d => {
  document.getElementById('info').textContent =
    '数据源 ' + d.sources.join('、') + ' · 工具 ' + d.tools.length + ' 个';
}).catch(() => {});

function el(cls, text) {
  const d = document.createElement('div'); d.className = cls;
  if (text !== undefined) d.textContent = text;
  chat.appendChild(d); chat.scrollTop = chat.scrollHeight; return d;
}
function scroll() { chat.scrollTop = chat.scrollHeight; }
function setBusy(v) { busy = v; sendBtn.disabled = v; input.disabled = v;
  stopBtn.style.display = v ? '' : 'none'; }

function handle(name, d) {
  if (name === 'turn_start') {
    bubble = null;
    el('line', '· 第 ' + d.turn + ' 轮' + (d.tools_enabled ? '' : '（收口：不再调用工具）'));
  } else if (name === 'text_delta') {
    if (!bubble) bubble = el('assistant', '');
    bubble.textContent += d.text; scroll();
  } else if (name === 'assistant_message') {
    bubble = null;  // 完整消息已由 text_delta 流出，这里只收尾
  } else if (name === 'tool_call') {
    bubble = null;
    lastCard = el('tool');
    const head = document.createElement('div'); head.className = 'head';
    head.textContent = '⚙ ' + d.name + '(' + d.arguments + ')';
    lastCard.appendChild(head);
  } else if (name === 'tool_result') {
    if (lastCard) {
      const st = document.createElement('div'); st.className = d.ok ? 'ok' : 'err';
      let t = (d.ok ? '✓ ' : '✗ ') + d.name + ' 返回 ' + d.content_chars + ' 字符';
      if (d.original_chars && d.original_chars !== d.content_chars)
        t += '（原始 ' + d.original_chars.toLocaleString() + '，已落盘）';
      st.textContent = t; lastCard.appendChild(st);
      if (d.preview) {
        const p = document.createElement('div'); p.className = 'prev';
        p.textContent = d.preview.slice(0, 300); lastCard.appendChild(p);
      }
      lastCard = null;
    }
  } else if (name === 'circuit_break') {
    bubble = null; el('line warn', '⛔ 熔断：' + d.name + ' 已被拦下，未执行');
  } else if (name === 'artifact') {
    el('line', '📄 结果过大已落盘：' + d.path + '（' + (d.content_chars || 0).toLocaleString() + ' 字符）');
  } else if (name === 'error') {
    el('line err', '! ' + d.kind + ': ' + d.message);
  } else if (name === 'run_result') {
    sessionId = d.session_id || sessionId;
    const labels = { done: '正常结束', max_turns: '达到轮数上限', token_budget: '超出 token 预算',
                     cancelled: '已取消', error: '运行出错' };
    let meta = '— ' + (labels[d.stop_reason] || d.stop_reason || '结束');
    if (d.turns) meta += ' · ' + d.turns + ' 轮';
    if (d.usage && d.usage.total_tokens) meta += ' · ' + d.usage.total_tokens.toLocaleString() + ' tokens';
    el('line', meta);
  }
}

async function send() {
  const task = input.value.trim();
  if (!task || busy) return;
  setBusy(true); el('user', task); input.value = '';
  bubble = null; lastCard = null;
  aborter = new AbortController();
  let sawRunId = false;
  try {
    const res = await fetch('/api/runs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: task, session_id: sessionId }),
      signal: aborter.signal
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      el('line err', '· ' + (err.detail || ('HTTP ' + res.status)));
      return;
    }
    const reader = res.body.getReader(), decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buf += decoder.decode(chunk.value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        let evName = 'message', dataText = '';
        for (const ln of block.split('\\n')) {
          if (ln.startsWith('event:')) evName = ln.slice(6).trim();
          else if (ln.startsWith('data:')) dataText += ln.slice(5).trim();
        }
        if (!dataText) continue;
        let d; try { d = JSON.parse(dataText); } catch (e) { continue; }
        if (!sawRunId && d.run_id) { runId = d.run_id; sawRunId = true; }
        handle(evName, d);
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') el('line err', '· 连接中断：' + e.message);
  } finally { setBusy(false); }
}

stopBtn.addEventListener('click', () => {
  if (runId) fetch('/api/runs/' + runId + '/cancel', { method: 'POST' }).catch(() => {});
  if (aborter) aborter.abort();
});
document.getElementById('composer').addEventListener('submit', e => { e.preventDefault(); send(); });
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
</script>
</body></html>
"""


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
        return HTMLResponse(_FALLBACK_UI)

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

    @app.post("/api/runs")
    async def api_create_run(req: RunRequest):
        task_text = req.task.strip()
        if not task_text:
            raise HTTPException(status_code=422, detail="问题不能为空")
        if _RUN_SLOT.busy():
            raise HTTPException(status_code=409, detail="已有一个运行中的任务，请等它结束或先停止")

        try:
            settings = Settings.load()
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            session = store.load(req.session_id) if req.session_id else store.create()
        except SessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
            _RUN_SLOT.task = run_task
            _RUN_SLOT.run_id = run_id
            run_task.add_done_callback(lambda _t: queue.put_nowait(_DONE))

            try:
                while True:
                    item = await queue.get()
                    if item is _DONE:
                        break
                    yield _sse_frame(item.type.value, item.to_dict())

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
                _RUN_SLOT.release(run_id)
                await asm.llm.aclose()
                await asm.service.aclose()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def api_cancel_run(run_id: str):
        task = _RUN_SLOT.task
        if _RUN_SLOT.run_id != run_id or task is None or task.done():
            raise HTTPException(status_code=404, detail="没有正在运行的该任务")
        task.cancel()
        return {"ok": True}

    return app


def start_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    """阻塞式启动（内部起事件循环）。给 `niulai serve` 和桌面壳调用。"""
    import uvicorn

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


__all__ = ["DEFAULT_PORT", "RunRequest", "create_app", "start_server", "web_dir"]
