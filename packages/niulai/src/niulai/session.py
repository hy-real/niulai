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

## 存储：SQLite 单文件

P4 起会话存进 `~/.niulai/niulai.db`（SQLite：单文件、零运维、stdlib 自带）：

- 每条消息一个 `payload` JSON —— **往返不丢东西**：`tool_calls` 和
  `raw_blocks` 少一样，恢复出来的 transcript 就发不出去
  （Anthropic 的 thinking 块缺了直接 400）；
- 保存 = 整段重写（消息全删全插，单事务）。会话是单写者的
  （webapi 按会话隔离并发，同一会话同时只有一个 run），
  不需要更细的增量写。
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import NiulaiError
from .llm.types import Message, ToolCall, Usage
from .paths import db_file

TITLE_CHARS = 40

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL DEFAULT '',
    updated_at         TEXT NOT NULL DEFAULT '',
    created_ms         INTEGER NOT NULL DEFAULT 0,
    updated_ms         INTEGER NOT NULL DEFAULT 0,
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    total_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    runs               TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS messages (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    PRIMARY KEY (session_id, ord)
);
"""

_SESSION_COLS = (
    "id, title, created_at, updated_at, prompt_tokens, completion_tokens,"
    " total_tokens, cache_read_tokens, cache_write_tokens, runs"
)


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
    """会话的落盘与读取（SQLite 单文件）。"""

    def __init__(self, db: Path | None = None) -> None:
        self._db = Path(db) if db else db_file()
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        """数据库文件位置。"""
        return self._db

    # ------------------------------------------------------------- 基础操作

    def new_id(self) -> str:
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"{stamp}-{secrets.token_hex(2)}"

    def create(self) -> Session:
        stamp = now_iso()
        return Session(id=self.new_id(), created_at=stamp, updated_at=stamp)

    def save(self, session: Session) -> Path:
        with closing(self._connect()) as conn, conn:
            _write(conn, session)
        return self._db

    def load(self, session_id: str) -> Session:
        sid = self._resolve(session_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
            if row is None:
                raise SessionError(f"找不到会话：{session_id}（库 {self._db.name}）")
            payloads = conn.execute(
                "SELECT payload FROM messages WHERE session_id = ? ORDER BY ord", (sid,)
            ).fetchall()
        return _session_from_row(row, _messages_from_payloads(payloads))

    def list_ids(self, *, limit: int = 20, offset: int = 0) -> list[Session]:
        """按更新时间倒序列出会话（只读元信息，messages 留空以省内存）。

        limit/offset 供分页：返回条数 < limit 即已翻到底。
        """
        limit, offset = max(0, int(limit)), max(0, int(offset))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {_SESSION_COLS} FROM sessions"
                " ORDER BY updated_ms DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    def latest(self) -> Session | None:
        items = self.list_ids(limit=1)
        return self.load(items[0].id) if items else None

    # ------------------------------------------------------------- 内部

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _resolve(self, session_id: str) -> str:
        """定位会话 id：精确匹配优先，其次子串（只认一个时直接采用）。

        围栏：输入经 webapi 原样暴露给浏览器端，这里只返回库内已存在的 id，
        不存在"按路径读任意文件"的可能 —— 围栏从"拒绝路径字符"收紧为
        "只认库内主键"。
        """
        raw = (session_id or "").strip()
        if not raw:
            raise SessionError("会话 id 不能为空")
        with closing(self._connect()) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY id")]

        if raw in ("latest", "last"):
            items = self.list_ids(limit=1)
            if not items:
                raise SessionError("还没有任何会话")
            return items[0].id

        if raw in ids:
            return raw

        matches = sorted(i for i in ids if raw in i)
        if not matches:
            raise SessionError(f"找不到会话：{raw}（库 {self._db}）")
        return matches[0]


# ---------------------------------------------------------------------- 序列化


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _ts_ms(iso: str) -> int:
    """ISO 时间转 epoch 毫秒（排序键）。解析不了就 0，沉底但不报错。"""
    try:
        return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        return 0


def _write(conn: sqlite3.Connection, session: Session) -> None:
    """写一个会话：元信息 upsert + 消息整段重写（全删全插）。

    必须在事务里调（`with conn:`）。单会话单写者（webapi 按会话隔离并发），
    事务保证中途崩溃不会留下"半段 transcript"。
    """
    conn.execute(
        "INSERT OR REPLACE INTO sessions"
        " (id, title, created_at, updated_at, created_ms, updated_ms,"
        "  prompt_tokens, completion_tokens, total_tokens,"
        "  cache_read_tokens, cache_write_tokens, runs)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            session.id,
            session.title,
            session.created_at,
            session.updated_at,
            _ts_ms(session.created_at),
            _ts_ms(session.updated_at),
            session.usage.prompt_tokens,
            session.usage.completion_tokens,
            session.usage.total_tokens,
            session.usage.cache_read_tokens,
            session.usage.cache_write_tokens,
            json.dumps(session.runs, ensure_ascii=False),
        ),
    )
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session.id,))
    conn.executemany(
        "INSERT INTO messages (session_id, ord, payload) VALUES (?,?,?)",
        [
            (session.id, i, json.dumps(_message_to_dict(m), ensure_ascii=False))
            for i, m in enumerate(session.messages)
        ],
    )


def _session_from_row(row: tuple, messages: list[Message] | None = None) -> Session:
    (
        sid, title, created_at, updated_at,
        prompt_tokens, completion_tokens, total_tokens,
        cache_read_tokens, cache_write_tokens, runs_json,
    ) = row
    return Session(
        id=str(sid),
        title=str(title or ""),
        created_at=str(created_at or ""),
        updated_at=str(updated_at or ""),
        usage=Usage(
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            total_tokens=int(total_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            cache_write_tokens=int(cache_write_tokens or 0),
        ),
        runs=list(json.loads(runs_json or "[]")),
        messages=messages if messages is not None else [],
    )


def _messages_from_payloads(payloads: list[tuple]) -> list[Message]:
    out: list[Message] = []
    for (payload,) in payloads:
        try:
            out.append(_message_from_dict(json.loads(payload)))
        except json.JSONDecodeError as exc:
            raise SessionError(f"消息数据损坏：{exc}") from exc
    return out


def _message_to_dict(message: Message) -> dict[str, Any]:
    """只写有值的字段。"""
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
