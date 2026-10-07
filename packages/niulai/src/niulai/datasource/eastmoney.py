"""东方财富数据源（纯 HTTP，不依赖 akshare / pandas）。

为什么第一个源选它：

- 覆盖全：行情 / K 线 / 财务 / 板块 / 资金流都在同一族接口下；
- 纯 JSON over HTTP，httpx 直接拿，包体零负担（akshare 会拖进 pandas + numpy）；
- 有名称搜索接口，所以「茅台」能直接解析成 600519。

⚠️ 这些是非官方公开接口，属灰色地带。**仅用于学习研究**，
   数据版权归原平台所有，请控制请求频率、勿商用。

## 两个已经踩过的坑（改这里之前先看）

1. **价格字段要按小数位缩放，成交量和成交额不要。** 东财的 `f43` 是
   `125257` 而不是 `1252.57`，缩放系数在 `f59`（小数位）里；但 `f47`/`f48`
   本身就是原始单位。这是全项目最容易写错的一处，已用测试锁住。

2. **前复权会让长期高分红股的早期价格为负。** 茅台 2001 年的前复权价是
   `-312.68` —— 不是数据错误，是复权算法在累计分红超过早期股价时的必然结果。
   所以 K 线默认只取最近 N 根，并允许显式指定复权方式。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import Any

import httpx

from ..errors import DataSourceError
from ..logs import get_logger
from .base import (
    Capability,
    FinancialPeriod,
    FinancialSummary,
    KlineBar,
    KlineSeries,
    MoneyflowDay,
    MoneyflowSeries,
    Quote,
    SectorBoard,
    SectorRow,
)

_log = get_logger("datasource.eastmoney")


# 实时行情快照：最新价 / 涨跌 / 今开 / 最高 / 最低 / 昨收 / 成交量额，
# 字段名是 f43 这类无语义代号，由下面的 F 表映射成可读名
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
# 历史 K 线：日 / 周 / 月（klt）、前 / 后复权（fqt），每根是一行固定 11 字段的 CSV
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
# 证券模糊搜索：把「茅台」这类名称 / 拼音解析成 6 位代码 + 市场号（_resolve 的兜底）
SEARCH_URL = "https://searchapi.eastmoney.com/api/suggest/get"
# 定期报告主要财务指标：营收 / 归母净利 / ROE 等。那边一次返回 165 个字段，
# 只挑 FINANCE_FIELDS 里列的这些
FINANCE_URL = "https://datacenter.eastmoney.com/securities/api/data/get"
# 板块榜单：概念 / 行业，一次返回板块涨跌幅、主力净流入、领涨股
SECTOR_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 个股资金流日序列：主力 = 超大单 + 大单，按天给净流入与净占比
MONEYFLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"

# 这是东方财富网页前端在用的公开 token，不是谁家的私钥。
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

# 重试次数。**东财会限流**：连续请求几次之后会直接断开连接而不返回响应
# （`RemoteProtocolError: Server disconnected without sending a response`）。
# 这是真实会发生的事，不是网络抖动 —— 不重试的话用户第一次撞上就失败。
DEFAULT_ATTEMPTS = 3
# 4xx 里只有 429 值得重试；其余是请求本身有问题，重试没有意义
_RETRYABLE_STATUS = {408, 425, 429}

_QUOTE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; niulai/0.1; +https://github.com/)",
    "Referer": "https://quote.eastmoney.com/",
}
_DATA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; niulai/0.1; +https://github.com/)",
    "Referer": "https://data.eastmoney.com/",
}

# 东财的字段名全是 f+数字，没有语义，所以在这里集中映射一次。
F = {
    "last": "f43",
    "open": "f46",
    "high": "f44",
    "low": "f45",
    "prev_close": "f60",
    "change": "f169",
    "change_pct": "f170",
    "volume": "f47",
    "amount": "f48",
    "code": "f57",
    "name": "f58",
    "decimals": "f59",
}

QUOTE_FIELDS = ",".join(F.values())

# K 线：fields2 的字段顺序是固定的，这里按位置声明，别改顺序。
KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
KLINE_INDEX = {
    "date": 0,
    "open": 1,
    "close": 2,
    "high": 3,
    "low": 4,
    "volume": 5,
    "amount": 6,
    "amplitude": 7,
    "change_pct": 8,
    "change": 9,
    "turnover": 10,
}

# klt：K 线周期；fqt：复权方式
KLT = {"daily": "101", "weekly": "102", "monthly": "103"}
FQT = {"none": "0", "forward": "1", "backward": "2"}

# 板块：fs 决定板块类型，fid 决定排序字段（po=1 倒序 = 从高到低）
SECTOR_FS = {"concept": "m:90+t:2", "industry": "m:90+t:1"}
SECTOR_FID = {"change_pct": "f3", "moneyflow": "f62"}
# 板块行的字段代号，实测锁定的映射（f136 领涨股涨跌幅、f140 领涨股代码）
SECTOR_FIELD_NAMES = {
    "change_pct": "f3",
    "code": "f12",
    "name": "f14",
    "main_net": "f62",
    "stock_up": "f104",
    "stock_down": "f105",
    "leader_name": "f128",
    "leader_change_pct": "f136",
    "leader_code": "f140",
}
SECTOR_FIELDS = ",".join(SECTOR_FIELD_NAMES.values())

# 资金流 fields2 的字段顺序固定，按位置解析（与 KLINE_INDEX 同理）
MONEYFLOW_FIELDS = "f51,f52,f53,f54,f55,f56,f62,f63"
MONEYFLOW_INDEX = {
    "date": 0,
    "main_net": 1,  # 主力净流入（元）= 超大单 + 大单
    "small_net": 2,  # 小单
    "mid_net": 3,  # 中单
    "big_net": 4,  # 大单
    "super_net": 5,  # 超大单
    "close": 6,
    "change_pct": 7,
}

# 财务接口的字段 → 我们的字段名。**只挑要用的**：那边返回 165 个字段。
FINANCE_FIELDS = {
    "revenue": "TOTALOPERATEREVE",
    "revenue_yoy": "TOTALOPERATEREVETZ",
    "net_profit": "PARENTNETPROFIT",
    "net_profit_yoy": "PARENTNETPROFITTZ",
    "deduct_net_profit": "KCFJCXSYJLR",
    "eps": "EPSJB",
    "bps": "BPS",
    "roe": "ROEJQ",
    "gross_margin": "XSMLL",
    "net_margin": "XSJLL",
    "debt_ratio": "ZCFZL",
    "ocf_per_share": "MGJYXJJE",
}

# 交易所前缀：东财约定 1 = 上交所，0 = 深交所 / 北交所
_MARKET_BY_PREFIX = {"SH": "1", "SZ": "0", "BJ": "0"}


class EastmoneyDataSource:
    """东方财富源：行情 / K 线 / 财务。"""

    id = "eastmoney"
    capabilities: set[Capability] = {"quote", "kline", "financial", "sector", "moneyflow"}

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        attempts: int = DEFAULT_ATTEMPTS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._attempts = max(1, attempts)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ 行情

    async def quote(self, symbol: str) -> Quote:
        secid, _ = await self._resolve(symbol)
        params = {"secid": secid, "fields": QUOTE_FIELDS, "invt": "2", "fltt": "1"}
        payload = await self._get_json(QUOTE_URL, params, headers=_QUOTE_HEADERS, what="行情")

        data = payload.get("data")
        if not data:
            raise DataSourceError(f"eastmoney 没有返回「{symbol}」的行情（代码可能不存在或已停牌）")

        return parse_quote(data)

    # ------------------------------------------------------------------ K 线

    async def kline(
        self,
        symbol: str,
        *,
        period: str = "daily",
        adjust: str = "forward",
        limit: int = 60,
    ) -> KlineSeries:
        if period not in KLT:
            raise DataSourceError(f"不支持的周期「{period}」，可选：{'、'.join(KLT)}")
        if adjust not in FQT:
            raise DataSourceError(f"不支持的复权方式「{adjust}」，可选：{'、'.join(FQT)}")

        secid, code = await self._resolve(symbol)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": KLINE_FIELDS,
            "klt": KLT[period],
            "fqt": FQT[adjust],
            # 不传 beg：让服务端从最新一根往回取。传 beg=0 会从上市首日开始，
            # 配合前复权就会拿到负价格（见模块开头坑 2）。
            "end": "20500101",
            "lmt": str(max(1, min(limit, 500))),
        }
        payload = await self._get_json(KLINE_URL, params, headers=_QUOTE_HEADERS, what="K 线")

        data = payload.get("data")
        if not data or not data.get("klines"):
            raise DataSourceError(
                f"eastmoney 没有返回「{symbol}」的 K 线（代码可能不存在、已退市或当日停牌）"
            )

        return parse_kline(
            data,
            period=period,
            adjust=adjust,
            fallback_symbol=code,
        )

    # ------------------------------------------------------------------ 财务

    async def financial(self, symbol: str, *, periods: int = 4) -> FinancialSummary:
        secid, code = await self._resolve(symbol)
        secucode = _to_secucode(secid, code)

        params = {
            "type": "RPT_F10_FINANCE_MAINFINADATA",
            "sty": "ALL",
            "filter": f'(SECUCODE="{secucode}")',
            "p": "1",
            "ps": str(max(1, min(periods, 12))),
            "sr": "-1",
            "st": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        }
        payload = await self._get_json(
            FINANCE_URL, params, headers=_DATA_HEADERS, what="财务指标"
        )

        result = payload.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            raise DataSourceError(
                f"eastmoney 没有返回「{symbol}」的财务数据"
                "（指数、基金、部分新股没有定期报告口径的财务指标）"
            )

        return parse_financial(rows, fallback_symbol=code)

    # ------------------------------------------------------------------ 板块

    async def sectors(
        self,
        *,
        kind: str = "concept",
        sort: str = "change_pct",
        limit: int = 10,
    ) -> SectorBoard:
        if kind not in SECTOR_FS:
            raise DataSourceError(f"不支持的板块类型「{kind}」，可选：{'、'.join(SECTOR_FS)}")
        if sort not in SECTOR_FID:
            raise DataSourceError(f"不支持的排序方式「{sort}」，可选：{'、'.join(SECTOR_FID)}")

        params = {
            "pn": "1",  # 第一页
            "pz": str(max(1, min(limit, 100))),
            "po": "1",  # 倒序（从高到低）
            "np": "1",
            "fltt": "2",  # 数值已按小数位缩放好
            "invt": "2",
            "fid": SECTOR_FID[sort],
            "fs": SECTOR_FS[kind],
            "fields": SECTOR_FIELDS,
        }
        payload = await self._get_json(SECTOR_URL, params, headers=_QUOTE_HEADERS, what="板块")
        rows = ((payload.get("data") or {}).get("diff")) or []
        if not rows:
            raise DataSourceError(f"eastmoney 没有返回{kind}板块榜单")

        return parse_sectors(rows, kind=kind, sort=sort)

    # ------------------------------------------------------------------ 资金流

    async def moneyflow(self, symbol: str, *, days: int = 5) -> MoneyflowSeries:
        secid, code = await self._resolve(symbol)
        params = {
            "lmt": str(max(1, min(days, 60))),
            "klt": "101",  # 日线
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": MONEYFLOW_FIELDS,
        }
        payload = await self._get_json(MONEYFLOW_URL, params, headers=_QUOTE_HEADERS, what="资金流")

        data = payload.get("data") or {}
        if not data.get("klines"):
            raise DataSourceError(
                f"eastmoney 没有返回「{symbol}」的资金流（指数、新股或长期停牌可能没有）"
            )

        return parse_moneyflow(data, fallback_symbol=code)

    # ------------------------------------------------------------------ 内部

    async def _get_json(
        self,
        url: str,
        params: dict[str, str],
        *,
        headers: dict[str, str],
        what: str,
    ) -> dict[str, Any]:
        """带重试的 GET。

        错误分两类，跟 LLM 层同一个原则：**可重试的才重试**。
        连接被断、超时、429、5xx 都可以再试；400/404 试一百次也是一样的结果，
        早失败早报错。
        """
        last_error: str = "未知错误"

        for attempt in range(self._attempts):
            _log.debug("GET %s (%s，第 %d 次)", url, what, attempt + 1)
            try:
                resp = await self._client.get(url, params=params, headers=headers)
                if resp.status_code in _RETRYABLE_STATUS or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                else:
                    resp.raise_for_status()
                    return resp.json()
            except httpx.HTTPStatusError as exc:
                raise DataSourceError(
                    f"eastmoney {what}请求被拒绝（HTTP {exc.response.status_code}）："
                    "请检查代码 / 参数是否正确；若持续出现，可能是被限流了。"
                ) from exc
            except httpx.TransportError as exc:
                # 限流最典型的表现就是这个：服务端直接掐断，不给响应
                last_error = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                # 返回了非 JSON（被限流时常见：给回一段 HTML 风控页）
                last_error = f"响应不是 JSON（{exc}）"

            if attempt < self._attempts - 1:
                delay = 0.4 * 2**attempt + random.uniform(0, 0.3)
                _log.warning(
                    "eastmoney %s 第 %d/%d 次失败（%s），%.1f 秒后重试",
                    what,
                    attempt + 1,
                    self._attempts,
                    last_error,
                    delay,
                )
                # 退避 + 抖动：限流场景下整齐重试会再次撞在一起
                await asyncio.sleep(delay)

        raise DataSourceError(
            f"eastmoney {what}请求失败（已重试 {self._attempts} 次）：{last_error}。"
            "多半是被限流了，稍等一会儿再试。"
        )

    async def _resolve(self, symbol: str) -> tuple[str, str]:
        """把 '600519' / 'sh600519' / '贵州茅台' / '茅台' 统一成 `(secid, code)`。"""
        raw = symbol.strip()
        if not raw:
            raise DataSourceError("股票代码或名称为空")

        upper = raw.upper()
        if len(upper) > 2 and upper[:2] in _MARKET_BY_PREFIX and upper[2:].isdigit():
            market = _MARKET_BY_PREFIX[upper[:2]]
            return f"{market}.{upper[2:]}", upper[2:]

        if raw.isdigit() and len(raw) == 6:
            return f"{_guess_market(raw)}.{raw}", raw

        return await self._search(raw)

    async def _search(self, keyword: str) -> tuple[str, str]:
        params = {"input": keyword, "type": "14", "token": SEARCH_TOKEN, "count": "5"}
        payload = await self._get_json(SEARCH_URL, params, headers=_QUOTE_HEADERS, what="搜索")

        items = (payload.get("QuotationCodeTable") or {}).get("Data") or []
        for item in items:
            code = item.get("Code")
            market = item.get("MktNum")
            if code and market not in (None, ""):
                return f"{market}.{code}", str(code)

        raise DataSourceError(f"找不到与「{keyword}」匹配的标的，请改用 6 位股票代码")


# ---------------------------------------------------------------------- 解析


def parse_quote(data: dict[str, Any]) -> Quote:
    """把东财的字段字典转成 `Quote`。

    做成模块级函数而不是方法，是为了能脱离网络单独测试 —— 字段缩放是最容易写错的地方
    （价格按小数位缩放，但成交量 / 成交额本身已是原始单位，**不能缩放**）。
    """
    decimals = int(data.get(F["decimals"]) or 2)
    price_scale = 10.0**decimals

    return Quote(
        symbol=str(data.get(F["code"]) or ""),
        name=str(data.get(F["name"]) or ""),
        price=_num(data.get(F["last"]), price_scale) or 0.0,
        open=_num(data.get(F["open"]), price_scale),
        high=_num(data.get(F["high"]), price_scale),
        low=_num(data.get(F["low"]), price_scale),
        prev_close=_num(data.get(F["prev_close"]), price_scale),
        change=_num(data.get(F["change"]), price_scale),
        change_pct=_num(data.get(F["change_pct"]), 100.0),
        volume=_num(data.get(F["volume"])),
        amount=_num(data.get(F["amount"])),
        source="eastmoney",
        as_of=_now(),
    )


def parse_kline(
    data: dict[str, Any],
    *,
    period: str,
    adjust: str,
    fallback_symbol: str = "",
) -> KlineSeries:
    """把 `data.klines` 里的 CSV 行转成 K 线序列。

    一行长这样（11 个字段，顺序固定）：
        2026-09-22,1252.15,1253.80,1265.88,1248.10,24573,3088526148.00,1.42,0.10,1.23,0.20
    对应 KLINE_INDEX 里的键。**按位置取值，不要按名字猜。**
    """
    bars: list[KlineBar] = []
    for row in data.get("klines") or []:
        parts = str(row).split(",")
        if len(parts) < 9:
            continue
        open_ = _num(parts[KLINE_INDEX["open"]])
        close = _num(parts[KLINE_INDEX["close"]])
        high = _num(parts[KLINE_INDEX["high"]])
        low = _num(parts[KLINE_INDEX["low"]])
        if None in (open_, close, high, low):
            continue
        bars.append(
            KlineBar(
                date=parts[KLINE_INDEX["date"]],
                open=open_,  # type: ignore[arg-type]
                close=close,  # type: ignore[arg-type]
                high=high,  # type: ignore[arg-type]
                low=low,  # type: ignore[arg-type]
                volume=_num(parts[KLINE_INDEX["volume"]]),
                amount=_num(parts[KLINE_INDEX["amount"]]),
                change_pct=_num(_at(parts, KLINE_INDEX["change_pct"])),
                turnover=_num(_at(parts, KLINE_INDEX["turnover"])),
            )
        )

    return KlineSeries(
        symbol=str(data.get("code") or fallback_symbol),
        name=str(data.get("name") or fallback_symbol),
        period=period,
        adjust=adjust,
        bars=bars,
        source="eastmoney",
        as_of=_now(),
    )


def parse_financial(rows: list[dict[str, Any]], *, fallback_symbol: str = "") -> FinancialSummary:
    """把财务接口的行转成 `FinancialSummary`。

    缺失字段保持 `None`，**不要填 0** —— 银行 / 保险类的字段大量为 None，
    填 0 会让模型得出"这家公司毛利率 0%"这种荒谬结论。
    """
    periods: list[FinancialPeriod] = []
    first = rows[0] if rows else {}

    for row in rows:
        periods.append(
            FinancialPeriod(
                period=str(row.get("REPORT_DATE_NAME") or ""),
                report_date=str(row.get("REPORT_DATE") or ""),
                **{name: _num(row.get(field)) for name, field in FINANCE_FIELDS.items()},
            )
        )

    return FinancialSummary(
        symbol=str(first.get("SECURITY_CODE") or fallback_symbol),
        name=str(first.get("SECURITY_NAME_ABBR") or fallback_symbol),
        periods=periods,
        source="eastmoney",
        as_of=_now(),
    )


def parse_sectors(rows: list[dict[str, Any]], *, kind: str, sort: str) -> SectorBoard:
    """把 clist 的板块行转成 `SectorBoard`。字段名映射见 SECTOR_FIELD_NAMES。"""
    sectors = [
        SectorRow(
            code=str(row.get(SECTOR_FIELD_NAMES["code"]) or ""),
            name=str(row.get(SECTOR_FIELD_NAMES["name"]) or ""),
            change_pct=_num(row.get(SECTOR_FIELD_NAMES["change_pct"])),
            main_net=_num(row.get(SECTOR_FIELD_NAMES["main_net"])),
            leader_name=str(row.get(SECTOR_FIELD_NAMES["leader_name"]) or ""),
            leader_code=str(row.get(SECTOR_FIELD_NAMES["leader_code"]) or ""),
            leader_change_pct=_num(row.get(SECTOR_FIELD_NAMES["leader_change_pct"])),
            stock_up=_count(row.get(SECTOR_FIELD_NAMES["stock_up"])),
            stock_down=_count(row.get(SECTOR_FIELD_NAMES["stock_down"])),
        )
        for row in rows
        if row.get(SECTOR_FIELD_NAMES["name"])
    ]

    return SectorBoard(kind=kind, sort=sort, rows=sectors, source="eastmoney", as_of=_now())


def parse_moneyflow(data: dict[str, Any], *, fallback_symbol: str = "") -> MoneyflowSeries:
    """把 fflow/daykline 的 CSV 行转成 `MoneyflowSeries`。

    行长这样（8 个字段，顺序固定，见 MONEYFLOW_INDEX）：
        2026-09-30,533565520.0,-539065.0,-533026464.0,121427904.0,412137616.0,1258.62,1.86
    主力(下标1) = 大单(4) + 超大单(5)，实测数值自洽 —— 若某天对不上，
    优先怀疑字段顺序变了。
    """
    days: list[MoneyflowDay] = []
    for row in data.get("klines") or []:
        parts = str(row).split(",")
        if len(parts) < len(MONEYFLOW_INDEX):
            continue
        days.append(
            MoneyflowDay(
                date=parts[MONEYFLOW_INDEX["date"]],
                main_net=_num(parts[MONEYFLOW_INDEX["main_net"]]),
                small_net=_num(parts[MONEYFLOW_INDEX["small_net"]]),
                mid_net=_num(parts[MONEYFLOW_INDEX["mid_net"]]),
                big_net=_num(parts[MONEYFLOW_INDEX["big_net"]]),
                super_net=_num(parts[MONEYFLOW_INDEX["super_net"]]),
                close=_num(parts[MONEYFLOW_INDEX["close"]]),
                change_pct=_num(parts[MONEYFLOW_INDEX["change_pct"]]),
            )
        )

    return MoneyflowSeries(
        symbol=str(data.get("code") or fallback_symbol),
        name=str(data.get("name") or fallback_symbol),
        days=days,
        source="eastmoney",
        as_of=_now(),
    )


def _count(value: Any) -> int | None:
    """家数类字段：正常是整数，异常（'-' 等）给 None，别让类型渗出去。"""
    return value if isinstance(value, int) else None


def _guess_market(code: str) -> str:
    """按代码首位猜交易所。猜不出来就让用户写 sh / sz 前缀，不要瞎猜。"""
    if code[0] in "569":
        return "1"
    if code[0] in "0123":
        return "0"
    raise DataSourceError(f"无法判断 {code} 属于哪个交易所，请加上 sh / sz 前缀")


def _to_secucode(secid: str, code: str) -> str:
    """`1.600519` → `600519.SH`。财务接口要的是后者。"""
    market = secid.split(".", 1)[0]
    if market == "1":
        suffix = "SH"
    elif code and code[0] in "48":
        suffix = "BJ"  # 北交所：83/87/88/43/920 等
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _at(parts: list[str], index: int) -> Any:
    return parts[index] if index < len(parts) else None


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _num(value: Any, scale: float = 1.0) -> float | None:
    """东财用字符串 '-' 表示缺失（停牌、新股无昨收等）。"""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value) / scale
    except (TypeError, ValueError):
        return None
