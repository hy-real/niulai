"""工具层。

对外暴露 `ToolRegistry` / `ToolResult` 与具体工具。
新增工具不需要改内核，只需要 `registry.add(...)`。

当前工具集：

| 工具 | 能力 | 做什么 |
| --- | --- | --- |
| `stock_quote` | read + network | 实时行情快照 |
| `stock_kline` | read + network | 历史 K 线 |
| `stock_financial` | read + network | 定期报告主要指标 |
| `stock_valuation` | read + network | 估值快照（PE / PB / PS / PCF） |
| `stock_anomaly` | read + network | 当日异动原因 |
| `market_hot` | read + network | 同花顺热股榜 / 飙升榜 |
| `market_limitup` | read + network | 涨跌停 / 炸板池与连板天梯 |
| `market_dragontiger` | read + network | 龙虎榜 |
| `stock_moneyflow` | read + network | 个股每日资金流 |
| `read_file` | read | 读回落盘的大结果 |

`read_file` 与前几个不同：它不碰网络，只读本程序自己落盘的 artifact。
这是"工具声明能力"的第一个真实用例 —— 能力不同，将来策略层的判定也该不同。
"""

from .base import MAX_RESULT_CHARS, READ_ONLY, Capability, Tool, ToolRegistry, ToolResult
from .files import READ_FILE_TOOL_NAME, ReadFileTool
from .market import (
    DRAGON_TIGER_TOOL_NAME,
    HOT_TOOL_NAME,
    LIMITUP_TOOL_NAME,
    MONEYFLOW_TOOL_NAME,
    MarketDragonTigerTool,
    MarketHotTool,
    MarketLimitPoolTool,
    StockMoneyflowTool,
    build_market_tools,
)
from .stock import (
    ANOMALY_TOOL_NAME,
    FINANCIAL_TOOL_NAME,
    KLINE_TOOL_NAME,
    QUOTE_TOOL_NAME,
    VALUATION_TOOL_NAME,
    StockAnomalyTool,
    StockFinancialTool,
    StockKlineTool,
    StockQuoteTool,
    StockValuationTool,
    build_stock_tools,
)

__all__ = [
    "ANOMALY_TOOL_NAME",
    "DRAGON_TIGER_TOOL_NAME",
    "FINANCIAL_TOOL_NAME",
    "HOT_TOOL_NAME",
    "KLINE_TOOL_NAME",
    "LIMITUP_TOOL_NAME",
    "MAX_RESULT_CHARS",
    "MONEYFLOW_TOOL_NAME",
    "QUOTE_TOOL_NAME",
    "READ_FILE_TOOL_NAME",
    "READ_ONLY",
    "VALUATION_TOOL_NAME",
    "Capability",
    "MarketDragonTigerTool",
    "MarketHotTool",
    "MarketLimitPoolTool",
    "ReadFileTool",
    "StockAnomalyTool",
    "StockFinancialTool",
    "StockKlineTool",
    "StockMoneyflowTool",
    "StockQuoteTool",
    "StockValuationTool",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "build_market_tools",
    "build_stock_tools",
]
