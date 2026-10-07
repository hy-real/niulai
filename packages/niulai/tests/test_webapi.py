"""Web 服务层测试：不跑真实 run（无网络、秒级），只验证只读端点、配置读写与守卫。

run 的完整链路（SSE 流、取消、断连清理）涉及真实 LLM 调用，属于
P3.5 任务级 eval 的范畴；这里守住的是"装配正确、协议不破、配置可读写"。
"""

from __future__ import annotations

import json
import os
import time

from fastapi.testclient import TestClient

from niulai.config import read_config_file
from niulai.llm.types import Message
from niulai.paths import trace_dir
from niulai.session import SessionStore
from niulai.webapi import create_app


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    return TestClient(create_app())


def test_tools端点列出数据源与工具(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/api/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources"] == ["eastmoney", "tencent", "sina"]
    assert data["routing"]["quote"] == ["eastmoney", "tencent", "sina"]
    assert data["routing"]["moneyflow"] == ["eastmoney", "sina"]
    names = {t["name"] for t in data["tools"]}
    assert {
        "stock_quote",
        "stock_kline",
        "stock_financial",
        "market_sectors",
        "sector_stocks",
        "stock_moneyflow",
        "read_file",
    } <= names


def test_sessions端点_空目录返回空列表(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/sessions").json() == []


def test_sessions端点_按更新时间倒序分页(monkeypatch, tmp_path):
    """limit/offset 供侧栏滚动分页：倒序、翻页不重不漏、返回不满页 = 到底。"""
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    client = _client(monkeypatch, tmp_path)
    store = SessionStore()
    for i in range(7):
        s = store.create()
        s.title = f"会话{i}"
        s.updated_at = f"2026-01-0{i + 1}T09:00:00+08:00"
        store.save(s)

    page1 = client.get("/api/sessions", params={"limit": 3}).json()
    page2 = client.get("/api/sessions", params={"limit": 3, "offset": 3}).json()
    page3 = client.get("/api/sessions", params={"limit": 3, "offset": 6}).json()

    assert [s["title"] for s in page1] == ["会话6", "会话5", "会话4"]
    assert [s["title"] for s in page2] == ["会话3", "会话2", "会话1"]
    assert [s["title"] for s in page3] == ["会话0"]      # 不满一页 = 没有更多
    ids = [s["id"] for s in page1 + page2 + page3]
    assert len(ids) == len(set(ids)) == 7                # 不重不漏


def test_会话详情不存在返回404(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/api/sessions/不存在的id")
    assert resp.status_code == 404


def test_跨源Host头被拒绝(monkeypatch, tmp_path):
    """只绑 127.0.0.1 之外的第二道防线：防 DNS rebinding。"""
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/api/tools", headers={"Host": "evil.example.com"})
    assert resp.status_code == 403


def test_没有Key时发起run返回400(monkeypatch, tmp_path):
    """Key 缺失是配置问题不是服务器错误：400 + 可读的指引文案。"""
    for name in ("NIULAI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/runs", json={"task": "茅台多少钱"})
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_空问题返回422(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    client = _client(monkeypatch, tmp_path)
    resp = client.post("/api/runs", json={"task": "   "})
    assert resp.status_code == 422


class _StubRuntime:
    """假内核：不联网，立即返回一个最小 run 结果。"""

    async def run(self, task, *, emitter, messages, session_id):
        from niulai.llm.types import Usage

        class _Result:
            pass

        r = _Result()
        r.stop_reason, r.turns, r.usage, r.breaks = "done", 1, Usage(1, 1, 2), 0
        r.messages = [Message(role="user", content=task)]
        return r


def test_新会话在run开始前就落库(monkeypatch, tmp_path):
    """前端首帧拿到 session_id 就会刷新侧栏 —— 那时会话必须已经在库里，
    否则列表是空的，用户要等 run 结束（或手动点）才能看到会话。"""
    import types

    import niulai.webapi as webapi_mod

    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(
        webapi_mod, "_assemble", lambda settings: types.SimpleNamespace(runtime=_StubRuntime())
    )

    resp = client.post("/api/runs", json={"task": "茅台多少钱"})
    assert resp.status_code == 200

    store = SessionStore()
    items = store.list_ids()
    assert len(items) == 1
    assert items[0].title == "茅台多少钱"   # 创建时就带标题，侧栏立刻可读
    loaded = store.latest()
    assert loaded.messages[0].content == "茅台多少钱"
    assert loaded.runs                       # run 结束后照常更新内容


# ---------------------------------------------------------------- 配置端点


def test_读取配置端点返回生效值(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_API_KEY", "sk-secret-9999")
    client = _client(monkeypatch, tmp_path)
    data = client.get("/api/config").json()
    assert data["protocol"] == "openai"
    assert data["max_turns"] == 12
    assert data["has_key"] is True
    assert "9999" in data["key_hint"]  # Key 只回尾号，不回原文


def test_写入配置端点更新config文件(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    client = _client(monkeypatch, tmp_path)
    resp = client.put(
        "/api/config",
        json={"model": "moonshot-v1-8k", "base_url": "https://api.moonshot.cn/v1", "max_turns": 6},
    )
    assert resp.status_code == 200

    # 直接读配置文件验证（GET 是合并视图，验证不了"写进了文件"）
    cfg = read_config_file()
    assert cfg["llm"]["model"] == "moonshot-v1-8k"
    assert cfg["llm"]["base_url"] == "https://api.moonshot.cn/v1"
    assert cfg["llm"]["max_turns"] == 6


def test_写入配置时空字段不覆盖(monkeypatch, tmp_path):
    """空串 = 不修改：设置面板里留空的输入框不应把已有配置抹掉。"""
    monkeypatch.setenv("NIULAI_API_KEY", "sk-x")
    client = _client(monkeypatch, tmp_path)
    client.put("/api/config", json={"model": "kimi"})
    client.put("/api/config", json={"model": "", "base_url": ""})

    cfg = read_config_file()
    assert cfg["llm"]["model"] == "kimi"
    assert "base_url" not in cfg["llm"]


def test_写入配置时api_key留空不清空原key(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    client.put("/api/config", json={"api_key": "sk-from-ui"})
    client.put("/api/config", json={"api_key": ""})

    cfg = read_config_file()
    assert cfg["llm"]["api_key"] == "sk-from-ui"


def test_非法协议返回422(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.put("/api/config", json={"protocol": "gemini"})
    assert resp.status_code == 422


# ---------------------------------------------------------------- 并发登记表


def test_runregistry_reserve是同步占位_防并发穿透():
    """handler 返回响应与生成器启动之间有异步间隙：占位必须是同步原子的
    check+set，两个并发请求不可能同时占住同一个会话。"""
    from niulai.webapi import _RunRegistry

    reg = _RunRegistry()
    assert reg.reserve("s1", "r1")
    assert not reg.reserve("s1", "r2")  # 第二个请求被挡下（→ 409）
    assert reg.reserve("s2", "r3")      # 不同会话不受影响

    # task 后挂：占位先行，attach 之后 cancel 端点才能找到它
    assert reg.task_for("r1") is None

    reg.finish("r1")
    assert reg.reserve("s1", "r4")      # run 结束（或被清理）后占位释放


# ---------------------------------------------------------------- 中断恢复端点


def _write_journal(session_id: str, *, finished: bool) -> None:
    """在 trace 目录手工放一份 journal（与内核写入的形状一致）。"""
    d = trace_dir()
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "run_start", "run_id": "run-i1", "session_id": session_id,
                    "task": "茅台多少钱"}, ensure_ascii=False),
        json.dumps({"type": "assistant_message", "text": "",
                    "tool_calls": [{"id": "c1", "name": "stock_quote",
                                    "arguments": "{\"symbol\": \"600519\"}"}]},
                   ensure_ascii=False),
        json.dumps({"type": "tool_result", "name": "stock_quote", "ok": True,
                    "tool_call_id": "c1", "content": "1253.80 元", "preview": "1253.80"},
                   ensure_ascii=False),
    ]
    if finished:
        lines.append(json.dumps({"type": "run_end", "stop_reason": "done"}, ensure_ascii=False))
    (d / "run-i1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_session(store: SessionStore) -> str:
    session = store.create()
    session.messages = [Message(role="user", content="上一轮的问题")]
    store.save(session)
    return session.id


def test_未收尾run_被检测并可恢复(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    client = _client(monkeypatch, tmp_path)
    store = SessionStore()
    sid = _make_session(store)
    _write_journal(sid, finished=False)

    data = client.get(f"/api/sessions/{sid}/interrupted").json()
    assert data["interrupted"] is True
    assert data["run_id"] == "run-i1"
    assert data["task"] == "茅台多少钱"

    resp = client.post(f"/api/sessions/{sid}/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["repaired"] == 0  # 工具结果已落 journal，无需补"未知"
    # 恢复 = 旧历史 + 中断 run 的现场（提问 + 工具调用 + 结果），顺序不乱
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "user", "assistant", "tool"]
    assert body["messages"][1]["content"] == "茅台多少钱"
    assert body["messages"][3]["content"] == "1253.80 元"


def test_中断run_悬空调用被补结果未知(monkeypatch, tmp_path):
    """journal 里工具没回来：恢复时补「结果未知」，不猜成败。"""
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    client = _client(monkeypatch, tmp_path)
    store = SessionStore()
    sid = _make_session(store)
    d = trace_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-i2.jsonl").write_text(
        json.dumps({"type": "run_start", "run_id": "run-i2", "session_id": sid,
                    "task": "查五粮液"}, ensure_ascii=False) + "\n"
        + json.dumps({"type": "assistant_message", "text": "",
                      "tool_calls": [{"id": "c9", "name": "stock_quote", "arguments": "{}"}]},
                     ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    body = client.post(f"/api/sessions/{sid}/resume").json()
    assert body["repaired"] == 1
    tail = body["messages"][-1]
    assert tail["role"] == "tool" and tail["is_error"] is True


def test_已收尾的run_不算中断(monkeypatch, tmp_path):
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    client = _client(monkeypatch, tmp_path)
    sid = _make_session(SessionStore())
    _write_journal(sid, finished=True)
    assert client.get(f"/api/sessions/{sid}/interrupted").json() == {"interrupted": False}
    resp = client.post(f"/api/sessions/{sid}/resume")
    assert resp.status_code == 404


def test_中断后又跑完过新run_残留过期(monkeypatch, tmp_path):
    """中断现场比会话最后保存还旧：恢复会把消息插进历史中间 —— 拒绝展示。"""
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    client = _client(monkeypatch, tmp_path)
    store = SessionStore()
    sid = _make_session(store)
    _write_journal(sid, finished=False)
    # 把 journal 的 mtime 拨到过去，模拟"中断后又有新 run 正常收尾"
    old = time.time() - 3600
    os.utime(trace_dir() / "run-i1.jsonl", (old, old))
    store.save(store.load(sid))  # 会话文件重新落盘（mtime = 现在）

    assert client.get(f"/api/sessions/{sid}/interrupted").json() == {"interrupted": False}
    assert client.post(f"/api/sessions/{sid}/resume").status_code == 404
