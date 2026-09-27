"""artifact 落盘与读取测试。

两组重点：

1. **路径围栏。** `read_file` 是第一个带边界限制的工具 —— 它只能读 artifacts
   目录内的文件。越界必须被拒绝，而且要拒绝得明确（回灌成可读的指引）。
2. **分页。** 一次性把 5 万行灌回上下文，等于把落盘省下的空间又还回去。
"""

from __future__ import annotations

import pytest

from niulai.artifacts import ArtifactError, ArtifactStore


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(root=tmp_path / "artifacts")


# ---------------------------------------------------------------------- 写


def test_落盘后能读回(store):
    artifact = store.save("a,b,c\n1,2,3\n", name="stock_kline", run_id="run1")
    assert artifact.chars == 12
    assert artifact.path.exists()
    assert store.read(artifact.ref)[0].startswith("a,b,c")


def test_引用是相对数据根的规范形式(store):
    """规范形式 = `artifacts/<run_id>/<文件名>`，read_file 的围栏是数据根，
    引用照抄即可解析；相对而非绝对——引用会存进会话 JSON，搬家不失效。"""
    artifact = store.save("hello", name="x", run_id="run1")
    assert artifact.ref.startswith("artifacts/run1/")
    assert "/" in artifact.ref


def test_同样内容复用同一个文件(store):
    """工具被重试时很常见，落两份就是垃圾。"""
    first = store.save("same content", name="x", run_id="run1")
    second = store.save("same content", name="x", run_id="run1")
    assert first.path == second.path
    assert len(list(store.root.rglob("*.txt"))) == 1


def test_扩展名按内容推断(store):
    assert store.save('{"a": 1}', name="j").path.suffix == ".json"
    assert store.save("a,b,c\n1,2,3\n4,5,6\n7,8,9", name="c").path.suffix == ".csv"
    assert store.save("就是一段话", name="t").path.suffix == ".txt"


def test_文件名里的非法字符会被清掉(store):
    artifact = store.save("x", name='stock_kline/"../.."/evil', run_id="run1")
    assert "/" not in artifact.name.replace("/", "", 1) or artifact.path.parent.name == "run1"
    assert artifact.path.parent == store.root / "run1"


# ---------------------------------------------------------------------- 路径围栏


def test_相对路径越界被拒绝(store):
    store.save("data", name="ok", run_id="run1")
    with pytest.raises(ArtifactError) as exc:
        store.resolve("../../etc/passwd")
    assert "拒绝访问" in str(exc.value)
    assert "artifacts 目录" in str(exc.value)


def test_绝对路径越界被拒绝(store, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("不该被读到", encoding="utf-8")
    with pytest.raises(ArtifactError):
        store.resolve(str(outside))


def test_空路径被拒绝(store):
    with pytest.raises(ArtifactError):
        store.resolve("   ")


def test_不存在的文件报清楚(store):
    with pytest.raises(ArtifactError) as exc:
        store.resolve("run1/不存在.txt")
    assert "找不到" in str(exc.value)


def test_重名文件要求写全路径(store):
    store.save("one", name="same", run_id="run1")
    store.save("two", name="same", run_id="run2")
    with pytest.raises(ArtifactError) as exc:
        store.resolve("same.txt")
    assert "多个文件" in str(exc.value)


# ---------------------------------------------------------------------- 读


def test_纯文件名可以在根下找到(store):
    store.save("only one", name="unique", run_id="run1")
    text, total, start = store.read("unique.txt")
    assert text == "only one"
    assert total == 1
    assert start == 1


def test_分页读(store):
    content = "\n".join(f"第{i}行" for i in range(1, 31))
    artifact = store.save(content, name="lines", run_id="run1")

    text, total, start = store.read(artifact.ref, offset=11, limit=5)
    assert total == 30
    assert start == 11
    assert text.splitlines() == ["第11行", "第12行", "第13行", "第14行", "第15行"]


def test_越界的页码会被夹回文件内(store):
    artifact = store.save("a\nb\nc", name="tiny", run_id="run1")
    text, total, start = store.read(artifact.ref, offset=999, limit=10)
    assert total == 3
    assert text == "c"  # 夹到最后一行的开头，返回空会更让人困惑


def test_列出全部文件按时间倒序(store):
    import os

    first = store.save("first", name="a", run_id="run1")
    second = store.save("second", name="b", run_id="run1")
    # 同一秒内建的两个文件 mtime 可能相同，显式错开，否则这条测的是运气
    os.utime(first.path, (1_600_000_000, 1_600_000_000))
    os.utime(second.path, (1_700_000_000, 1_700_000_000))

    items = store.list_all()
    assert len(items) == 2
    assert items[0].chars == len("second")  # 最新写的排第一


def test_描述里带规模(store):
    artifact = store.save("a,b\n1,2\n3,4", name="x", run_id="run1")
    assert "字符" in artifact.describe()
    assert "行" in artifact.describe()
