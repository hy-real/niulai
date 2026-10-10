"""市场级工具：热榜 / 涨跌停池 / 龙虎榜 / 个股资金流。

与 `stock.py` 的分工：那边是「单标的」工具（输入一只股票），这边是
「看全市场」的工具 —— 帮用户在没有明确标的时先看到市场在关注什么、
情绪在哪边，再回头用 stock_quote / stock_kline 深入。
description 的写法与 stock.py 同一条实测经验：把「适用 / 不适用 / 参数怎么传」
写清楚，模型才能一次选对。
"""

from __future__ import annotations

from typing import Any

from ..datasource import MarketDataService
from ..llm.types import ToolSpec
from .stock import _MARKET_CAPABILITIES, _SYMBOL_PROPERTY, _as_int

HOT_TOOL_NAME = "market_hot"
LIMITUP_TOOL_NAME = "market_limitup"
DRAGON_TIGER_TOOL_NAME = "market_dragontiger"
MONEYFLOW_TOOL_NAME = "stock_moneyflow"
AUCTION_TOOL_NAME = "stock_auction"
BENCHMARK_TOOL_NAME = "market_auction_benchmark"
INDEX_TOOL_NAME = "market_index"

_HOT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "board": {
            "type": "string",
            "enum": ["hot", "skyrocket"],
            "description": "榜单：hot 热股榜（当前最受关注，默认）、skyrocket 飙升榜（排名快速跃升）。",
        },
        "period": {
            "type": "string",
            "enum": ["day", "hour"],
            "description": "周期：day 24小时 / 日级（默认）、hour 小时级（盘中看即时热度）。",
        },
        "limit": {
            "type": "integer",
            "description": "返回前多少只，默认 10，最大 30。看头部用 10，全景用 20–30。",
        },
    },
    "additionalProperties": False,
}

_LIMITUP_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["up", "down", "break", "ladder"],
            "description": (
                "池子：up 涨停池（默认，含连板天数与涨停原因）、down 跌停池、"
                "break 炸板池（涨停后开板）、ladder 连板天梯（近 30 日连板梯队矩阵）。"
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                "up / down / break 返回前多少只（默认 20，最大 50）；"
                "ladder 返回最近几个交易日（默认 5，最大 30）。"
            ),
        },
    },
    "additionalProperties": False,
}

_DRAGON_TIGER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "board_type": {
            "type": "string",
            "enum": ["all", "org", "hot_money"],
            "description": "榜单：all 全部（默认）、org 机构榜、hot_money 游资榜（返回游资聚合视角）。",
        },
        "date": {
            "type": "string",
            "description": (
                "目标交易日 yyyy-MM-dd。省略时自动取最近一个交易日；"
                "显式传入必须是真实交易日，传节假日会报错。只支持一年内。"
            ),
        },
    },
    "additionalProperties": False,
}

_MONEYFLOW_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": _SYMBOL_PROPERTY,
        "days": {
            "type": "integer",
            "description": (
                "最近多少个交易日，默认 5，最大 20。"
                "看今天用 1；判断主力是持续流入还是一日游用 5–10。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


_AUCTION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "标的标识，支持逗号分隔多个（如 600519,000858 或 茅台,五粮液）"
                "做竞价强弱对比。可传 6 位代码或名称，服务端会做解析。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}

_BENCHMARK_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": (
                "目标交易日 yyyy-MM-dd。省略取当日；显式传入必须是真实交易日。"
            ),
        },
    },
    "additionalProperties": False,
}

