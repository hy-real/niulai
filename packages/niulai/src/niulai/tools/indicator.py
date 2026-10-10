"""技术指标工具。

取一段 K 线，交给 `indicators.py` 做确定性计算 —— 本工具自己不算任何指标，
它只负责"取数 + 传话"。这样指标算法可以脱离网络单独测试。
"""

from __future__ import annotations

from typing import Any

from ..datasource import MarketDataService
from ..indicators import render_indicators
from ..llm.types import ToolSpec

INDICATOR_TOOL_NAME = "stock_indicator"

# 与其余行情工具同一套能力声明：纯读 + 出网。
_MARKET_CAPABILITIES = frozenset({"read", "network"})

_SYMBOL_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "标的标识。可以传 6 位代码（如 600519），也可以直接传名称"
        "（如 贵州茅台、茅台）。不确定代码时传名称即可，服务端会做解析。"
    ),
}

_INDICATOR_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "symbol": _SYMBOL_PROPERTY,
        "period": {
            "type": "string",
            "enum": ["daily", "weekly", "monthly"],
            "description": (
                "指标基于的 K 线周期：daily 日线（默认）、weekly 周线、monthly 月线。"
                "问中长期趋势可传 weekly。"
            ),
        },
        "adjust": {
            "type": "string",
            "enum": ["forward", "none", "backward"],
            "description": "复权方式，默认 forward 前复权（指标计算的标准口径）。",
        },
        "limit": {
            "type": "integer",
            "description": (
                "取多少根 K 线参与计算，默认 150，最大 250。"
                "默认值已够 MA60 与 MACD 热身，一般不用传。"
            ),
        },
    },
    "required": ["symbol"],
    "additionalProperties": False,
}


class StockIndicatorTool:
    """计算单只 A 股的技术指标（均线 / MACD / KDJ / RSI / 布林 / 量能）。"""

    capabilities = _MARKET_CAPABILITIES

    def __init__(self, service: MarketDataService) -> None:
        self._service = service
        self.spec = ToolSpec(
            name=INDICATOR_TOOL_NAME,
            description=(
                "计算单只 A 股的**技术指标**：均线 MA5/10/20/60 及多空排列、"
                "MACD(12,26,9) 与最近金叉/死叉、KDJ(9,3,3) 与最近金叉/死叉、"
                "RSI6/12、布林带(20,2)、量能（今日量比、放量缩量、量价配合）、"
                "区间位置（近 20 根涨跌幅、距 60 根高低点、连涨连跌）。\n"
                "适用：「MACD 金叉了吗」「KDJ 超买了吗」「均线多头排列吗」"
                "「最近放量了吗」「技术面怎么样」—— 技术面分析的原料都来自这里。\n"
                "不适用：实时价格（用 stock_quote）、财务与估值"
                "（stock_financial / stock_valuation）、资金流（stock_moneyflow）、"
                "异动原因（stock_anomaly）。\n"
                "返回的是指标数值与交叉信号（事实），不是买卖建议；"
                "解读时应结合行情、资金、估值一起给，并给出反方证据。"
            ),
            parameters=_INDICATOR_PARAMETERS,
        )

    async def run(self, args: dict[str, Any]) -> str:
        symbol = str(args.get("symbol") or "").strip()
        period = str(args.get("period") or "daily").strip().lower()
        adjust = str(args.get("adjust") or "forward").strip().lower()
        limit = _as_int(args.get("limit"), default=150, low=30, high=250)

        series = await self._service.kline(symbol, period=period, adjust=adjust, limit=limit)
        return render_indicators(series)


def _as_int(value: Any, *, default: int, low: int, high: int) -> int:
    """模型给的整数参数经常是字符串，夹取到合法区间而不是报错 —— 这是可恢复的。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


__all__ = ["INDICATOR_TOOL_NAME", "StockIndicatorTool"]
