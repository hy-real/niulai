"""工具层测试：参数校验、错误回灌、超时、截断。

这些路径比"正常跑通"更重要 —— 模型能不能自愈，全看错误信息写得够不够可执行。
"""

from __future__ import annotations

import asyncio

import pytest

from niulai.llm import ToolCall, ToolSpec
from niulai.tools import ToolRegistry
from niulai.tools.base import _truncate


class _EchoTool:
    spec = ToolSpec(
        name="echo",
        description="回显传入的文本",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    async def run(self, args: dict) -> str:
        return f"echo:{args['text']}"


class _BoomTool:
    spec = ToolSpec(
        name="boom",
        description="总是抛异常",
        parameters={"type": "object", "properties": {}},
    )

    async def run(self, args: dict) -> str:
        raise RuntimeError("上游接口 500")


class _SlowTool:
    spec = ToolSpec(
        name="slow",
        description="很慢",
        parameters={"type": "object", "properties": {}},
    )

    async def run(self, args: dict) -> str:
        await asyncio.sleep(5)
        return "done"


def _registry() -> ToolRegistry:
    return ToolRegistry([_EchoTool(), _BoomTool(), _SlowTool()])


async def test_正常执行():
    result = await _registry().execute(
        ToolCall(id="1", name="echo", arguments='{"text":"hi"}'), timeout_s=1
    )
    assert result.ok
    assert result.content == "echo:hi"


async def test_未知工具_应提示可用工具名():
    result = await _registry().execute(ToolCall(id="1", name="nope", arguments="{}"), timeout_s=1)
    assert not result.ok
    assert "echo" in result.content


async def test_参数不是合法JSON_应给出可恢复提示():
    result = await _registry().execute(
        ToolCall(id="1", name="echo", arguments="{text: hi}"), timeout_s=1
    )
    assert not result.ok
    assert "JSON" in result.content


async def test_缺少必填参数_应回灌参数定义():
    result = await _registry().execute(ToolCall(id="1", name="echo", arguments="{}"), timeout_s=1)
    assert not result.ok
    assert "text" in result.content


async def test_工具抛异常_应转成可读提示而不是崩掉():
    result = await _registry().execute(ToolCall(id="1", name="boom", arguments="{}"), timeout_s=1)
    assert not result.ok
    assert "RuntimeError" in result.content


async def test_超时_应中止并提示缩小范围():
    result = await _registry().execute(ToolCall(id="1", name="slow", arguments="{}"), timeout_s=0.05)
    assert not result.ok
    assert "中止" in result.content


def test_截断_保留头部并标注():
    text, truncated = _truncate("x" * 100, limit=10)
    assert truncated
    assert text.startswith("x" * 10)
    assert "截断" in text


def test_未超限不截断():
    text, truncated = _truncate("hello", limit=100)
    assert not truncated
    assert text == "hello"


# --------------------------------------------------------------------------
# 大结果落盘（取代"静默截断"）
# --------------------------------------------------------------------------


class _HugeTool:
    spec = ToolSpec(
        name="huge",
        description="返回超长内容",
        parameters={"type": "object", "properties": {}},
    )
    capabilities = frozenset({"read"})

    async def run(self, args: dict) -> str:
        return "\n".join(f"行{i}" for i in range(1, 500))


@pytest.fixture
def store(tmp_path):
    from niulai.artifacts import ArtifactStore

    return ArtifactStore(root=tmp_path / "artifacts")


async def test_结果超限时落盘并只回摘要(store):
    registry = ToolRegistry([_HugeTool()], artifact_store=store, inline_limit=100)
    result = await registry.execute(
        ToolCall(id="1", name="huge", arguments="{}"), timeout_s=1, run_id="run1"
    )

    assert result.ok
    assert len(result.content) < 700  # 进上下文的是摘要，不是 499 行
    assert result.meta["content_chars"] > 2000  # 原始规模照样报出来
    assert result.meta["artifact"]
    assert "已完整保存为文件" in result.content
    assert 'read_file(path=' in result.content
    # 这一句是关键：模型必须知道预览是被截断的开头，否则会基于残数据下结论
    assert "不代表全部数据" in result.content


async def test_落盘的文件内容与原始结果一致(store):
    registry = ToolRegistry([_HugeTool()], artifact_store=store, inline_limit=100)
    result = await registry.execute(
        ToolCall(id="1", name="huge", arguments="{}"), timeout_s=1, run_id="run1"
    )
    text, total, _ = store.read(result.meta["artifact"])
    assert total > 400
    assert text.startswith("行1")


async def test_没配置store时退回截断并说明(store):
    """极简用法（不落盘）也要能用，但不能假装数据是完整的。"""
    registry = ToolRegistry([_HugeTool()], inline_limit=100)
    result = await registry.execute(ToolCall(id="1", name="huge", arguments="{}"), timeout_s=1)
    assert result.ok
    assert result.meta.get("artifact") is None
    assert "截断" in result.content


async def test_小结果原样进上下文(store):
    registry = ToolRegistry([_EchoTool()], artifact_store=store, inline_limit=100)
    result = await registry.execute(ToolCall(id="1", name="echo", arguments='{"text":"hi"}'), timeout_s=1)
    assert result.content == "echo:hi"
    assert result.meta.get("artifact") is None


# --------------------------------------------------------------------------
# read_file 工具：只能读自己落盘的东西
# --------------------------------------------------------------------------


async def test_read_file_列出全部文件(store, tmp_path):
    from niulai.tools import ReadFileTool

    store.save("a,b\n1,2\n3,4\n5,6", name="stock_kline", run_id="run1")
    tool = ReadFileTool(store, data_root=tmp_path)

    output = await tool.run({})
    assert "可读文件" in output
    assert "stock_kline" in output


async def test_read_file_能分页读(store, tmp_path):
    from niulai.tools import ReadFileTool

    artifact = store.save("\n".join(f"第{i}行" for i in range(1, 101)), name="big", run_id="run1")
    tool = ReadFileTool(store, data_root=tmp_path)

    # 规范形式：引用相对数据根（artifacts/run1/...），走数据根解析，不经过 store
    output = await tool.run({"path": artifact.ref, "offset": 11, "limit": 5})
    assert "第 11–15 行 / 共 100 行" in output
    assert "第11行" in output
    assert "用 offset=16 继续读" in output


async def test_read_file_老会话里的旧引用仍可读(store, tmp_path):
    """兼容层：旧版引用（run1/big-xxx.csv，相对 artifacts 子目录）经 store 解析。"""
    from niulai.tools import ReadFileTool

    artifact = store.save("旧格式内容", name="legacy", run_id="run1")
    old_ref = artifact.ref.removeprefix("artifacts/")  # 模拟老会话里存的形状
    tool = ReadFileTool(store, data_root=tmp_path)

    output = await tool.run({"path": old_ref})
    assert "旧格式内容" in output


async def test_read_file_越界时给可恢复的指引(store, tmp_path):
    """越界不能只是抛异常 —— 要告诉模型接下来能做什么。"""
    from niulai.tools import ReadFileTool

    store.save("x", name="ok", run_id="run1")
    tool = ReadFileTool(store, data_root=tmp_path)

    output = await tool.run({"path": "../../../etc/passwd"})
    assert "拒绝访问" in output
    assert "可以调用 read_file 不传 path" in output


async def test_read_file_没有文件时说清楚(store, tmp_path):
    from niulai.tools import ReadFileTool

    output = await ReadFileTool(store, data_root=tmp_path).run({})
    assert "没有落盘的文件" in output


async def test_read_file_能力声明不含network(store, tmp_path):
    """它不出网、不写盘 —— 能力声明要如实。将来策略层靠它判断。"""
    from niulai.tools import ReadFileTool

    capabilities = ReadFileTool(store, data_root=tmp_path).capabilities
    assert "read" in capabilities
    assert "network" not in capabilities
    assert "write_local" not in capabilities


# --------------------------------------------------------------------------
# read_file 边界 = 整个数据目录（2026-09-27 放宽，见 architecture.md 7.12）
# --------------------------------------------------------------------------


def _tool_with_data_root(store, tmp_path):
    from niulai.tools import ReadFileTool

    return ReadFileTool(store, data_root=tmp_path)


async def test_read_file_能读数据目录内的其他文件(store, tmp_path):
    """trace / 会话 / 日志都是程序自己生成的，整个数据目录都应该可读。"""
    traces = tmp_path / "traces"
    traces.mkdir()
    (traces / "run1.jsonl").write_text('{"type": "run_start"}\n', encoding="utf-8")

    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({"path": "traces/run1.jsonl"})
    assert "run_start" in output
    assert "第 1–1 行 / 共 1 行" in output


async def test_read_file_绝对路径落在数据目录内也可读(store, tmp_path):
    """模型可能给绝对路径——只要在围栏内就放行。"""
    target = tmp_path / "config.toml"
    target.write_text("[llm]\nmodel = 'x'\n", encoding="utf-8")

    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({"path": str(target)})
    assert "model" in output


async def test_read_file_读config时对api_key脱敏(store, tmp_path):
    """config.toml 里可能存着 Key（webapi 设置面板会写入）；工具结果会进
    模型上下文（= 随下一次请求发到模型厂商），明文 Key 不能走这条路。
    只脱敏值本身，其余配置项照常可读。"""
    (tmp_path / "config.toml").write_text(
        '[llm]\nmodel = "x"\napi_key = "sk-secret-1234567890"\n', encoding="utf-8"
    )

    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({"path": "config.toml"})
    assert "model" in output
    assert "sk-secret-1234567890" not in output
    assert "已脱敏" in output


async def test_read_file_数据目录内找不到时给相对路径写法指引(store, tmp_path):
    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({"path": "traces/不存在.jsonl"})
    assert "找不到文件" in output
    assert "相对路径" in output


async def test_read_file_数据目录外的绝对路径被围栏拒绝(store, tmp_path):
    """目录外 = 将来走审批通道的占位：现在如实拒绝，且不让模型换路径重试。"""
    outside = tmp_path.parent / "数据目录之外.txt"

    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({"path": str(outside)})
    assert "拒绝访问" in output
    assert "尚未实现" in output
    assert "不要换路径重试" in output


async def test_read_file_索引提示数据目录内的其他可读项(store, tmp_path):
    tool = _tool_with_data_root(store, tmp_path)
    output = await tool.run({})
    assert "traces/" in output
    assert "sessions/" in output

