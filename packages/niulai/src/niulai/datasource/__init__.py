"""数据源层。

对外的名字：`MarketDataService`（工具只用它）和三种返回类型
（`Quote` / `KlineSeries` / `FinancialSummary`）。
具体数据源（东财 / 腾讯 / akshare）都藏在里面。
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
    Quote,
)
from .eastmoney import EastmoneyDataSource
from .service import MarketDataService, build_default_service

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
    "Quote",
    "build_default_service",
]
