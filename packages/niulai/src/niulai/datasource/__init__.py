"""数据源层。

对外的名字：`MarketDataService`（工具只用它）和各返回类型
（`Quote` / `KlineSeries` / `FinancialSummary` / `HotList` / `LimitPool` …）。
具体数据源（同花顺 / 东财 / 腾讯）都藏在里面。
"""

from .base import (
    ADJUST_LABEL,
    AnomalyList,
    AnomalyRow,
    AuctionBenchmark,
    AuctionRow,
    AuctionSnapshot,
    BenchmarkRow,
    Capability,
    DragonTigerBoard,
    DragonTigerRow,
    FinancialPeriod,
    FinancialSummary,
    HotList,
    HotRow,
    KlineBar,
    KlineSeries,
    LadderDay,
    LimitLadder,
    LimitPool,
    LimitPoolRow,
    MarketDataSource,
    MoneyflowDay,
    MoneyflowSeries,
    PERIOD_LABEL,
    Quote,
    ValuationList,
    ValuationRow,
)
from .eastmoney import EastmoneyDataSource
from .service import MarketDataService, build_default_service
from .tencent import TencentDataSource
from .ths import ThsDataSource

__all__ = [
    "ADJUST_LABEL",
    "AnomalyList",
    "AnomalyRow",
    "AuctionBenchmark",
    "AuctionRow",
    "AuctionSnapshot",
    "BenchmarkRow",
    "Capability",
    "DragonTigerBoard",
    "DragonTigerRow",
    "EastmoneyDataSource",
    "FinancialPeriod",
    "FinancialSummary",
    "HotList",
    "HotRow",
    "KlineBar",
    "KlineSeries",
    "LadderDay",
    "LimitLadder",
    "LimitPool",
    "LimitPoolRow",
    "MarketDataService",
    "MarketDataSource",
    "MoneyflowDay",
    "MoneyflowSeries",
    "PERIOD_LABEL",
    "Quote",
    "ThsDataSource",
    "TencentDataSource",
    "ValuationList",
    "ValuationRow",
    "build_default_service",
]
