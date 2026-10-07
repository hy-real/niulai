"""腾讯财经数据源（纯 HTTP），东财的轻量兜底。

选它做第二源的理由：

- 风控比东财宽松：连续请求不易被掐断，行情接口响应极快；
- 行情字段比东财还富：五档盘口、内外盘、量比、流通市值、主力净流入，
  为 P3 的 moneyflow 能力留了路；
- 纯 HTTP + 文本/JSON，无重依赖。

代价与坑（改这里之前先看）：

1. **没有名称搜索**。只认代码（600519 / sh600519）。收到中文股票名时
   直接抛错，让 `MarketDataService` 的路由自动降级到东财的搜索 ——
   兜底源不做自己不擅长的事。
2. 行情返回 **GBK 编码纯文本**，`v_sh600519="1~贵州茅台~..."`，`~` 分隔、
   全靠下标取值（见 TQ 表）；价格已是正常小数，**不需要按小数位缩放**。
3. 行情的成交额单位是**万元**（下标 37），要 ×1e4 换算成元；
   成交量单位是手，与全项目约定一致。
4. K 线行只有「日期,开,收,高,低,量」6 列，没有成交额 / 涨跌幅列；
   涨跌幅由相邻收盘价补算，首根为 None。

⚠️ 与东财同属非官方公开接口，仅用于学习研究，请控制频率、勿商用。
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from ..errors import DataSourceError
from ..logs import get_logger
from .base import Capability, KlineBar, KlineSeries, Quote, to_market_code

_log = get_logger("datasource.tencent")

# 实时行情快照：纯文本，多标的一次可查（逗号分隔），这里单标的最简单
QUOTE_URL = "https://qt.gtimg.cn/q="
# 历史 K 线：日 / 周 / 月，qfq 前复权 / hfq 后复权 / 空为不复权
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

DEFAULT_ATTEMPTS = 3
# 与东财同一原则：4xx 里只有 429 值得重试
_RETRYABLE_STATUS = {408, 425, 429}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; niulai/0.1; +https://github.com/)",
    "Referer": "https://gu.qq.com/",
}

# 行情文本的字段下标表。腾讯的字段没有名字只有位置，与东财的 F 表同理：
# 集中映射一次，官方改字段顺序时只改这里（靠测试样本锁住）。
TQ = {
    "name": 1,
    "code": 2,
    "last": 3,
    "prev_close": 4,
    "open": 5,
    "volume": 6,  # 手
    "time": 30,  # 形如 20261007150003
    "change": 31,
    "change_pct": 32,
    "high": 33,
    "low": 34,
    "amount": 37,  # 万元！全项目其他地方都用元
}

# K 线：period → 腾讯的周期名；adjust → 腾讯的复权前缀（不复权是空前缀）
_PERIOD_NAME = {"daily": "day", "weekly": "week", "monthly": "month"}
_ADJUST_PREFIX = {"forward": "qfq", "backward": "hfq", "none": ""}


class TencentDataSource:
    """腾讯财经源：行情 / K 线。"""

    id = "tencent"
    capabilities: set[Capability] = {"quote", "kline"}

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
        code = to_market_code(symbol)
        payload = await self._fetch_text(QUOTE_URL + code, what="行情")
        try:
            quote = parse_quote(payload)
        except DataSourceError as exc:
            # 停牌 / 不存在的代码返回字段残缺的空壳 —— 报错带上原始代码才可读
            raise DataSourceError(
                f"tencent 没有返回「{symbol}」的行情（代码可能不存在或已停牌）: {exc}"
            ) from exc

        if quote.price <= 0:
            raise DataSourceError(
                f"tencent 没有返回「{symbol}」的行情（代码可能不存在或已停牌）"
            )
        return quote

    # ------------------------------------------------------------------ K 线

    async def kline(
        self,
        symbol: str,
        *,
        period: str = "daily",
        adjust: str = "forward",
        limit: int = 60,
    ) -> KlineSeries:
        if period not in _PERIOD_NAME:
            raise DataSourceError(f"不支持的周期「{period}」，可选：{'、'.join(_PERIOD_NAME)}")
        if adjust not in _ADJUST_PREFIX:
            raise DataSourceError(f"不支持的复权方式「{adjust}」，可选：{'、'.join(_ADJUST_PREFIX)}")

        code = to_market_code(symbol)
        params = {
            # param=代码,周期,开始,结束,数量,复权 —— 开始/结束留空 = 从最近一根往回取
            "param": f"{code},{_PERIOD_NAME[period]},,,{max(1, min(limit, 500))},{_ADJUST_PREFIX[adjust]}",
        }
        payload = await self._fetch_text(KLINE_URL, params=params, what="K 线")

        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise DataSourceError(f"tencent K 线响应不是 JSON（可能被限流）: {exc}") from exc

        series = parse_kline(data, code=code, period=period, adjust=adjust)
        if not series.bars:
            raise DataSourceError(
                f"tencent 没有返回「{symbol}」的 K 线（代码可能不存在、已退市或当日停牌）"
            )
        return series

    # ------------------------------------------------------------------ 内部

    async def _fetch_text(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        what: str = "数据",
    ) -> str:
        """带重试的 GET。重试判定与东财同一条原则：可重试的才重试。"""
        last_error: str = "未知错误"

        for attempt in range(self._attempts):
            _log.debug("GET %s (%s，第 %d 次)", url, what, attempt + 1)
            try:
                resp = await self._client.get(url, params=params, headers=_HEADERS)
                if resp.status_code in _RETRYABLE_STATUS or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                else:
                    resp.raise_for_status()
                    # 行情是 GBK，K 线 JSON 是 UTF-8。先按 UTF-8 严格解码，
                    # 失败说明是 GBK 文本 —— 两个接口一劳永逸地兼容掉
                    try:
                        return resp.content.decode("utf-8")
                    except UnicodeDecodeError:
                        return resp.content.decode("gbk", errors="replace")
            except httpx.HTTPStatusError as exc:
                raise DataSourceError(
                    f"tencent {what}请求被拒绝（HTTP {exc.response.status_code}）："
                    "请检查代码 / 参数是否正确。"
                ) from exc
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                last_error = f"响应异常（{exc}）"

            if attempt < self._attempts - 1:
                delay = 0.4 * 2**attempt + random.uniform(0, 0.3)
                _log.warning(
                    "tencent %s 第 %d/%d 次失败（%s），%.1f 秒后重试",
                    what, attempt + 1, self._attempts, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise DataSourceError(
            f"tencent {what}请求失败（已重试 {self._attempts} 次）：{last_error}"
        )


# ---------------------------------------------------------------------- 解析


def parse_quote(text: str) -> Quote:
    """把腾讯的 `v_sh600519="..."` 文本转成 `Quote`。

    模块级函数、脱离网络可测 —— 字段全靠下标取值，顺序错了不报错只错位，
    与东财的 parse_quote 同一教训：**用真实样本的测试锁住映射表**。
    """
    body = text.split('"')[1] if '"' in text else ""
    parts = body.split("~")
    if len(parts) <= TQ["amount"]:
        raise DataSourceError("tencent 行情响应字段不足（代码可能不存在或已停牌）")

    amount_wan = _num(parts[TQ["amount"]])  # 万元，全项目统一用元
    return Quote(
        symbol=parts[TQ["code"]],
        name=parts[TQ["name"]],
        price=_num(parts[TQ["last"]]) or 0.0,
        prev_close=_num(parts[TQ["prev_close"]]),
        open=_num(parts[TQ["open"]]),
        high=_num(parts[TQ["high"]]),
        low=_num(parts[TQ["low"]]),
        change=_num(parts[TQ["change"]]),
        change_pct=_num(parts[TQ["change_pct"]]),
        volume=_num(parts[TQ["volume"]]),
        amount=amount_wan * 1e4 if amount_wan is not None else None,
        source="tencent",
        as_of=_format_time(parts[TQ["time"]]),
    )


def parse_kline(
    data: dict[str, Any],
    *,
    code: str,
    period: str,
    adjust: str,
) -> KlineSeries:
    """把 fqkline 的 JSON 转成 K 线序列。

    行是「日期,开,收,高,低,量」6 列，按位置取值。没有涨跌幅列，
    用前一根收盘价补算 —— 首根没有参照，保持 None，**不要硬造数**。
    """
    node = (data.get("data") or {}).get(code) or {}
    key = f"{_ADJUST_PREFIX[adjust]}{_PERIOD_NAME[period]}"  # 如 qfqday / week
    rows = node.get(key) or node.get(_PERIOD_NAME[period]) or []

    bars: list[KlineBar] = []
    prev_close: float | None = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        open_ = _num(row[1])
        close = _num(row[2])
        high = _num(row[3])
        low = _num(row[4])
        if None in (open_, close, high, low):
            continue
        change_pct = (
            round((close / prev_close - 1) * 100, 2) if prev_close else None
        )
        bars.append(
            KlineBar(
                date=str(row[0]),
                open=open_,  # type: ignore[arg-type]
                close=close,  # type: ignore[arg-type]
                high=high,  # type: ignore[arg-type]
                low=low,  # type: ignore[arg-type]
                volume=_num(row[5]),
                change_pct=change_pct,
            )
        )
        prev_close = close

    qt_row = (node.get("qt") or {}).get(code) or []
    name = str(qt_row[1]) if len(qt_row) > 1 else code

    return KlineSeries(
        symbol=code[2:],  # sh600519 → 600519
        name=name,
        period=period,
        adjust=adjust,
        bars=bars,
        source="tencent",
        as_of="",
    )


def _format_time(raw: str) -> str:
    """'20261007150003' → '2026-10-07 15:00:03'。解析不了就空着。"""
    if len(raw) == 14 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    return ""


def _num(value: Any, scale: float = 1.0) -> float | None:
    """腾讯用空串表示缺失（停牌等），与东财的 '-' 同理。"""
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value) / scale
    except (TypeError, ValueError):
        return None
