"""股票工具。

三个只读工具：行情快照 / K 线 / 财务摘要。

注意 `description` 的写法 —— 实测里"只重写工具描述和参数名"就能把同一个
任务的成功率从 60% 拉到 95%。所以这里把「适用 / 不适用 / 参数怎么传 /
什么时候该用另一个工具」全部写清楚，而不是糊一句"查询股票行情"。

**没有的工具要在描述里说清楚没有**，否则模型会去猜一个不存在的工具名，
然后浪费两轮在错误里打转。
"""

from __future__ import annotations

from typing import Any

from ..datasource import MarketDataService
from ..llm.types import ToolSpec

QUOTE_TOOL_NAME = "stock_quote"
KLINE_TOOL_NAME = "stock_kline"
FINANCIAL_TOOL_NAME = "stock_financial"
VALUATION_TOOL_NAME = "stock_valuation"
ANOMALY_TOOL_NAME = "stock_anomaly"

# 工具的能力声明。这一个是纯读 + 出网，不碰本地文件、不执行命令。
_MARKET_CAPABILITIES = frozenset({"read", "network"})

_SYMBOL_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "标的标识。可以传 6 位代码（如 600519），也可以直接传名称"
        "（如 贵州茅台、茅台）。不确定代码时传名称即可，服务端会做解析。"
    ),
}

_QUOTE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"symbol": _SYMBOL_PROPERTY},
    "required": ["symbol"],
    "additionalProperties": False,
}

