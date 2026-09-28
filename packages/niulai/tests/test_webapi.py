"""Web 服务层测试：不跑真实 run（无网络、秒级），只验证只读端点与守卫。

run 的完整链路（SSE 流、取消、断连清理）涉及真实 LLM 调用，属于
P3.5 任务级 eval 的范畴；这里守住的是"装配正确、协议不破"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

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
