"""Prompt 加载。

prompt 独立成文件、文件名带版本号，是为了让 eval 能对比不同版本的差异（P8 跑 A/B）。
不要把 prompt 硬编码进代码 —— 那样改一次就丢一次历史，也没法回滚。

版本：
- `system_v1.md` —— P0，只有一个 `stock_quote` 工具。
- `system_v2.md` —— 当前。工具面扩到四个，并补上"历史被压缩""数据时效"
  两条规则；"连续失败就停"也从"请求"改成了"运行时强制"的写法。

旧版本**不删**：会话里存的历史是按旧 prompt 产生的，做 A/B 对比时还要用。
"""

from __future__ import annotations

from importlib.resources import files

DEFAULT_PROMPT = "system_v2.md"


def load_prompt(name: str = DEFAULT_PROMPT) -> str:
    """从 `niulai/agent/prompts/` 读取 prompt 文本。"""
    resource = files(__package__) / "prompts" / name
    return resource.read_text(encoding="utf-8").strip()
