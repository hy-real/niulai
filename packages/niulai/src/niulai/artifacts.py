"""大结果落盘。

必须落盘而不是截断：截断会丢数据，而且模型不知道自己缺了什么 —— 它会
基于残缺数据给出一个看起来正确的结论，这比直接报错更糟。落盘则保留数据：
只把「摘要 + 路径」送进上下文，模型需要细节时再用 read_file 去取。这不是
优化项，是正确性问题。

路径安全：ArtifactStore 只认自己管的那个目录，`..`、绝对路径穿越、符号
链接逃逸一律拒绝。read_file 是第一个真正带"能力"的工具，它必须只能读
该读的地方。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import NiulaiError
from .paths import artifacts_dir

# 落盘阈值：超过这个字符数就不原样进上下文了。
# 放在这里而不是 tools/base.py，是因为"什么时候落盘"和"落到哪"是同一件事。
DEFAULT_INLINE_LIMIT = 4_000

# 下划线要保留：文件名前半截就是工具名（`stock_kline`），改掉会让人对不上号
_SLUG_RE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")


class ArtifactError(NiulaiError):
    """artifact 读写失败（含路径越界）。"""


@dataclass(slots=True)
class Artifact:
    name: str
    path: Path
    chars: int
    lines: int

    @property
    def ref(self) -> str:
        """给模型看的引用：相对**数据根目录**（`~/.niulai`）的路径。

        规范形式是 `artifacts/<run_id>/<文件名>`——read_file 的围栏就是
        数据根，模型拿到引用原样调用即可，无需知道任何内部目录结构。
        相对而非绝对：引用会被存进会话 JSON（工具结果文本），数据目录
        搬家后老会话的引用依然有效，也不会把用户名写进 trace。
        """
        return f"artifacts/{self.path.parent.name}/{self.path.name}"

    def describe(self) -> str:
        return f"{self.ref}（{self.chars:,} 字符 / {self.lines:,} 行）"


class ArtifactStore:
    """把大结果写成文件，并负责安全地读回来。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root else artifacts_dir()

    @property
    def root(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def dir_for(self, run_id: str) -> Path:
        """某个 run 的落盘目录。save 与删除共用这一套命名 —— 映射只有一份。"""
        return self.root / _slug(run_id or "shared")

    def remove_run(self, run_id: str) -> bool:
        """删掉某个 run 的全部落盘（删会话时按 run 清单逐个调）。

        空 run_id 拒删：save 里空 id 落到 "shared" 目录，那不属于任何
        会话，别顺走别人的数据。返回是否删干净了。
        """
        if not (run_id or "").strip():
            return False
        root = self.root.resolve()
        folder = self.dir_for(run_id).resolve()
        try:
            folder.relative_to(root)
        except ValueError:
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()

    # ---------------------------------------------------------------- 写

    def save(self, content: str, *, name: str = "result", run_id: str = "shared") -> Artifact:
        """落盘一份内容，返回可被 `read_file` 引用的 Artifact。"""
        folder = self.dir_for(run_id)
        folder.mkdir(parents=True, exist_ok=True)

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        filename = f"{_slug(name or 'result')}-{digest}{_extension_for(content)}"
        path = folder / filename

        if not path.exists():  # 同样内容重复落盘就复用（工具被重试时常见）
            path.write_text(content, encoding="utf-8")

        return Artifact(
            name=filename,
            path=path,
            chars=len(content),
            lines=content.count("\n") + 1,
        )

    # ---------------------------------------------------------------- 读

    def resolve(self, ref: str) -> Path:
        """把引用解析成真实路径，越界一律拒绝。

        接受的写法（都必须在 artifacts 根目录之内）：
        - `run_id/file.json`  相对路径
        - `file.json`         纯文件名（在根下递归找，找不到多个就报错）
        """
        raw = (ref or "").strip().strip('"').strip("'")
        if not raw:
            raise ArtifactError("没有给出文件路径")

        root = self.root.resolve()
        candidate = Path(raw)

        # 兼容两种引用形式：规范形式带 `artifacts/` 前缀（相对数据根），
        # 旧会话里存的是不带前缀的（相对本目录）。store 对两者都认——
        # read_file 的围栏在数据根那层，这里只管把文件找出来。
        if candidate.parts and candidate.parts[0] == "artifacts":
            candidate = Path(*candidate.parts[1:])

        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (root / candidate).resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ArtifactError(
                f"拒绝访问 `{raw}`：只能读取 artifacts 目录内的文件"
                f"（当前 artifacts 目录：{root}）。"
                "如果你需要的是别的文件，请让用户明确说明。"
            ) from exc

        if resolved.is_file():
            return resolved

        # 纯文件名：在根下按名字找
        if candidate.parent == Path("."):
            matches = sorted(root.rglob(candidate.name))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                options = "、".join(m.relative_to(root).as_posix() for m in matches[:5])
                raise ArtifactError(f"`{raw}` 匹配到多个文件，请写完整路径：{options}")

            # 文件名里带着内容摘要（`stock_kline-3f9c1a2b.csv`），模型经常只记住
            # 前面那半截。所以再按"包含"找一次 —— 这是真实会发生的用法，
            # 不是宽容：路径本来就只由我们生成，不存在歧义风险。
            loose = sorted(root.rglob(f"*{candidate.stem}*"))
            if len(loose) == 1:
                return loose[0]
            if len(loose) > 1:
                options = "、".join(m.relative_to(root).as_posix() for m in loose[:5])
                raise ArtifactError(f"`{raw}` 匹配到多个文件，请写完整路径：{options}")

            raise ArtifactError(
                f"找不到 artifact：`{raw}`。可用 `read_file` 不传路径来列出全部。"
            )

        raise ArtifactError(f"找不到 artifact：`{raw}`")

    def list_all(self, *, limit: int = 50) -> list[Artifact]:
        root = self.root
        items = [p for p in root.rglob("*") if p.is_file()]
        items.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        out: list[Artifact] = []
        for path in items[:limit]:
            text = _safe_read(path)
            out.append(
                Artifact(
                    name=path.name,
                    path=path,
                    chars=len(text),
                    lines=text.count("\n") + 1,
                )
            )
        return out

    def read(self, ref: str, *, offset: int = 1, limit: int = 200) -> tuple[str, int, int]:
        """读一段文本，返回 `(内容, 总行数, 本次起始行)`。

        支持分页，因为"读一个文件的第 500-600 行"是真实需求 ——
        一次性把 5 万行灌回上下文，等于把 artifact 的收益又还回去了。
        """
        path = self.resolve(ref)
        text = _safe_read(path)
        lines = text.splitlines()
        total = len(lines)

        start = max(0, offset - 1)
        start = min(start, max(0, total - 1)) if total else 0
        chunk = lines[start : start + max(1, limit)]
        return "\n".join(chunk), total, start + 1


# ---------------------------------------------------------------------- 工具


def _slug(value: str) -> str:
    cleaned = _SLUG_RE.sub("-", value).strip("-")
    return cleaned[:48] or "x"


def _extension_for(content: str) -> str:
    head = content.lstrip()[:1]
    if head in ("{", "["):
        return ".json"
    # 典型 K 线 CSV 是逗号分隔且多行
    first_line = content.split("\n", 1)[0]
    if content.count("\n") >= 3 and first_line.count(",") >= 2:
        return ".csv"
    return ".txt"


def _safe_read(path: Path, *, max_chars: int = 8_000_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_chars)
    except OSError as exc:
        raise ArtifactError(f"读取 {path.name} 失败：{exc}") from exc