_INDEX_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["quote", "kline"],
            "description": "quote 最新行情快照（默认，支持多个）；kline 历史日 K（单只）。",
        },
        "symbol": {
            "type": "string",
            "description": (
                "指数标识。常用指数可用名称（上证指数 / 大盘 / 深证成指 / 创业板指 / "
                "科创50 / 沪深300 / 中证500 / 中证1000 / 北证50），也可传完整代码"
                "（000001.SH、886042.TI）。同花顺板块 / 行业指数传 88 开头代码（如 886042.TI）。"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "kline 取最近多少根日 K，默认 60，最大 500。quote 时忽略。",
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


class MarketHotTool:
    """查询同花顺热股榜 / 飙升榜。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=HOT_TOOL_NAME,
            description=(
                "查询**同花顺热榜**：全市场关注度最高的股票（热股榜）或排名快速跃升的"
                "股票（飙升榜），来自同花顺数千万用户的真实浏览行为。\n"
                "适用：「现在哪些股票最火」「大家都在关注什么」「今天热度飙升的票」"
                "这类帮用户**发现标的**的问题。\n"
                "不适用：某只股票的行情 / 资金 / 财务（用 stock_quote / stock_moneyflow / "
                "stock_financial）、涨跌停与连板情绪（用 market_limitup）。\n"
                "热度 ≠ 涨幅：上榜的票可能在涨也可能在跌，热度代表关注度。"
                "返回的代码可直接用于后续追问。"
            ),
            parameters=_HOT_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        board = str(args.get("board") or "hot").strip().lower()
        period = str(args.get("period") or "day").strip().lower()
        limit = _as_int(args.get("limit"), default=10, low=1, high=30)

        board_list = await self._service.hotlist(board=board, period=period, limit=limit)
        return board_list.render()


class MarketLimitPoolTool:
    """查询涨停 / 跌停 / 炸板股票池与连板天梯。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=LIMITUP_TOOL_NAME,
            description=(
                "查询当日**涨跌停与炸板股票池**，以及近 30 日的**连板天梯**。\n"
                "适用：「今天多少家涨停」「连板股有哪些」「涨停的原因是什么」「炸板多不多」"
                "「最近的连板梯队怎么分布」—— 盘面情绪与短线题材判断。\n"
                "不适用：某只股票的资金流（用 stock_moneyflow）、关注度榜单（用 market_hot）、"
                "龙虎榜席位（用 market_dragontiger）。\n"
                "涨停池带涨停时间、连板天数、涨停原因与封单额，是复盘情绪面的主要数据源。"
            ),
            parameters=_LIMITUP_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "up").strip().lower()
        limit = _as_int(args.get("limit"), default=20, low=1, high=50)

        if kind == "ladder":
            return (await self._service.limit_ladder(days=limit)).render()
        return (await self._service.limitpool(kind=kind, limit=limit)).render()


class MarketDragonTigerTool:
    """查询龙虎榜（全部 / 机构榜 / 游资榜）。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=DRAGON_TIGER_TOOL_NAME,
            description=(
                "查询**龙虎榜**：登上交易所公开席位的股票，按全部 / 机构 / 游资三种视角。\n"
                "适用：「昨天龙虎榜上有什么」「机构买了哪些票」「游资在炒什么」"
                "—— 看大资金动向。\n"
                "不适用：未上龙虎榜的普通股票资金流（用 stock_moneyflow）、"
                "涨停原因（用 market_limitup 的涨停池更全）。\n"
                "上榜有门槛（涨跌幅 / 换手 / 振幅达标），所以这是**异常波动**股票的"
                "席位数据，不是全市场资金分布。"
            ),
            parameters=_DRAGON_TIGER_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        board_type = str(args.get("board_type") or "all").strip().lower()
        date = str(args.get("date") or "").strip() or None

        board = await self._service.dragon_tiger(board_type=board_type, date=date)
        return board.render()


class StockAuctionTool:
    """查询单只或多只 A 股的集合竞价快照。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=AUCTION_TOOL_NAME,
            description=(
                "查询 A 股的**集合竞价快照**：竞价价格、竞价涨跌幅、竞价量比、"
                "竞价量相对昨日成交量的比例、竞价成交额、未匹配量、开盘价。\n"
                "适用：「今天开盘竞价怎么样」「XX 竞价高开多少」「竞价放量了吗」"
                "「这几只票谁的竞价更强」—— 开盘前最强的短线技术信号。\n"
                "不适用：盘中 / 收盘价格（用 stock_quote）、历史走势（用 stock_kline）、"
                "全市场情绪风向（用 market_auction_benchmark）。\n"
                "竞价只在交易日 9:15–9:25 产生；盘后查到的是当日终态。"
                "高开 + 竞价放量是偏强信号，低开缩量偏弱。"
            ),
            parameters=_AUCTION_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        snapshot = await self._service.auction(symbol)
        return snapshot.render()


class MarketAuctionBenchmarkTool:
    """查询短线风向标集合竞价基准。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=BENCHMARK_TOOL_NAME,
            description=(
                "查询**短线风向标的集合竞价基准**：市场短线情绪参照股集合"
                "（多为前期涨停 / 人气股）当日的竞价涨跌幅与竞价标签（高开 / 放量等）。\n"
                "适用：「今天情绪怎么样」「开盘风向如何」「接力氛围好不好」"
                "—— 判断当日短线情绪强弱的首选数据。\n"
                "不适用：特定个股的竞价细节（用 stock_auction）、"
                "涨停池与连板梯队（用 market_limitup）。\n"
                "风向标集体高开放量 = 情绪偏强，集体低开 = 情绪退潮；"
                "这是市场整体信号，不代表任何单只股票。"
            ),
            parameters=_BENCHMARK_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        date = str(args.get("date") or "").strip() or None
        benchmark = await self._service.auction_benchmark(date=date)
        return benchmark.render()


class MarketIndexTool:
    """查询指数 / 板块的行情快照与历史 K 线。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=INDEX_TOOL_NAME,
            description=(
                "查询**指数与同花顺板块**的行情：最新快照（上证指数 / 创业板指等大盘指数，"
                "以及 88 开头的同花顺行业 / 概念板块指数）或历史日 K。\n"
                "适用：「大盘今天怎么样」「半导体板块最近走势」「这只票跑赢它的板块了吗」"
                "—— 个股技术分析需要的大盘与板块参照系。\n"
                "不适用：个股行情（用 stock_quote）、个股 K 线（用 stock_kline）、"
                "个股所属哪个板块（本工具不提供反查）。\n"
                "板块指数用同花顺代码（886042.TI 这类）；常用大盘指数可直接传名称。"
                "个股与板块强弱对比：先取板块 kline，再取个股 stock_kline，对比区间涨跌幅。"
            ),
            parameters=_INDEX_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "quote").strip().lower()
        symbol = str(args.get("symbol") or "").strip()

        if kind == "kline":
            limit = _as_int(args.get("limit"), default=60, low=1, high=500)
            return (await self._service.index_kline(symbol, limit=limit)).render()

        quotes = await self._service.index_quote(symbol)
        return "\n\n".join(q.render() for q in quotes)


