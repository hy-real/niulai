"""MCP server 入口（P9 实现）。

计划暴露的工具（直接复用 `niulai.datasource`，不重复写取数逻辑）：

| 工具 | 说明 |
| --- | --- |
| `quote` | 单只标的的行情快照 |
| `kline` | 日 / 周 / 月 K 线 |
| `financial` | 主要财务指标 |

传输：stdio（本地客户端首选）+ Streamable HTTP。
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "niulai-mcp 尚未实现（计划在 P9）。\n"
        "现在的数据源层已经可用：pip install niulai 后用 niulai.datasource 直接调用。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
