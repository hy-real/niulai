"""按能力路由的数据服务 —— 工具唯一认识的门面。

P0 先做最简单的"按顺序试，记录失败原因"。
P3 会补上优先级排序、健康度降权、结果缓存与限流。

三个取数方法走的是同一套路由逻辑：**先按 capability 筛源，再逐个试，
全失败才报错**。这样加一个源（腾讯 / 新浪 / akshare）不需要动这里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from ..errors import DataSourceError
from .base import (
    Capability,
    FinancialSummary,
    KlineSeries,
    MarketDataSource,
    Quote,
)
from .eastmoney import EastmoneyDataSource

T = TypeVar("T")


class MarketDataService:
    """按 capability 挑源，失败自动换下一个。"""

    def __init__(self, sources: Sequence[MarketDataSource]) -> None:
        if not sources:
            raise ValueError("至少要提供一个数据源")
        self._sources = list(sources)

    @property
    def source_ids(self) -> list[str]:
        return [s.id for s in self._sources]

    def sources_for(self, capability: Capability) -> list[MarketDataSource]:
        return [s for s in self._sources if capability in s.capabilities]

    async def quote(self, symbol: str) -> Quote:
        """实时行情快照：最新价 / 涨跌 / 今开 / 最高 / 最低 / 成交量额。

        `symbol` 可以是 '600519' / 'sh600519' / 「茅台」这类名称（由源内部解析）。
        """
        return await self._route("quote", "quote", symbol)

    async def kline(
        self,
        symbol: str,
        *,
        period: str = "daily",
        adjust: str = "forward",
        limit: int = 60,
    ) -> KlineSeries:
        """历史 K 线：按 period（daily/weekly/monthly）与 adjust（复权方式）取最近 limit 根。"""
        return await self._route(
            "kline", "kline", symbol, period=period, adjust=adjust, limit=limit
        )

    async def financial(self, symbol: str, *, periods: int = 4) -> FinancialSummary:
        """定期报告主要财务指标：营收 / 归母净利 / ROE / 毛利率等，最近 periods 期。"""
        return await self._route("financial", "financial", symbol, periods=periods)

    async def aclose(self) -> None:
        for source in self._sources:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    # ------------------------------------------------------------------ 内部

    async def _route(
        self,
        capability: Capability,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> T:
        candidates = self.sources_for(capability)
        if not candidates:
            raise DataSourceError(f"没有任何数据源支持 {capability} 能力")

        failures: list[str] = []
        for source in candidates:
            fn: Callable[..., Awaitable[T]] | None = getattr(source, method_name, None)
            if fn is None:  # 声明了能力却没实现方法：跳过，别让它把整条链带崩
                failures.append(f"{source.id}: 未实现 {method_name}()")
                continue
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— "换下一个源"正是这一层存在的理由
                failures.append(f"{source.id}: {exc}")

        raise DataSourceError(f"所有数据源都失败了 —— {' | '.join(failures)}")


def build_default_service() -> MarketDataService:
    """P0/P3 只有东财一个源。

    P3 后半会补上腾讯 / 新浪做轻量心跳兜底，akshare 作为可选长尾 adapter。
    """
    return MarketDataService([EastmoneyDataSource()])
