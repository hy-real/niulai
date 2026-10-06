"""Web 服务层测试：不跑真实 run（无网络、秒级），只验证只读端点、配置读写与守卫。

run 的完整链路（SSE 流、取消、断连清理）涉及真实 LLM 调用，属于
P3.5 任务级 eval 的范畴；这里守住的是"装配正确、协议不破、配置可读写"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from niulai.config import read_config_file
from niulai.webapi import create_app


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("NIULAI_HOME", str(tmp_path))
    return TestClient(create_app())


def test_tools端点列出数据源与工具(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    resp = client.get("/api/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sources"] == ["eastmoney"]
    assert data["routing"]["quote"] == ["eastmoney"]
    names = {t["name"] for t in data["tools"]}
    assert {"stock_quote", "stock_kline", "stock_financial", "read_file"} <= names


def test_sessions端点_空目录返回空列表(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/sessions").json() == []


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
