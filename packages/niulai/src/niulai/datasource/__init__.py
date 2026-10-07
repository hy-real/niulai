"""数据源层。

对外的名字：`MarketDataService`（工具只用它）和各返回类型
（`Quote` / `KlineSeries` / `FinancialSummary` / `SectorBoard` / `MoneyflowSeries`）。
具体数据源（东财 / 腾讯 / 新浪）都藏在里面。
"""

from .base import (
    ADJUST_LABEL,
    PERIOD_LABEL,
    Capability,
    FinancialPeriod,
    FinancialSummary,
    KlineBar,
    KlineSeries,
    MarketDataSource,
    MoneyflowDay,
    MoneyflowSeries,
    Quote,
    SectorBoard,
    SectorRow,
    SectorStockList,
    SectorStockRow,
)
from .eastmoney import EastmoneyDataSource
from .service import MarketDataService, build_default_service
from .sina import SinaDataSource
from .tencent import TencentDataSource

__all__ = [
    "ADJUST_LABEL",
    "PERIOD_LABEL",
    "Capability",
    "EastmoneyDataSource",
    "FinancialPeriod",
    "FinancialSummary",
    "KlineBar",
    "KlineSeries",
    "MarketDataService",
    "MarketDataSource",
    "MoneyflowDay",
    "MoneyflowSeries",
    "Quote",
    "SectorBoard",
    "SectorRow",
    "SectorStockList",
    "SectorStockRow",
    "SinaDataSource",
    "TencentDataSource",
    "build_default_service",
]
