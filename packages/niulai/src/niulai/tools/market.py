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
    return tools


__all__ = [
    "DRAGON_TIGER_TOOL_NAME",
    "HOT_TOOL_NAME",
    "LIMITUP_TOOL_NAME",
    "MONEYFLOW_TOOL_NAME",
    "MarketDragonTigerTool",
    "MarketHotTool",
    "MarketLimitPoolTool",
    "StockMoneyflowTool",
    "build_market_tools",
]
