"""工具层。

对外暴露 `ToolRegistry` / `ToolResult` 与具体工具。
新增工具不需要改内核，只需要 `registry.add(...)`。

当前工具集：

| 工具 | 能力 | 做什么 |
| --- | --- | --- |
| `stock_quote` | read + network | 实时行情快照 |
| `stock_kline` | read + network | 历史 K 线 |
| `stock_financial` | read + network | 定期报告主要指标 |
| `read_file` | read | 读回落盘的大结果 |

`read_file` 与前三个不同：它不碰网络，只读本程序自己落盘的 artifact。
这是"工具声明能力"的第一个真实用例 —— 能力不同，将来策略层的判定也该不同。
"""

from .base import MAX_RESULT_CHARS, READ_ONLY, Capability, Tool, ToolRegistry, ToolResult
from .files import READ_FILE_TOOL_NAME, ReadFileTool
from .stock import (
    FINANCIAL_TOOL_NAME,
    KLINE_TOOL_NAME,
    QUOTE_TOOL_NAME,
    StockFinancialTool,
    StockKlineTool,
    StockQuoteTool,
    build_stock_tools,
)

__all__ = [
    "FINANCIAL_TOOL_NAME",
    "KLINE_TOOL_NAME",
    "MAX_RESULT_CHARS",
    "QUOTE_TOOL_NAME",
    "READ_FILE_TOOL_NAME",
    "READ_ONLY",
    "Capability",
    "ReadFileTool",
    "StockFinancialTool",
    "StockKlineTool",
    "StockQuoteTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_stock_tools",
]
