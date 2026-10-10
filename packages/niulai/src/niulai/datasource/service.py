"""按能力路由的数据服务 —— 工具唯一认识的门面。

P0 先做最简单的"按顺序试，记录失败原因"。
P3 会补上优先级排序、健康度降权、结果缓存与限流。

三个取数方法走的是同一套路由逻辑：**先按 capability 筛源，再逐个试，
全失败才报错**。这样加一个源（腾讯 / akshare）不需要动这里。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from ..errors import DataSourceError
from ..logs import get_logger
from .base import (
    AnomalyList,
    AuctionBenchmark,
    AuctionSnapshot,
    Capability,
    DragonTigerBoard,
    FinancialSummary,
    HotList,
    KlineSeries,
    LimitLadder,
    LimitPool,
    MarketDataSource,
    MoneyflowSeries,
    Quote,
    ValuationList,
)
from .eastmoney import EastmoneyDataSource
from .tencent import TencentDataSource
from .ths import ThsDataSource

T = TypeVar("T")

_log = get_logger("datasource.service")


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

    async def valuation(self, symbol: str) -> ValuationList:
        """估值快照：PE / PB / PS / PCF，`symbol` 支持逗号分隔多个。"""
        return await self._route("valuation", "valuation", symbol)

    async def hotlist(
        self, *, board: str = "hot", period: str = "day", limit: int = 10
    ) -> HotList:
        """同花顺热榜：hot 热股榜 / skyrocket 飙升榜。"""
        return await self._route("hotlist", "hotlist", board=board, period=period, limit=limit)

    async def limitpool(self, *, kind: str = "up", limit: int = 20) -> LimitPool:
        """涨跌停 / 炸板股票池：up / down / break。"""
        return await self._route("limitup", "limitpool", kind=kind, limit=limit)

    async def limit_ladder(self, *, days: int = 5) -> LimitLadder:
        """连板天梯：最近 days 个交易日的连板梯队。"""
        return await self._route("limitup", "limit_ladder", days=days)

    async def dragon_tiger(
        self, *, board_type: str = "all", date: str | None = None
    ) -> DragonTigerBoard:
        """龙虎榜：all / org / hot_money。"""
        return await self._route(
            "dragontiger", "dragon_tiger", board_type=board_type, date=date
        )

    async def anomaly(self, symbol: str) -> AnomalyList:
        """当日异动原因，`symbol` 支持逗号分隔多个。"""
        return await self._route("anomaly", "anomaly", symbol)

    async def moneyflow(self, symbol: str, *, days: int = 5) -> MoneyflowSeries:
        """个股资金流：主力 / 超大单 / 大单 / 中单 / 小单，最近 days 个交易日。"""
        return await self._route("moneyflow", "moneyflow", symbol, days=days)

    async def auction(self, symbol: str) -> AuctionSnapshot:
        """集合竞价快照：竞价价 / 竞价涨幅 / 竞价量比等，`symbol` 支持逗号分隔多个。"""
        return await self._route("auction", "auction", symbol)

    async def auction_benchmark(self, *, date: str | None = None) -> AuctionBenchmark:
        """短线风向标竞价基准：参照股集合的当日竞价表现与标签。"""
        return await self._route("auction", "auction_benchmark", date=date)

    async def index_quote(self, symbols: str) -> list[Quote]:
        """指数 / 板块行情快照，`symbols` 支持逗号分隔多个（000001.SH / 886042.TI）。"""
        return await self._route("index_quote", "index_quote", symbols)

    async def index_kline(self, symbol: str, *, limit: int = 60) -> KlineSeries:
        """指数 / 板块历史 K 线（日线，单只）。"""
        return await self._route("index_kline", "index_kline", symbol, limit=limit)

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


def build_default_service(ths_api_key: str | None = None) -> MarketDataService:
    """同花顺官方 API 主源，东财 / 腾讯兜底。

    路由顺序即兜底顺序（THS 在最前）：
    - quote / kline / financial / valuation / hotlist / limitup / dragontiger /
      anomaly / auction / index_quote / index_kline：ths；
    - moneyflow：THS 的资金流是端内专用接口调不了，由东财提供；
    - quote / kline / financial 的兜底：ths 失败 → 东财 → 腾讯。

    THS 需要 API Key（env `NIULAI_THS_API_KEY` 或 config.toml `[datasource]`）；
    没配时不注册 THS、退回纯东财链路 —— 打一条 warning，不拦运行。
    """
    sources: list[MarketDataSource] = []
    if ths_api_key:
        sources.append(ThsDataSource(api_key=ths_api_key))
    else:
        _log.warning(
            "未配置同花顺 API Key（%s 或 config.toml [datasource] ths_api_key），"
            "本次只使用东财 / 腾讯兜底源",
            "NIULAI_THS_API_KEY",
        )
    sources += [EastmoneyDataSource(), TencentDataSource()]
    return MarketDataService(sources)