_KLINE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": _SYMBOL_PROPERTY,
        "period": {
            "type": "string",
            "enum": ["daily", "weekly", "monthly"],
            "description": "K 线周期：daily 日线（默认）、weekly 周线、monthly 月线。",
        },
        "limit": {
            "type": "integer",
            "description": (
                "取最近多少根 K 线，默认 60，最大 500。"
                "问「最近走势 / 近一个月」用 20–60；问「今年表现」用 120–250。"
                "取太多会更早触发大结果落盘，需要再用 read_file 分页读。"
            ),
        },
        "adjust": {
            "type": "string",
            "enum": ["forward", "none", "backward"],
            "description": (
                "复权方式，默认 forward 前复权（看走势连续性最好）。"
                "注意前复权在长期高分红股上会让很久以前的早期价格为负，"
                "那是算法结果不是数据错误；要看历史真实成交价就传 none。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}

_FINANCIAL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": _SYMBOL_PROPERTY,
        "periods": {
            "type": "integer",
            "description": (
                "返回最近几期定期报告，默认 4，最大 12。"
                "看最新一期传 1；看同比趋势传 4–8。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


class StockQuoteTool:
    """查询单只 A 股的实时行情快照。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=QUOTE_TOOL_NAME,
            description=(
                "查询单只 A 股的**实时行情快照**：最新价、涨跌幅、今开 / 最高 / 最低 / 昨收、"
                "成交量与成交额。\n"
                "适用：用户问某只股票「现在多少钱」「今天涨了还是跌了」「今天成交多少」。\n"
                "不适用：历史走势（用 stock_kline）、财务指标（用 stock_financial）、"
                "板块或指数行情、逐笔明细 —— 这些本工具不提供。\n"
                "返回的是快照，时间精度到秒。"
            ),
            parameters=_QUOTE_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        quote = await self._service.quote(symbol)
        return quote.render()


class StockKlineTool:
    """查询单只 A 股的历史 K 线。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=KLINE_TOOL_NAME,
            description=(
                "查询单只 A 股的**历史 K 线**（开高低收 + 成交量），用于回答走势类问题。\n"
                "适用：涨跌幅趋势、区间高低点、均线所处位置、连续涨跌天数、"
                "「最近一个月怎么样」「今年以来表现如何」。\n"
                "不适用：当前价（用 stock_quote 更快更省）、财务指标（用 stock_financial）、"
                "技术指标（MACD/KDJ 等本工具不计算，需要你自己从 K 线推）。\n"
                "数据量可能较大：超过阈值时系统会把完整数据落盘，只给你摘要和路径，"
                "这时用 read_file 分页读，不要把摘要当全部数据。"
            ),
            parameters=_KLINE_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        period = str(args.get("period") or "daily").strip().lower()
        adjust = str(args.get("adjust") or "forward").strip().lower()
        limit = _as_int(args.get("limit"), default=60, low=1, high=500)

        series = await self._service.kline(
            symbol, period=period, adjust=adjust, limit=limit
        )
        return series.render()


class StockFinancialTool:
    """查询单只 A 股的定期报告主要财务指标。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=FINANCIAL_TOOL_NAME,
            description=(
                "查询单只 A 股的**定期报告主要财务指标**：营业总收入、归母净利润、"
                "扣非净利润、每股收益、每股净资产、加权 ROE、毛利率、净利率、资产负债率，"
                "以及营收 / 净利润的同比增速。\n"
                "适用：问某只股票「业绩怎么样」「利润多少」「毛利率多高」「ROE 多少」。\n"
                "不适用：实时行情（用 stock_quote）、股价走势（用 stock_kline）、"
                "券商研报观点、业绩预告。\n"
                "数据来自定期报告（一季报 / 中报 / 三季报 / 年报），是**累计口径**，"
                "所以一季报的数字天然小于年报。银行、保险等行业的若干字段为空，属正常。"
            ),
            parameters=_FINANCIAL_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        periods = _as_int(args.get("periods"), default=4, low=1, high=12)
        summary = await self._service.financial(symbol, periods=periods)
        return summary.render()


_VALUATION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "标的标识，支持逗号分隔多个（如 600519,000858 或 茅台,五粮液）做对比。"
                "可传 6 位代码或名称，服务端会做解析。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}

_ANOMALY_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": {
            "type": "string",
            "description": (
                "标的标识，支持逗号分隔多个（如 600519,000858）。"
                "可传 6 位代码或名称，服务端会做解析。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


class StockValuationTool:
    """查询单只或多只 A 股的估值快照。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=VALUATION_TOOL_NAME,
            description=(
                "查询 A 股的**最新估值快照**：市盈率 PE（TTM / MRQ 两种口径）、"
                "市净率 PB、市销率 PS、市现率 PCF。支持多标的一次对比。\n"
                "适用：「茅台贵不贵」「XX 和 YY 谁便宜」「现在估值多少倍」。\n"
                "不适用：实时价格（用 stock_quote）、历史走势（用 stock_kline）、"
                "业绩本身（用 stock_financial）。\n"
                "亏损公司 PE 为负或缺失属正常；不同行业的合理估值区间差异很大，"
                "对比请在同行业内做。"
            ),
            parameters=_VALUATION_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        result = await self._service.valuation(symbol)
        return result.render()


class StockAnomalyTool:
    """查询单只或多只 A 股的当日异动原因。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=ANOMALY_TOOL_NAME,
            description=(
                "查询 A 股的**当日异动原因**：股价出现大涨 / 大跌 / 快速拉升 / "
                "快速下挫等异动时，由数据方给出的解读文本与关键词。\n"
                "适用：「XX 今天为什么大涨」「这票怎么突然跳水」「XX 异动的原因」。\n"
                "不适用：没有异动的平静走势（会返回无记录）、涨停池全景"
                "（用 market_limitup）、龙虎榜席位（用 market_dragontiger）。\n"
                "仅当日有效；解读是数据方观点，供参考，不是因果结论。"
            ),
            parameters=_ANOMALY_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        result = await self._service.anomaly(symbol)
        return result.render()


def build_stock_tools(service: MarketDataService) -> list[Any]:
    """按数据源能力组装股票工具集。

    注意"有没有这个工具"是由**数据源的能力**决定的，不是写死的。
    将来加一个新源，只要它声明了 quote 能力，工具就自动可用；
    反过来，某个源如果只有行情没有财务，`stock_financial` 就不会出现 ——
    模型看不到它，也就不会去调一个注定失败的接口。
    """
    tools: list[Any] = []
    if service.sources_for("quote"):
        tools.append(StockQuoteTool(service))
    if service.sources_for("kline"):
        tools.append(StockKlineTool(service))
    if service.sources_for("financial"):
        tools.append(StockFinancialTool(service))
    if service.sources_for("valuation"):
        tools.append(StockValuationTool(service))
    if service.sources_for("anomaly"):
        tools.append(StockAnomalyTool(service))
    return tools


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    """模型给的整数参数经常是字符串，夹取到合法区间而不是报错 —— 这是可恢复的。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


__all__ = [
    "ANOMALY_TOOL_NAME",
    "FINANCIAL_TOOL_NAME",
    "KLINE_TOOL_NAME",
    "QUOTE_TOOL_NAME",
    "VALUATION_TOOL_NAME",
    "StockAnomalyTool",
    "StockFinancialTool",
    "StockKlineTool",
    "StockQuoteTool",
    "StockValuationTool",
    "build_stock_tools",
]