def build_market_tools(service: MarketDataService) -> list[Any]:
    """完整市场工具集。与 build_stock_tools 同一条规则：工具有没有，由数据源的能力决定。"""
    tools: list[Any] = []
    if service.sources_for("hotlist"):
        tools.append(MarketHotTool(service))
    if service.sources_for("limitup"):
        tools.append(MarketLimitPoolTool(service))
    if service.sources_for("dragontiger"):
        tools.append(MarketDragonTigerTool(service))
    if service.sources_for("moneyflow"):
        tools.append(StockMoneyflowTool(service))
    if service.sources_for("auction"):
        tools.append(StockAuctionTool(service))
        tools.append(MarketAuctionBenchmarkTool(service))
    if service.sources_for("index_quote"):
        tools.append(MarketIndexTool(service))
    return tools


class StockMoneyflowTool:
    """查询单只 A 股的资金流向。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=MONEYFLOW_TOOL_NAME,
            description=(
                "查询单只 A 股的**每日资金流向**：主力（超大单+大单）、中单、小单的"
                "净流入，以及当日收盘与涨跌幅，最近 days 个交易日。\n"
                "适用：「主力在买还是在卖」「最近资金是流入还是流出」「今天有多少"
                "主力资金进来」。\n"
                "不适用：行情价格（用 stock_quote）、市场情绪与涨停（用 market_limitup）、"
                "龙虎榜席位（用 market_dragontiger）、实时逐笔大单明细（本工具是按天的汇总）。\n"
                "正值为净流入，负值为净流出；判断持续性要看多天趋势，别只看一天。"
            ),
            parameters=_MONEYFLOW_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        days = _as_int(args.get("days"), default=5, low=1, high=20)

        series = await self._service.moneyflow(symbol, days=days)
        return series.render()


__all__ = [
    "AUCTION_TOOL_NAME",
    "BENCHMARK_TOOL_NAME",
    "DRAGON_TIGER_TOOL_NAME",
    "HOT_TOOL_NAME",
    "INDEX_TOOL_NAME",
    "LIMITUP_TOOL_NAME",
    "MONEYFLOW_TOOL_NAME",
    "MarketAuctionBenchmarkTool",
    "MarketDragonTigerTool",
    "MarketHotTool",
    "MarketIndexTool",
    "MarketLimitPoolTool",
    "StockAuctionTool",
    "StockMoneyflowTool",
    "build_market_tools",
]
