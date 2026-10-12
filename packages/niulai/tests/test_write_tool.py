"""write_file 工具测试：围栏、目录创建、与 read_file 的往返。

策略判定（放行 / 询问 / 拒绝）在 permissions.py 测；这里只证明工具本身：
被允许的事一定做得到，围栏外的事直接调也做不了。
"""

from __future__ import annotations

import asyncio

from niulai.artifacts import ArtifactStore
from niulai.tools import ReadFileTool, WriteFileTool


def _run(tool, args):
    return asyncio.run(tool.run(args))


def test_写入新文件并自动建目录(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)

    out = _run(tool, {"path": "sessions/sess-1/茅台-分析.md", "content": "# 报告\n结论"})

    assert "已写入" in out
    target = tmp_path / "sessions" / "sess-1" / "茅台-分析.md"
    assert target.read_text(encoding="utf-8") == "# 报告\n结论"


def test_写完后可以用read_file原样读回(tmp_path):
    """两个工具同一个根、同一种路径形式 —— 写进去的引用读得回来。"""
    writer = WriteFileTool(data_root=tmp_path)
    reader = ReadFileTool(ArtifactStore(root=tmp_path / "artifacts"), data_root=tmp_path)

    _run(writer, {"path": "sessions/sess-1/a.md", "content": "第一行\n第二行"})
    text = _run(reader, {"path": "sessions/sess-1/a.md"})

    assert "第一行" in text and "第二行" in text


def test_覆盖已存在文件(tmp_path):
    """覆盖 = 修改，要不要覆盖由策略门去问用户；工具保证"被允许就一定做得到"。"""
    tool = WriteFileTool(data_root=tmp_path)
    target = tmp_path / "sessions" / "sess-1" / "a.md"
    target.parent.mkdir(parents=True)
    target.write_text("旧", encoding="utf-8")

    _run(tool, {"path": "sessions/sess-1/a.md", "content": "新"})

    assert target.read_text(encoding="utf-8") == "新"


def test_目录外绝对路径被围栏拒绝(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)
    outside = tmp_path.parent / "别处.txt"

    out = _run(tool, {"path": str(outside), "content": "x"})

    assert "拒绝写入" in out
    assert not outside.exists()


def test_点点点逃逸被围栏拒绝(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)

    out = _run(tool, {"path": "../逃逸.txt", "content": "x"})

    assert "拒绝写入" in out
    assert not (tmp_path.parent / "逃逸.txt").exists()


def test_缺少参数时给可恢复指引(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)

    assert "缺少 path" in _run(tool, {"content": "x"})
    assert "缺少 content" in _run(tool, {"path": "sessions/s/a.md"})


def test_目标是已存在目录时换文件名(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)
    (tmp_path / "sessions" / "sess-1").mkdir(parents=True)

    out = _run(tool, {"path": "sessions/sess-1", "content": "x"})

    assert "是目录" in out


def test_能力声明只有write_local(tmp_path):
    tool = WriteFileTool(data_root=tmp_path)
    assert tool.capabilities == frozenset({"write_local"})
