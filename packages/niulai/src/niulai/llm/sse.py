"""Server-Sent Events 的一行解析。

OpenAI 和 Anthropic 都用 SSE，只是事件内容不同 —— 解析外层协议这件事
两家可以共用，所以单独放一个模块。

Anthropic 会发 `event: xxx` 行，但 `data:` 里同样带 `"type"` 字段，
所以这里统一忽略 `event:` / `:keep-alive` 这类行，只取 JSON。
"""

from __future__ import annotations

import json
from typing import Any

SSE_DATA_PREFIX = "data:"


def parse_sse_line(line: str) -> dict[str, Any] | None:
    """解析一行 SSE。返回 None 表示这行不用管。

    解析失败（半行 / 心跳）也返回 None —— 流式响应里出现一行读不通的内容
    不该让整个请求失败。
    """
    if not line:
        return None
    line = line.strip()
    if not line.startswith(SSE_DATA_PREFIX):
        return None
    data = line[len(SSE_DATA_PREFIX) :].strip()
    if not data or data == "[DONE]":  # [DONE] 是 OpenAI 的结束哨兵，Anthropic 没有
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
