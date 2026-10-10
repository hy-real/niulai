"""同花顺金融数据 API 数据源（主源）。

官方文档：https://fuyao.aicubes.cn/docs/api-reference/overview/

为什么它是主源：
- 官方接口：统一信封 `{code, message, request_id, data}`，字段名有语义，
  不像东财那样靠 `f43` 这类代号猜；鉴权用 `X-api-key`，无灰色地带；
- 三个核心能力（quote / kline / financial）覆盖到位，且有官方标的检索
  （tickers/search），「茅台」→ `600519.SH` 的解析不用再猜。

已知边界（2026-10 确认）：
- K 线服务端仅支持日线（`interval=1d`，窗口 ≤ 10 年）。周线 / 月线由本层
  用日线聚合 —— 工具层的 `period` 参数语义不变；
- 行情快照不返回名称，名称统一走 tickers/search 解析；
- 资金流 / 高频数据是「端内专用」，外部 Key 调不了 —— moneyflow 能力
  仍由东财兜底，本源不声明它。

单位约定（见 base.py，别在某一层偷偷换算）：THS 的成交量是**股**，
本项目约定是**手**，换算发生在本层；金额一律元。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import re
from typing import Any

import httpx

from ..errors import DataSourceError
from ..logs import get_logger
from .base import (
    AnomalyList,
    AnomalyRow,
    AuctionBenchmark,
    AuctionRow,
    AuctionSnapshot,
    BenchmarkRow,
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
    Quote,
    ValuationList,
    ValuationRow,
)

_log = get_logger("datasource.ths")

BASE_URL = "https://fuyao.aicubes.cn"

SEARCH_URL = f"{BASE_URL}/api/meta/tickers/search"
QUOTE_URL = f"{BASE_URL}/api/a-share/prices/snapshot"
KLINE_URL = f"{BASE_URL}/api/a-share/prices/historical"
INCOME_URL = f"{BASE_URL}/api/a-share/financials/income-statements"
BALANCE_URL = f"{BASE_URL}/api/a-share/financials/balance-sheets"
VALUATION_URL = f"{BASE_URL}/api/a-share/valuations/snapshot"
HOT_URL = f"{BASE_URL}/api/a-share/special-data/hot-stock-list"
SKYROCKET_URL = f"{BASE_URL}/api/a-share/special-data/skyrocket-list"
LIMIT_POOL_URLS = {
    "up": f"{BASE_URL}/api/a-share/special-data/limit-up-pool",
    "down": f"{BASE_URL}/api/a-share/special-data/limit-down-pool",
    "break": f"{BASE_URL}/api/a-share/special-data/limit-break-pool",
}
LADDER_URL = f"{BASE_URL}/api/a-share/special-data/limit-up-ladder"
DRAGON_TIGER_URL = f"{BASE_URL}/api/a-share/special-data/dragon-tiger-list"
ANOMALY_STOCK_URL = f"{BASE_URL}/api/a-share/special-data/anomaly-analysis-stock"
AUCTION_URL = f"{BASE_URL}/api/a-share/auction/snapshot"
BENCHMARK_URL = f"{BASE_URL}/api/a-share/auction/short-term-benchmark"
INDEX_SNAPSHOT_URL = f"{BASE_URL}/api/a-share-index/prices/snapshot"
INDEX_HISTORICAL_URL = f"{BASE_URL}/api/a-share-index/prices/historical"

DEFAULT_ATTEMPTS = 3
# 4xx 里只有 429 值得重试；其余是请求本身有问题，重试没有意义
_RETRYABLE_STATUS = {408, 425, 429}
# 业务码里可重试的：限流（4001）与 5xxx 服务端错误。
# 1001-1004 参数、2001 未认证、2003 无权限、3001 标的不存在 —— 重试无意义
_RETRYABLE_CODES = {4001, 5001, 5002, 5003}

_THS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
# 指数 / 板块代码：000001.SH / 399001.SZ / 886042.TI（同花顺板块）
_INDEX_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ|TI)$")

# 指数快照不返回名称，检索接口也不覆盖指数 —— 常用指数用内置映射，
# 板块（88xxxx.TI）没有名录，名称位置显示代码本身。
_INDEX_NAMES: dict[str, str] = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000688.SH": "科创50",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "899050.BJ": "北证50",
    "399303.SZ": "国证2000",
}
_INDEX_ALIASES: dict[str, str] = {
    "上证指数": "000001.SH",
    "上证": "000001.SH",
    "大盘": "000001.SH",
    "深证成指": "399001.SZ",
    "创业板指": "399006.SZ",
    "创业板": "399006.SZ",
    "科创50": "000688.SH",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "中证1000": "000852.SH",
    "北证50": "899050.BJ",
}

# K 线服务端仅支持日线。按周期放大取数窗口（自然日），聚合在本层做：
# 日线 ~2.1 自然日/根；周线 ~10.5；月线 ~45（留了节假日的余量）
_PERIOD_DAY_FACTOR = {"daily": 2.1, "weekly": 10.5, "monthly": 45.0}
# 服务端窗口硬上限 10 年（≈3653 天），留安全边际
_MAX_WINDOW_DAYS = 3580
# 指数 K 线服务端只给近 5 年
_MAX_INDEX_WINDOW_DAYS = 1825

_TZ_CN = dt.timezone(dt.timedelta(hours=8))

# 财务报告期标签
_FIN_LABEL = {"FY": "年报", "Q1": "一季报", "Q2": "中报", "Q3": "三季报", "Q4": "年报"}


class ThsDataSource:
    """同花顺官方 API 源：行情 / K 线 / 财务。"""

    id = "ths"
    capabilities: set[str] = {
        "quote", "kline", "financial",
        "valuation", "hotlist", "limitup", "dragontiger", "anomaly",
        "auction", "index_quote", "index_kline",
    }

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        attempts: int = DEFAULT_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ThsDataSource 需要 api_key（环境变量 NIULAI_THS_API_KEY 或 config.toml [datasource]）")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._attempts = max(1, attempts)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ 行情

    async def quote(self, symbol: str) -> Quote:
        thscode, name = await self._resolve(symbol)
        payload = await self._get_json(
            f"{self._base_url}/api/a-share/prices/snapshot",
            {"thscodes": thscode},
            what="行情",
        )
        items = (payload.get("data") or {}).get("item") or []
        if not items:
            raise DataSourceError(
                f"ths 没有返回「{symbol}」的行情（代码可能不存在或已停牌）"
            )
        return parse_quote(items[0], name=name or thscode)

    # ------------------------------------------------------------------ K 线

    async def kline(
        self,
        symbol: str,
        *,
        period: str = "daily",
        adjust: str = "forward",
        limit: int = 60,
    ) -> KlineSeries:
        if period not in _PERIOD_DAY_FACTOR:
            raise DataSourceError(
                f"不支持的周期「{period}」，可选：{'、'.join(_PERIOD_DAY_FACTOR)}"
            )
        if adjust not in ("none", "forward", "backward"):
            raise DataSourceError(f"不支持的复权方式「{adjust}」，可选：none、forward、backward")

        thscode, name = await self._resolve(symbol)
        # THS 只给日线：按周期放大自然日窗口取日 K，周 / 月线在本层聚合。
        # 多取一点再截尾，保证聚合后仍有足够根数。
        days = min(int(limit * _PERIOD_DAY_FACTOR[period]) + 10, _MAX_WINDOW_DAYS)
        end_ms = _now_ms()
        start_ms = end_ms - days * 86_400_000

        payload = await self._get_json(
            f"{self._base_url}/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": str(start_ms),
                "end": str(end_ms),
                "adjust": adjust,
            },
            what="K 线",
        )
        items = (payload.get("data") or {}).get("item") or []
        if not items:
            raise DataSourceError(
                f"ths 没有返回「{symbol}」的 K 线（代码可能不存在、已退市或长期停牌）"
            )

        bars = [bar for item in items if (bar := parse_bar(item)) is not None]
        if period in ("weekly", "monthly"):
            bars = aggregate_bars(bars, period)
        bars = bars[-max(1, min(limit, 500)) :]
        _fill_change_pct(bars)

        return KlineSeries(
            symbol=thscode.split(".")[0],
            name=name or thscode,
            period=period,
            adjust=adjust,
            bars=bars,
            source="ths",
            as_of=_now(),
        )

    # ------------------------------------------------------------------ 财务

    async def financial(self, symbol: str, *, periods: int = 4) -> FinancialSummary:
        periods = max(1, min(periods, 12))
        thscode, name = await self._resolve(symbol)
        # 多取 4 期：同比要拿上一年同期做分母
        fetch_n = min(20, periods + 4)

        income = await self._get_json(
            f"{self._base_url}/api/a-share/financials/income-statements",
            {"thscode": thscode, "period": "quarterly", "limit": str(fetch_n)},
            what="利润表",
        )
        balance = await self._get_json(
            f"{self._base_url}/api/a-share/financials/balance-sheets",
            {"thscode": thscode, "period": "quarterly", "limit": str(fetch_n)},
            what="资产负债表",
        )
        rows = (income.get("data") or {}).get("item") or []
        balance_rows = (balance.get("data") or {}).get("item") or []
        if not rows:
            raise DataSourceError(
                f"ths 没有返回「{symbol}」的财务数据（指数、基金、部分新股没有定期报告）"
            )

        return parse_financial(
            rows,
            balance_rows=balance_rows,
            periods=periods,
            name=name or thscode,
            fallback_symbol=thscode.split(".")[0],
        )

    # ------------------------------------------------------- 市场视野（全局）

    async def valuation(self, symbol: str) -> ValuationList:
        """估值快照，`symbol` 支持逗号分隔多个（服务端上限 100，这里限 50）。"""
        thscodes = await self._resolve_many(symbol, max_tokens=50)
        payload = await self._get_json(
            f"{self._base_url}/api/a-share/valuations/snapshot",
            {"thscodes": ",".join(thscodes)},
            what="估值",
        )
        items = (payload.get("data") or {}).get("item") or []
        return ValuationList(
            rows=[parse_valuation(it) for it in items],
            source="ths",
            as_of=_now(),
        )

    async def hotlist(
        self, *, board: str = "hot", period: str = "day", limit: int = 10
    ) -> HotList:
        """同花顺热榜：热股榜（关注度）或飙升榜（排名跃升）。Top30。"""
        if board not in ("hot", "skyrocket"):
            raise DataSourceError(f"不支持的榜单「{board}」，可选：hot、skyrocket")
        if period not in ("day", "hour"):
            raise DataSourceError(f"不支持的周期「{period}」，可选：day、hour")
        url = SKYROCKET_URL if board == "skyrocket" else HOT_URL
        payload = await self._get_json(url, {"period": period}, what="热榜")
        items = (payload.get("data") or {}).get("item") or []
        rows = [
            HotRow(
                code=str(it.get("ticker") or ""),
                name=str(it.get("name") or ""),
                rank=int(it["rank"]) if it.get("rank") is not None else 0,
                heat=str(it.get("heat") or ""),
                rank_change=_int_or_none(it.get("rank_change")),
                trend=str(it.get("rank_trend") or ""),
            )
            for it in items
        ]
        return HotList(
            board=board, period=period, rows=rows[: max(1, min(limit, 30))],
            source="ths", as_of=_now(),
        )

    async def limitpool(self, *, kind: str = "up", limit: int = 20) -> LimitPool:
        """涨跌停 / 炸板股票池（当日）。"""
        if kind not in LIMIT_POOL_URLS:
            raise DataSourceError(f"不支持的池「{kind}」，可选：{'、'.join(LIMIT_POOL_URLS)}")
        payload = await self._get_json(
            LIMIT_POOL_URLS[kind],
            {"page": "1", "size": str(max(1, min(limit, 200)))},
            what="涨跌停池",
        )
        data = payload.get("data") or {}
        pagination = data.get("pagination") or {}
        rows = [parse_limit_row(it) for it in data.get("item") or []]
        return LimitPool(
            kind=kind,
            total=pagination.get("total"),
            rows=rows,
            source="ths",
            as_of=_now(),
        )

    async def limit_ladder(self, *, days: int = 5) -> LimitLadder:
        """连板天梯：近 30 个交易日的连板梯队矩阵（无入参，这里只截取最近几天）。"""
        payload = await self._get_json(LADDER_URL, {}, what="连板天梯")
        items = (payload.get("data") or {}).get("item") or []
        out: list[LadderDay] = []
        for it in items[: max(1, min(days, 30))]:
            boards: dict[str, list[str]] = {}
            for key, members in (it.get("boards") or {}).items():
                boards[key] = [
                    f"{m.get('name') or m.get('ticker')}({m.get('ticker')})" for m in members or []
                ]
            out.append(LadderDay(date=str(it.get("date") or ""), boards=boards))
        return LimitLadder(days=out, source="ths", as_of=_now())

    async def dragon_tiger(
        self, *, board_type: str = "all", date: str | None = None
    ) -> DragonTigerBoard:
        """龙虎榜。`date` 格式 yyyy-MM-dd；省略时服务端自动取最近交易日。"""
        if board_type not in ("all", "org", "hot_money"):
            raise DataSourceError(f"不支持的榜单「{board_type}」，可选：all、org、hot_money")
        params = {"board_type": board_type}
        if date:
            params["date"] = date
        payload = await self._get_json(DRAGON_TIGER_URL, params, what="龙虎榜")
        data = payload.get("data") or {}
        rows = [parse_dragon_row(it) for it in data.get("stock_items") or []]
        hot_money = [
            (str(m.get("name") or ""), _num(m.get("buying")) or 0.0)
            for m in data.get("hot_money_items") or []
        ]
        return DragonTigerBoard(
            board_type=str(data.get("board_type") or board_type),
            trade_date=str(data.get("trade_date") or ""),
            stock_count=data.get("stock_count"),
            rows=rows,
            hot_money=hot_money,
            source="ths",
            as_of=_now(),
        )

    async def anomaly(self, symbol: str) -> AnomalyList:
        """当日异动原因，`symbol` 支持逗号分隔多个（服务端上限 50）。"""
        thscodes = await self._resolve_many(symbol, max_tokens=50)
        payload = await self._get_json(
            f"{self._base_url}/api/a-share/special-data/anomaly-analysis-stock",
            {"thscodes": ",".join(thscodes)},
            what="异动原因",
        )
        items = (payload.get("data") or {}).get("item") or []
        return AnomalyList(
            rows=[parse_anomaly(it) for it in items],
            source="ths",
            as_of=_now(),
        )

    # ------------------------------------------------------- 集合竞价 / 指数

    async def auction(self, symbol: str) -> AuctionSnapshot:
        """集合竞价快照，`symbol` 支持逗号分隔多个（服务端上限 100，这里限 50）。"""
        thscodes = await self._resolve_many(symbol, max_tokens=50)
        payload = await self._get_json(
            AUCTION_URL,
            {"thscodes": ",".join(thscodes)},
            what="集合竞价",
        )
        data = payload.get("data") or {}
        items = data.get("item") or []
        if not items:
            raise DataSourceError(
                "ths 没有返回集合竞价数据（非交易日或标的代码不存在）"
            )
        return AuctionSnapshot(
            phase=str(data.get("auction_phase") or ""),
            rows=[parse_auction_row(it) for it in items],
            source="ths",
            as_of=_now(),
        )

    async def auction_benchmark(self, *, date: str | None = None) -> AuctionBenchmark:
        """短线风向标竞价基准。`date` 格式 yyyy-MM-dd，省略取当日。"""
        params: dict[str, str] = {}
        if date:
            params["date"] = date
        payload = await self._get_json(BENCHMARK_URL, params, what="短线风向标")
        data = payload.get("data") or {}
        return AuctionBenchmark(
            date=str(data.get("date") or ""),
            rows=[parse_benchmark_row(it) for it in data.get("item") or []],
            source="ths",
            as_of=_now(),
        )

    async def index_quote(self, symbols: str) -> list[Quote]:
        """指数 / 板块行情快照，`symbols` 支持逗号分隔多个（如 000001.SH,886042.TI）。"""
        thscodes = [self._to_index_code(token) for token in _split_symbols(symbols)]
        payload = await self._get_json(
            INDEX_SNAPSHOT_URL,
            {"thscodes": ",".join(thscodes)},
            what="指数行情",
        )
        items = (payload.get("data") or {}).get("item") or []
        if not items:
            raise DataSourceError("ths 没有返回指数行情（代码可能不存在）")
        return [parse_index_quote(it) for it in items]

    async def index_kline(self, symbol: str, *, limit: int = 60) -> KlineSeries:
        """指数 / 板块历史 K 线。服务端仅日线、近 5 年，无复权语义。"""
        thscode = self._to_index_code(symbol)
        limit = max(1, min(limit, 500))
        days = min(int(limit * _PERIOD_DAY_FACTOR["daily"]) + 10, _MAX_INDEX_WINDOW_DAYS)
        end_ms = _now_ms()
        start_ms = end_ms - days * 86_400_000

        payload = await self._get_json(
            INDEX_HISTORICAL_URL,
            {
                "thscode": thscode,
                "interval": "1d",
                "start": str(start_ms),
                "end": str(end_ms),
            },
            what="指数 K 线",
        )
        items = (payload.get("data") or {}).get("item") or []
        if not items:
            raise DataSourceError(f"ths 没有返回「{thscode}」的指数 K 线")
        bars = [bar for item in items if (bar := parse_bar(item)) is not None]
        bars = bars[-limit:]
        _fill_change_pct(bars)
        return KlineSeries(
            symbol=thscode,
            name=_INDEX_NAMES.get(thscode, thscode),
            period="daily",
            adjust="none",
            bars=bars,
            source="ths",
            as_of=_now(),
        )

    def _to_index_code(self, symbol: str) -> str:
        """「上证指数」/ '000001.SH' / '886042.TI' → 标准 thscode。"""
        raw = symbol.strip().upper()
        alias = _INDEX_ALIASES.get(raw) or _INDEX_ALIASES.get(symbol.strip())
        if alias:
            return alias
        if _INDEX_CODE_RE.match(raw):
            return raw
        raise DataSourceError(
            f"「{symbol}」不是可识别的指数代码。常用指数可用名称（上证指数 / 创业板指 /"
            " 沪深300 等），板块请传同花顺板块代码（如 886042.TI）。"
        )

    # ------------------------------------------------------------------ 内部

    async def _resolve_many(self, symbol: str, *, max_tokens: int) -> list[str]:
        """逗号分隔的标的列表 → 去重的 thscode 列表（保持出现顺序）。"""
        tokens = [t.strip() for t in symbol.replace("，", ",").split(",") if t.strip()]
        if not tokens:
            raise DataSourceError("股票代码或名称为空")
        if len(tokens) > max_tokens:
            raise DataSourceError(f"一次最多 {max_tokens} 个标的，收到 {len(tokens)} 个")
        out: list[str] = []
        for token in tokens:
            code = _try_to_thscode(token)
            if code is None:
                code, _ = await self._search(token)
            if code not in out:
                out.append(code)
        return out

    async def _get_json(self, url: str, params: dict[str, str], *, what: str) -> dict[str, Any]:
        """带重试的 GET。

        与东财同一条原则：**可重试的才重试**。传输错误 / 429 / 5xx /
        信封里的 4001、5xxx 重试；参数错、Key 错、权限错立即失败。
        """
        last_error: str = "未知错误"

        for attempt in range(self._attempts):
            _log.debug("GET %s (%s，第 %d 次)", url, what, attempt + 1)
            try:
                resp = await self._client.get(
                    url, params=params, headers={"X-api-key": self._api_key}
                )
                if resp.status_code in _RETRYABLE_STATUS or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                else:
                    resp.raise_for_status()
                    payload = resp.json()
                    envelope_error = _envelope_error(payload, what=what)
                    if envelope_error is None:
                        return payload
                    retryable, message = envelope_error
                    if not retryable:
                        raise DataSourceError(message)
                    last_error = message
            except httpx.HTTPStatusError as exc:
                raise DataSourceError(
                    f"ths {what}请求被拒绝（HTTP {exc.response.status_code}）："
                    "请检查参数是否正确；若持续出现，可能是被限流了。"
                ) from exc
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                last_error = f"响应不是 JSON（{exc}）"

            if attempt < self._attempts - 1:
                delay = 0.4 * 2**attempt + random.uniform(0, 0.3)
                _log.warning(
                    "ths %s 第 %d/%d 次失败（%s），%.1f 秒后重试",
                    what, attempt + 1, self._attempts, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise DataSourceError(
            f"ths {what}请求失败（已重试 {self._attempts} 次）：{last_error}。"
            "多半是限流，稍等一会儿再试。"
        )

    async def _resolve(self, symbol: str) -> tuple[str, str]:
        """把 '600519' / 'sh600519' / '600519.SH' / 「茅台」统一成 `(thscode, name)`。

        快照不返回名称，所以纯代码输入也要补一次检索换名字 —— 每次解析
        恰好一次 search 请求，行为可预测（缓存层将来接在这里）。
        """
        raw = symbol.strip()
        if not raw:
            raise DataSourceError("股票代码或名称为空")

        code = _try_to_thscode(raw)
        return await self._search(code if code else raw)

    async def _search(self, keyword: str) -> tuple[str, str]:
        """按代码或名称检索标的，返回 `(thscode, name)`。"""
        payload = await self._get_json(
            f"{self._base_url}/api/meta/tickers/search",
            {"q": keyword, "asset_type": "a-share", "limit": "5"},
            what="标的检索",
        )
        items = (payload.get("data") or {}).get("item") or []
        for item in items:
            if item.get("asset_type") == "a-share" and item.get("thscode"):
                return str(item["thscode"]), str(item.get("name") or "")
        raise DataSourceError(f"找不到与「{keyword}」匹配的 A 股标的，请改用 6 位股票代码")


# ---------------------------------------------------------------------- 解析
# 做成模块级函数而不是方法，是为了能脱离网络单独测试。


def parse_quote(item: dict[str, Any], *, name: str, fallback_symbol: str = "") -> Quote:
    """快照单条记录 → `Quote`。成交量股 → 手，金额保持元。"""
    return Quote(
        symbol=str(item.get("ticker") or fallback_symbol),
        name=name,
        price=_num(item.get("last_price")) or 0.0,
        prev_close=_num(item.get("prev_price")),
        open=_num(item.get("open_price")),
        high=_num(item.get("high_price")),
        low=_num(item.get("low_price")),
        change=_num(item.get("price_change")),
        change_pct=_num(item.get("price_change_ratio_pct")),
        volume=_shares_to_lots(item.get("volume")),
        amount=_num(item.get("turnover")),
        source="ths",
        as_of=_now(),
    )


def parse_valuation(item: dict[str, Any]) -> ValuationRow:
    """估值快照单条 → `ValuationRow`。code 用 6 位 ticker，与 Quote 一致。"""
    return ValuationRow(
        code=str(item.get("ticker") or item.get("thscode") or ""),
        name=str(item.get("name") or ""),
        pe_ttm=_num(item.get("pe_ttm")),
        pe_mrq=_num(item.get("pe_mrq")),
        pb_mrq=_num(item.get("pb_mrq")),
        ps_ttm=_num(item.get("ps_ttm")),
        pcf_ttm=_num(item.get("pcf_ttm")),
    )


def parse_auction_row(item: dict[str, Any]) -> AuctionRow:
    """集合竞价单条 → `AuctionRow`。成交量股 → 手，金额保持元。"""
    return AuctionRow(
        code=str(item.get("ticker") or item.get("thscode") or "").split(".")[0],
        name=str(item.get("name") or ""),
        auction_price=_num(item.get("auction_price")),
        auction_pct=_num(item.get("auction_pct")),
        auction_volume=_shares_to_lots(item.get("auction_volume")),
        auction_amount=_num(item.get("auction_amount")),
        unmatched=_shares_to_lots(item.get("auction_unmatched")),
        turnover_pct=_num(item.get("auction_turnover_pct")),
        yesterday_ratio_pct=_num(item.get("auction_yesterday_ratio_pct")),
        volume_ratio=_num(item.get("auction_volume_ratio")),
        pre_close=_num(item.get("pre_close_price")),
        open_price=_num(item.get("open_price")),
    )


def parse_benchmark_row(item: dict[str, Any]) -> BenchmarkRow:
    """风向标单条 → `BenchmarkRow`。"""
    return BenchmarkRow(
        code=str(item.get("ticker") or item.get("thscode") or "").split(".")[0],
        name=str(item.get("name") or ""),
        auction_pct=_num(item.get("auction_pct")),
        tags=[str(t) for t in item.get("tags") or []],
    )


def parse_index_quote(item: dict[str, Any]) -> Quote:
    """指数快照单条 → `Quote`。指数无名称字段，用内置映射，查不到就显示代码。"""
    thscode = str(item.get("thscode") or item.get("ticker") or "")
    return Quote(
        symbol=thscode,
        name=_INDEX_NAMES.get(thscode, thscode),
        price=_num(item.get("last_price")) or 0.0,
        prev_close=_num(item.get("prev_price")),
        open=_num(item.get("open_price")),
        high=_num(item.get("high_price")),
        low=_num(item.get("low_price")),
        change=_num(item.get("price_change")),
        change_pct=_num(item.get("price_change_ratio_pct")),
        volume=_shares_to_lots(item.get("volume")),
        amount=_num(item.get("turnover")),
        source="ths",
        as_of=_now(),
    )


def parse_limit_row(item: dict[str, Any]) -> LimitPoolRow:
    """涨跌停 / 炸板池单条 → `LimitPoolRow`。三种池字段富余不同，按有无取。"""
    return LimitPoolRow(
        code=str(item.get("ticker") or ""),
        name=str(item.get("name") or ""),
        price=_num(item.get("last_price")),
        change_pct=_num(item.get("price_change_ratio_pct")),
        time_text=str(item.get("limit_up_time") or item.get("last_limit_time") or ""),
        continue_text=str(item.get("continue_day_text") or ""),
        continue_cnt=_int_or_none(item.get("continue_day_cnt")),
        reason=str(item.get("limit_up_reason") or ""),
        seal_money=_num(item.get("seal_money")),
        open_times=_int_or_none(item.get("open_times")),
        turnover_pct=_num(item.get("turnover_ratio_pct")),
    )


def parse_dragon_row(item: dict[str, Any]) -> DragonTigerRow:
    """龙虎榜股票维度单条 → `DragonTigerRow`。change / net_rate 上游是小数形式。"""
    return DragonTigerRow(
        code=str(item.get("ticker") or ""),
        name=str(item.get("name") or ""),
        change_pct=_scale(item.get("change"), 100.0),
        net_value=_num(item.get("net_value")),
        buy_value=_num(item.get("buy_value")),
        sell_value=_num(item.get("sell_value")),
        reason=str(item.get("limit_reason") or ""),
        range_days=_int_or_none(item.get("range_days")),
        org_net_value=_num(item.get("org_net_value")),
        org_buy_num=_int_or_none(item.get("org_buy_num")),
        org_sell_num=_int_or_none(item.get("org_sell_num")),
    )


def parse_anomaly(item: dict[str, Any]) -> AnomalyRow:
    """异动解读单条 → `AnomalyRow`。"""
    return AnomalyRow(
        code=str(item.get("thscode") or "").split(".")[0],
        name=str(item.get("stock_name") or ""),
        tag=str(item.get("tag_name") or ""),
        content=str(item.get("analysis_content") or ""),
        keywords=[str(k) for k in item.get("keyword_list") or []],
    )


def parse_bar(item: dict[str, Any]) -> KlineBar | None:
    """K 线单条记录 → `KlineBar`。四价缺任一就丢弃整根 —— 残行比缺行更危险。"""
    open_ = _num(item.get("open_price"))
    close = _num(item.get("close_price"))
    high = _num(item.get("high_price"))
    low = _num(item.get("low_price"))
    if None in (open_, close, high, low):
        return None
    return KlineBar(
        date=_ms_to_date(item.get("date_ms")),
        open=open_,  # type: ignore[arg-type]
        close=close,  # type: ignore[arg-type]
        high=high,  # type: ignore[arg-type]
        low=low,  # type: ignore[arg-type]
        volume=_shares_to_lots(item.get("volume")),
        amount=_num(item.get("turnover")),
    )


def aggregate_bars(daily: list[KlineBar], period: str) -> list[KlineBar]:
    """日线聚合为周线 / 月线。date 取组内最后一根的日期（周期结束日）。"""
    if period not in ("weekly", "monthly"):
        return daily

    def key(bar: KlineBar) -> tuple[int, int]:
        d = dt.date.fromisoformat(bar.date)
        if period == "weekly":
            iso = d.isocalendar()
            return (iso[0], iso[1])
        return (d.year, d.month)

    grouped: list[KlineBar] = []
    current_key: tuple[int, int] | None = None
    for bar in daily:
        k = key(bar)
        if current_key is None or k != current_key:
            grouped.append(
                KlineBar(
                    date=bar.date,
                    open=bar.open,
                    close=bar.close,
                    high=bar.high,
                    low=bar.low,
                    volume=bar.volume,
                    amount=bar.amount,
                )
            )
            current_key = k
            continue
        head = grouped[-1]
        head.date = bar.date
        head.high = max(head.high, bar.high)
        head.low = min(head.low, bar.low)
        head.close = bar.close
        head.volume = _sum_opt(head.volume, bar.volume)
        head.amount = _sum_opt(head.amount, bar.amount)
    return grouped


def parse_financial(
    rows: list[dict[str, Any]],
    *,
    balance_rows: list[dict[str, Any]],
    periods: int,
    name: str,
    fallback_symbol: str = "",
) -> FinancialSummary:
    """利润表 + 资产负债表 → `FinancialSummary`。

    THS 给的是报表原文，FinancialSummary 里的比率字段（毛利率 / 净利率 /
    资产负债率 / 同比）由本层从原文**确定性计算** —— 不是估算，公式固定。
    缺失字段保持 None，不要用 0 冒充。
    """
    income_by_key = {(r.get("fiscal_year"), r.get("fiscal_period")): r for r in rows}
    rows = rows[:periods]
    balance_by_key = {
        (r.get("fiscal_year"), r.get("fiscal_period")): r for r in balance_rows
    }

    periods_out: list[FinancialPeriod] = []
    for row in rows:
        fy = row.get("fiscal_year")
        fp = row.get("fiscal_period")
        revenue = _num(row.get("operating_income"))
        costs = _num(row.get("operating_costs"))
        net_profit = _num(row.get("parent_holder_net_profit"))

        prev = income_by_key.get((fy - 1 if isinstance(fy, int) else None, fp))
        balance = balance_by_key.get((fy, fp))

        periods_out.append(
            FinancialPeriod(
                period=f"{fy}{_FIN_LABEL.get(str(fp), str(fp))}",
                report_date=_ms_to_date(row.get("report_date_ms")),
                revenue=revenue,
                revenue_yoy=_pct_change(revenue, _num((prev or {}).get("operating_income"))),
                net_profit=net_profit,
                net_profit_yoy=_pct_change(
                    net_profit, _num((prev or {}).get("parent_holder_net_profit"))
                ),
                eps=_num(row.get("basic_eps")),
                gross_margin=_ratio(revenue - costs if costs is not None else None, revenue),
                net_margin=_ratio(net_profit, revenue),
                debt_ratio=_ratio(
                    _num((balance or {}).get("total_debt")),
                    _num((balance or {}).get("assets_total")),
                ),
            )
        )

    first = rows[0] if rows else {}
    return FinancialSummary(
        symbol=str(first.get("ticker") or fallback_symbol),
        name=name,
        periods=periods_out,
        source="ths",
        as_of=_now(),
    )


# ---------------------------------------------------------------------- 代码转换


def _try_to_thscode(symbol: str) -> str | None:
    """'600519' / 'sh600519' / '600519.SH' → '600519.SH'；像名称则返回 None。"""
    raw = symbol.strip().upper()
    if _THS_CODE_RE.match(raw):
        return raw
    if len(raw) > 2 and raw[:2] in ("SH", "SZ", "BJ") and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"
    if raw.isdigit() and len(raw) == 6:
        return f"{raw}.{_guess_suffix(raw)}"
    return None


def _split_symbols(symbol: str) -> list[str]:
    """逗号分隔（中英文逗号都认）的标的列表 → 非空 token 列表。"""
    tokens = [t.strip() for t in symbol.replace("，", ",").split(",") if t.strip()]
    if not tokens:
        raise DataSourceError("股票代码或名称为空")
    return tokens


def _guess_suffix(code: str) -> str:
    """按代码首位猜交易所后缀。920 开头是北交所新代码段，要在 9 之前判。"""
    if code.startswith("92") or code[0] in "48":
        return "BJ"
    if code[0] in "569":
        return "SH"
    if code[0] in "0123":
        return "SZ"
    raise DataSourceError(f"无法判断 {code} 属于哪个交易所，请带上后缀（如 600519.SH）")


# ---------------------------------------------------------------------- 信封与单位


def _envelope_error(payload: Any, *, what: str) -> tuple[bool, str] | None:
    """统一信封检查。None = 成功；`(retryable, message)` = 业务错误。"""
    if not isinstance(payload, dict) or "code" not in payload:
        return False, f"ths {what}响应缺少统一信封（code 字段），接口可能已变更"
    code = payload.get("code")
    if code == 0:
        return None
    message = str(payload.get("message") or "")
    if code == 2001:
        return False, (
            "ths API Key 无效或缺失（code=2001）。"
            "请检查 NIULAI_THS_API_KEY 或 config.toml [datasource] ths_api_key"
        )
    if code == 2003:
        return False, f"ths API Key 无权访问{what}接口（code=2003），需开通对应权限"
    if code == 3001:
        return False, f"ths 找不到该标的（code=3001），请确认代码是否正确：{message}"
    if isinstance(code, int) and 1001 <= code <= 1004:
        return False, f"ths {what}参数不合法（code={code}）：{message}"
    if code in _RETRYABLE_CODES:
        return True, f"ths {what}暂时不可用（code={code}：{message}）"
    return False, f"ths {what}返回未知业务码 code={code}：{message}"


def _shares_to_lots(value: Any) -> float | None:
    """股 → 手（×1/100）。THS 全线的成交量都是股。"""
    num = _num(value)
    return None if num is None else num / 100


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scale(value: Any, factor: float) -> float | None:
    num = _num(value)
    return None if num is None else num * factor


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """百分比比率。分母缺失或为 0 都给 None —— 除零比 0% 诚实。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator * 100


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1) * 100


def _sum_opt(a: float | None, b: float | None) -> float | None:
    if a is None and b is None:
        return None
    return (a or 0.0) + (b or 0.0)


def _fill_change_pct(bars: list[KlineBar]) -> None:
    """THS 的 K 线不带涨跌幅，从相邻收盘价确定性补出。首根无前收盘，留 None。"""
    for prev, bar in zip(bars, bars[1:]):
        if prev.close:
            bar.change_pct = (bar.close - prev.close) / prev.close * 100


def _ms_to_date(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return dt.datetime.fromtimestamp(value / 1000, tz=_TZ_CN).date().isoformat()


def _now_ms() -> int:
    return int(dt.datetime.now(tz=_TZ_CN).timestamp() * 1000)


def _now() -> str:
    return dt.datetime.now(tz=_TZ_CN).isoformat(timespec="seconds")
