"""会话：跨多次 run 的对话历史。

## 为什么单独一层

内核的单位是 **run**（一次任务），用户要的单位是 **会话**（一串对话）。
把一个塞进另一个会出两种问题：要么内核被迫知道"用户是谁、上次聊到哪"，
要么会话被写成"每轮丢掉历史"的假象 —— 后者更常见，表现是"越聊越笨"：
第二轮问"它今天成交多少"，模型根本不知道"它"指谁。

## 存储的与发送的不是同一份

`Session.messages` 是**完整** transcript（含全部工具结果原文），
发给模型的是 `context.fit_context()` 裁出来的**视图**。

这个不对称是故意的：存储要够完整才能恢复、回放、审计；
发送要够小才烧得起 token。如果为了省 token 把存储也裁了，
那"压缩"就变成了"不可逆的丢数据"。

（P4 会把它换成 SQLite，并加上按 run 的索引与迁移逻辑。
现在先用 JSON：一个会话一个文件，出问题时用文本编辑器就能看。）
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import NiulaiError
from .llm.types import Message, ToolCall, Usage
from .paths import sessions_dir

TITLE_CHARS = 40


class SessionError(NiulaiError):
    """会话读写失败。"""


@dataclass(slots=True)
class Session:
    """一条会话。`messages` 里**不含 system 消息** —— 那由 runtime 每次注入。"""

    id: str
    created_at: str
    updated_at: str
    messages: list[Message] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    runs: list[str] = field(default_factory=list)
    title: str = ""

    @property
    def turns(self) -> int:
        """用户提问了几次（不是 ReAct 轮数）。"""
        return sum(1 for m in self.messages if m.role == "user")

    def note_run(self, run_id: str, usage: Usage) -> None:
        self.runs.append(run_id)
        self.usage = self.usage.merged(usage)
        self.updated_at = now_iso()
        if not self.title:
            self.title = _derive_title(self.messages)


class SessionStore:
    """会话的落盘与读取。"""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = Path(directory) if directory else sessions_dir()

    @property
    def directory(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def new_id(self) -> str:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"{stamp}-{secrets.token_hex(2)}"

    def create(self) -> Session:
        stamp = now_iso()
        return Session(id=self.new_id(), created_at=stamp, updated_at=stamp)

    def save(self, session: Session) -> Path:
        path = self.directory / f"{session.id}.json"
        payload = {
            "id": session.id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "usage": session.usage.to_dict(),
            "runs": session.runs,
            "messages": [_message_to_dict(m) for m in session.messages],
        }
        # 先写临时文件再替换：中途崩溃不会留下半个 JSON
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, session_id: str) -> Session:
        path = self._resolve(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"读取会话 {path.name} 失败：{exc}") from exc

        return Session(
            id=str(payload.get("id") or path.stem),
            title=str(payload.get("title") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            usage=_usage_from_dict(payload.get("usage") or {}),
            runs=list(payload.get("runs") or []),
            messages=[_message_from_dict(d) for d in payload.get("messages") or []],
        )

    def list_ids(self, *, limit: int = 20) -> list[Session]:
        """按更新时间倒序列出会话（只读元信息，messages 留空以省内存）。"""
        paths = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out: list[Session] = []
        for path in paths[:limit]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append(
                Session(
                    id=str(payload.get("id") or path.stem),
                    title=str(payload.get("title") or ""),
                    created_at=str(payload.get("created_at") or ""),
                    updated_at=str(payload.get("updated_at") or ""),
                    usage=_usage_from_dict(payload.get("usage") or {}),
                    runs=list(payload.get("runs") or []),
                )
            )
        return out

    def latest(self) -> Session | None:
        items = self.list_ids(limit=1)
        return self.load(items[0].id) if items else None

    def _resolve(self, session_id: str) -> Path:
        raw = (session_id or "").strip()
        if not raw:
            raise SessionError("会话 id 不能为空")
        if raw in ("latest", "last"):
            items = self.list_ids(limit=1)
            if not items:
                raise SessionError("还没有任何会话")
            return self.directory / f"{items[0].id}.json"

        # 围栏：只认本目录内的会话文件。raw 会经 webapi 原样暴露给浏览器端，
        # 这里若接受"任意已存在的路径"，就是一个现成的任意文件读。
        if ".." in raw or "/" in raw or "\\" in raw:
            raise SessionError(f"非法的会话 id：{raw}")

        exact = self.directory / f"{raw}.json"
        if exact.is_file():
            return exact

        matches = sorted(self.directory.glob(f"*{raw}*.json"))
        if not matches:
            raise SessionError(f"找不到会话：{raw}（目录 {self.directory}）")
        return matches[0]


# ---------------------------------------------------------------------- 序列化


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _message_to_dict(message: Message) -> dict[str, Any]:
    """只写有值的字段 —— 会话文件要能被人直接打开读。"""
    data: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        data["content"] = message.content
    if message.tool_calls:
        data["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in message.tool_calls
        ]
    if message.tool_call_id:
        data["tool_call_id"] = message.tool_call_id
    if message.name:
        data["name"] = message.name
    if message.is_error:
        data["is_error"] = True
    if message.raw_blocks:
        # thinking 块等厂商私有内容必须一起存：恢复时少一个块就会被拒
        data["raw_blocks"] = message.raw_blocks
    return data


def _message_from_dict(data: dict[str, Any]) -> Message:
    return Message(
        role=data.get("role") or "user",
        content=data.get("content"),
        tool_calls=[
            ToolCall(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                arguments=str(c.get("arguments") or ""),
            )
            for c in data.get("tool_calls") or []
        ]
        or None,
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
        is_error=bool(data.get("is_error")),
        raw_blocks=data.get("raw_blocks"),
    )


def _usage_from_dict(data: dict[str, Any]) -> Usage:
    return Usage(
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        total_tokens=int(data.get("total_tokens") or 0),
        cache_read_tokens=int(data.get("cache_read_tokens") or 0),
        cache_write_tokens=int(data.get("cache_write_tokens") or 0),
    )


def _derive_title(messages: list[Message]) -> str:
    for message in messages:
        if message.role == "user" and message.content:
            text = " ".join(message.content.split())
            return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS] + "…"
    return "(空会话)"


__all__ = ["Session", "SessionError", "SessionStore"]
