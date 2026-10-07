"""市场级工具：板块榜单 / 个股资金流。

与 `stock.py` 的分工：那边是「单标的」工具（输入一只股票），这边是
「看全市场」的工具（板块榜不需要标的，资金流虽然输入标但视角是资金而非价格）。
description 的写法与 stock.py 同一条实测经验：把「适用 / 不适用 / 参数怎么传」
写清楚，模型才能一次选对。
"""

from __future__ import annotations

from typing import Any

from ..datasource import MarketDataService
from ..llm.types import ToolSpec
from .stock import _MARKET_CAPABILITIES, _SYMBOL_PROPERTY, _as_int, build_stock_tools

SECTORS_TOOL_NAME = "market_sectors"
SECTOR_STOCKS_TOOL_NAME = "sector_stocks"
MONEYFLOW_TOOL_NAME = "stock_moneyflow"

_SECTORS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["concept", "industry"],
            "description": "板块类型：concept 概念板块（默认）、industry 行业板块。",
        },
        "sort": {
            "type": "string",
            "enum": ["change_pct", "moneyflow"],
            "description": (
                "排序：change_pct 按涨跌幅（默认）、moneyflow 按主力净流入。"
                "问「哪些板块热」用 change_pct；问「资金在流进哪些板块」用 moneyflow。"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "返回前多少个板块，默认 10，最大 50。看头部用 10，看全景用 20–50。",
        },
    },
    "additionalProperties": False,
}

_SECTOR_STOCKS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "板块标识。传 market_sectors 返回的板块代码（如 gn_hwqc），"
                "或板块名称（如 华为汽车）。"
            ),
        },
        "sort": {
            "type": "string",
            "enum": ["change_pct", "amount", "turnover"],
            "description": (
                "排序：change_pct 按涨跌幅（默认）、amount 按成交额、turnover 按换手率。"
            ),
        },
        "limit": {
            "type": "integer",
            "description": "返回前多少只，默认 10，最大 50。",
        },
    },
    "required": ["symbol"],
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


class MarketSectorsTool:
    """查询概念 / 行业板块榜单。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=SECTORS_TOOL_NAME,
            description=(
                "查询 A 股**板块榜单**：概念板块 / 行业板块的涨跌幅、主力净流入与领涨股。\n"
                "适用：「今天哪些板块涨得好」「资金在流进哪些板块」「概念板块排行」"
                "这类看全市场的问题。\n"
                "不适用：某只股票的行情（用 stock_quote）、某个板块内的成分股列表、"
                "个股资金流（用 stock_moneyflow）。\n"
                "榜单按指定字段从高到低排序，返回板块代码可用于后续追问。"
            ),
            parameters=_SECTORS_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "concept").strip().lower()
        sort = str(args.get("sort") or "change_pct").strip().lower()
        limit = _as_int(args.get("limit"), default=10, low=1, high=50)

        board = await self._service.sectors(kind=kind, sort=sort, limit=limit)
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
                "不适用：行情价格（用 stock_quote）、板块整体资金流向（用 market_sectors）、"
                "实时逐笔大单明细（本工具是按天的汇总）。\n"
                "正值为净流入，负值为净流出；判断持续性要看多天趋势，别只看一天。"
            ),
            parameters=_MONEYFLOW_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        days = _as_int(args.get("days"), default=5, low=1, high=20)

        series = await self._service.moneyflow(symbol, days=days)
        return series.render()


class SectorStocksTool:
    """查询板块的成分股列表。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=SECTOR_STOCKS_TOOL_NAME,
            description=(
                "查询某个**板块的成分股列表**：板块内逐股的最新价、涨跌幅、成交额、"
                "换手率、市盈率、市净率与总市值。\n"
                "适用：「华为汽车板块里哪些股票涨得好」「XX 板块有哪些股票」"
                "这类先有板块、再看个股的问题。\n"
                "不适用：全市场板块榜（用 market_sectors，它返回的板块代码是本工具的"
                "输入）、单只股票的详细行情 / 资金流（用 stock_quote / stock_moneyflow）。\n"
                "板块代码优先用 market_sectors 查到的；传名称也行，服务端会做解析。"
            ),
            parameters=_SECTOR_STOCKS_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        sort = str(args.get("sort") or "change_pct").strip().lower()
        limit = _as_int(args.get("limit"), default=10, low=1, high=50)

        stocks = await self._service.sector_stocks(symbol, sort=sort, limit=limit)
        return stocks.render()


def build_market_tools(service: MarketDataService) -> list[Any]:
    """完整行情工具集 = 单标的工具（stock.py）+ 市场级工具（本模块）。

    与 build_stock_tools 同一条规则：工具有没有，由数据源的能力决定。
    """
    tools = [*build_stock_tools(service)]
    if service.sources_for("sector"):
        tools.append(MarketSectorsTool(service))
    if service.sources_for("sector_stocks"):
        tools.append(SectorStocksTool(service))
    if service.sources_for("moneyflow"):
        tools.append(StockMoneyflowTool(service))
    return tools


__all__ = [
    "MONEYFLOW_TOOL_NAME",
    "SECTORS_TOOL_NAME",
    "SECTOR_STOCKS_TOOL_NAME",
    "MarketSectorsTool",
    "SectorStocksTool",
    "StockMoneyflowTool",
    "build_market_tools",
]
