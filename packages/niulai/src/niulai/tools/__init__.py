"""工具层。

对外暴露 `ToolRegistry` / `ToolResult` 与具体工具。
新增工具不需要改内核，只需要 `registry.add(...)`。

当前工具集：

| 工具 | 能力 | 做什么 |
| --- | --- | --- |
| `stock_quote` | read + network | 实时行情快照 |
| `stock_kline` | read + network | 历史 K 线 |
| `stock_indicator` | read + network | 技术指标（MA / MACD / KDJ / RSI / 布林 / 量能） |
| `stock_financial` | read + network | 定期报告主要指标 |
| `stock_valuation` | read + network | 估值快照（PE / PB / PS / PCF） |
| `stock_anomaly` | read + network | 当日异动原因 |
| `market_hot` | read + network | 同花顺热股榜 / 飙升榜 |
| `market_limitup` | read + network | 涨跌停 / 炸板池与连板天梯 |
| `market_dragontiger` | read + network | 龙虎榜 |
| `stock_moneyflow` | read + network | 个股每日资金流 |
| `stock_auction` | read + network | 集合竞价快照（竞价涨幅 / 量比） |
| `market_auction_benchmark` | read + network | 短线风向标竞价基准 |
| `market_index` | read + network | 指数 / 板块行情与 K 线 |
| `read_file` | read | 读回落盘的大结果 |
| `write_file` | write_local | 写报告 / 产出到当前会话目录（越权与覆盖先问用户） |

`read_file` 与前几个不同：它不碰网络，只读本程序自己落盘的 artifact。
`write_file` 是第一个声明 `write_local` 的工具 —— 能力不同，策略层的判定
也不同（见 permissions.py）：会话目录内的新文件直接放行，覆盖 / 越目录 /
出围栏要么问用户、要么硬拒绝。
"""

from .base import MAX_RESULT_CHARS, READ_ONLY, Capability, Tool, ToolRegistry, ToolResult
from .files import READ_FILE_TOOL_NAME, ReadFileTool
from .indicator import INDICATOR_TOOL_NAME, StockIndicatorTool
from .market import (
    AUCTION_TOOL_NAME,
    BENCHMARK_TOOL_NAME,
    DRAGON_TIGER_TOOL_NAME,
    HOT_TOOL_NAME,
    INDEX_TOOL_NAME,
    LIMITUP_TOOL_NAME,
    MONEYFLOW_TOOL_NAME,
    MarketAuctionBenchmarkTool,
    MarketDragonTigerTool,
    MarketHotTool,
    MarketIndexTool,
    MarketLimitPoolTool,
    StockAuctionTool,
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
from .write import WRITE_FILE_TOOL_NAME, WriteFileTool

__all__ = [
    "ANOMALY_TOOL_NAME",
    "AUCTION_TOOL_NAME",
    "BENCHMARK_TOOL_NAME",
    "DRAGON_TIGER_TOOL_NAME",
    "HOT_TOOL_NAME",
    "INDEX_TOOL_NAME",
    "FINANCIAL_TOOL_NAME",
    "HOT_TOOL_NAME",
    "INDICATOR_TOOL_NAME",
    "KLINE_TOOL_NAME",
    "LIMITUP_TOOL_NAME",
    "MAX_RESULT_CHARS",
    "MONEYFLOW_TOOL_NAME",
    "QUOTE_TOOL_NAME",
    "READ_FILE_TOOL_NAME",
    "READ_ONLY",
    "VALUATION_TOOL_NAME",
    "WRITE_FILE_TOOL_NAME",
    "Capability",
    "MarketAuctionBenchmarkTool",
    "MarketDragonTigerTool",
    "MarketHotTool",
    "MarketIndexTool",
    "MarketLimitPoolTool",
    "ReadFileTool",
    "StockAnomalyTool",
    "StockFinancialTool",
    "StockIndicatorTool",
    "StockKlineTool",
    "StockMoneyflowTool",
    "StockAuctionTool",
    "StockQuoteTool",
    "StockValuationTool",
    "Tool",
    "WriteFileTool",
    "ToolRegistry",
    "ToolResult",
    "build_market_tools",
    "build_stock_tools",
]
