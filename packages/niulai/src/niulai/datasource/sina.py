"""新浪财经数据源（纯 HTTP），第三源：quote / sector / moneyflow 兜底。

性格与腾讯互补：接口**老但一直活着**（hq.sinajs 行情十几年没变过格式），
覆盖腾讯没有的板块与资金流。代价（改这里之前先看）：

1. **必须带 `Referer: https://finance.sina.com.cn`**，否则 403 —— 2022 年起
   的新增门槛，测试样本里要锁住这一点；
2. 行情是 **GBK 编码纯文本**，逗号分隔（SIQ 表）；成交量单位是**股**
   （东财 / 腾讯都是手），要 ÷100 换算；涨跌幅没有现成字段，由现价 / 昨收补算；
3. 板块返回的是 **JS 变量**（`var S_Finance_bankuai_class = {...}`），不是
   标准 JSON —— 剥掉前缀再 `json.loads`；只有涨跌幅排序，**没有主力净流入**，
   所以 `sort="moneyflow"` 显式抛错降级，不要硬装会；
4. 资金流是标准 JSON（唯一不用剥壳的），但**没有股票名称**，行序是
   从新到旧，要反转成全项目统一的「从旧到新」。

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
from .base import (
    Capability,
    MoneyflowDay,
    MoneyflowSeries,
    Quote,
    SectorBoard,
    SectorRow,
    SectorStockList,
    SectorStockRow,
    to_market_code,
)

_log = get_logger("datasource.sina")

# 实时行情快照：GBK 纯文本，逗号分隔
QUOTE_URL = "https://hq.sinajs.cn/list="
# 板块榜单：newFLJK 的 param=class 是概念板块；行业板块是另一个端点
SECTOR_URL = {
    "concept": "https://money.finance.sina.com.cn/q/view/newFLJK.php?param=class",
    "industry": "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
}
# 个股资金流历史：标准 JSON，从新到旧
MONEYFLOW_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "MoneyFlow.ssl_qsfx_lscjfb"
)
# 板块成分股列表：node 接受 gn_ / chgn_ / new_ 等节点代码，与板块榜单同源
SECTOR_STOCKS_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
# 成分股的排序字段：全项目语义 → 新浪参数
SECTOR_STOCKS_SORT = {"change_pct": "changepercent", "amount": "amount", "turnover": "turnoverratio"}

# 板块节点代码前缀：带这些前缀的直接当节点用，否则当名称去榜单里解析
_SECTOR_CODE_PREFIXES = ("gn_", "chgn_", "new_", "diyu_")

DEFAULT_ATTEMPTS = 3
_RETRYABLE_STATUS = {408, 425, 429}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; niulai/0.1; +https://github.com/)",
    # 新浪的门槛：没有这个 Referer 直接 403
    "Referer": "https://finance.sina.com.cn",
}

# 行情文本的字段下标表（逗号分隔）。新浪的字段顺序同样没有官方文档。
SIQ = {
    "name": 0,
    "open": 1,
    "prev_close": 2,
    "last": 3,
    "high": 4,
    "low": 5,
    "volume": 8,  # 股！全项目统一用首
    "amount": 9,  # 元
    "date": 30,
    "time": 31,
}

# 板块行的字段下标（行内逗号分隔）：gn_hwqc,华为汽车,97,均价,涨跌额,涨跌幅,...
SBS = {
    "code": 0,
    "name": 1,
    "stock_count": 2,
    "change_pct": 5,
    "leader_name": 12,
}


class SinaDataSource:
    """新浪财经源：行情 / 板块 / 资金流（全部兜底性质）。"""

    id = "sina"
    capabilities: set[Capability] = {"quote", "sector", "sector_stocks", "moneyflow"}

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
        quote = parse_quote(payload)

        if quote.price <= 0:
            raise DataSourceError(
                f"sina 没有返回「{symbol}」的行情（代码可能不存在或已停牌）"
            )
        return quote

    # ------------------------------------------------------------------ 板块

    async def sectors(
        self,
        *,
        kind: str = "concept",
        sort: str = "change_pct",
        limit: int = 10,
    ) -> SectorBoard:
        if kind not in SECTOR_URL:
            raise DataSourceError(f"不支持的板块类型「{kind}」，可选：{'、'.join(SECTOR_URL)}")
        if sort != "change_pct":
            # 新浪板块没有主力净流入字段 —— 装会只会给出误导性的顺序
            raise DataSourceError(
                f"sina 板块不支持按「{sort}」排序（没有主力净流入字段），仅支持 change_pct"
            )

        payload = await self._fetch_text(SECTOR_URL[kind], what="板块")
        board = parse_sectors(payload, kind=kind, limit=limit)
        if not board.rows:
            raise DataSourceError(f"sina 没有返回{kind}板块榜单")
        return board

    # ------------------------------------------------------------------ 成分股

    async def sector_stocks(
        self,
        symbol: str,
        *,
        sort: str = "change_pct",
        limit: int = 10,
    ) -> SectorStockList:
        if sort not in SECTOR_STOCKS_SORT:
            raise DataSourceError(
                f"不支持的排序方式「{sort}」，可选：{'、'.join(SECTOR_STOCKS_SORT)}"
            )

        node, node_name = await self._resolve_node(symbol)
        params = {
            "node": node,
            "sort": SECTOR_STOCKS_SORT[sort],
            "asc": "0",  # 从高到低
            "page": "1",
            "num": str(max(1, min(limit, 80))),
        }
        payload = await self._fetch_text(SECTOR_STOCKS_URL, params=params, what="板块成分股")

        try:
            rows = json.loads(payload)
        except ValueError as exc:
            raise DataSourceError(f"sina 板块成分股响应不是 JSON（可能被限流）: {exc}") from exc
        if not rows:
            raise DataSourceError(f"sina 没有返回板块「{node_name}」（{node}）的成分股")

        return parse_sector_stocks(rows, node=node, node_name=node_name, sort=sort)

    async def _resolve_node(self, symbol: str) -> tuple[str, str]:
        """把 'gn_hwqc' / '华为汽车' 统一成 `(节点代码, 板块名称)`。

        带节点前缀的直接放行；名称则去板块榜单里找（先概念后行业）——
        榜单与成分股接口共用同一套节点代码，不用引节点树。
        """
        raw = symbol.strip()
        if not raw:
            raise DataSourceError("板块代码或名称为空")

        lower = raw.lower()
        if lower.startswith(_SECTOR_CODE_PREFIXES):
            return lower, await self._node_name(lower, fallback=raw)

        for kind in ("concept", "industry"):
            payload = await self._fetch_text(SECTOR_URL[kind], what="板块")
            board = parse_sectors(payload, kind=kind)  # 全量，供名称匹配
            exact = [r for r in board.rows if r.name == raw]
            if exact:
                return exact[0].code, exact[0].name
            fuzzy = [r for r in board.rows if raw in r.name]
            if len(fuzzy) == 1:
                return fuzzy[0].code, fuzzy[0].name
            if fuzzy:
                names = "、".join(r.name for r in fuzzy[:5])
                raise DataSourceError(f"板块「{raw}」匹配到多个：{names}，请用更完整的名称")

        raise DataSourceError(f"找不到与「{raw}」匹配的板块，可先用 market_sectors 查榜单拿代码")

    async def _node_name(self, node: str, *, fallback: str) -> str:
        """反查节点的可读名称。榜单挂了不阻塞取数，回落显示节点代码。"""
        for kind in ("concept", "industry"):
            try:
                payload = await self._fetch_text(SECTOR_URL[kind], what="板块")
            except DataSourceError:
                continue
            board = parse_sectors(payload, kind=kind)
            for row in board.rows:
                if row.code == node:
                    return row.name
        return fallback

    # ------------------------------------------------------------------ 资金流

    async def moneyflow(self, symbol: str, *, days: int = 5) -> MoneyflowSeries:
        code = to_market_code(symbol)
        params = {
            "page": "1",
            "num": str(max(1, min(days, 60))),
            "sort": "opendate",
            "asc": "0",  # 从新到旧，解析后反转
            "daima": code,
        }
        payload = await self._fetch_text(MONEYFLOW_URL, params=params, what="资金流")

        try:
            rows = json.loads(payload)
        except ValueError as exc:
            raise DataSourceError(f"sina 资金流响应不是 JSON（可能被限流）: {exc}") from exc
        if not rows:
            raise DataSourceError(
                f"sina 没有返回「{symbol}」的资金流（指数、新股或长期停牌可能没有）"
            )

        return parse_moneyflow(rows, symbol=code[2:])

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
                    # 行情 / 板块是 GBK，资金流 JSON 是 UTF-8：先 UTF-8 严格解码，
                    # 失败再回落 GBK，两种接口一劳永逸
                    try:
                        return resp.content.decode("utf-8")
                    except UnicodeDecodeError:
                        return resp.content.decode("gbk", errors="replace")
            except httpx.HTTPStatusError as exc:
                raise DataSourceError(
                    f"sina {what}请求被拒绝（HTTP {exc.response.status_code}）："
                    "请检查代码 / 参数是否正确。"
                ) from exc
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            except ValueError as exc:
                last_error = f"响应异常（{exc}）"

            if attempt < self._attempts - 1:
                delay = 0.4 * 2**attempt + random.uniform(0, 0.3)
                _log.warning(
                    "sina %s 第 %d/%d 次失败（%s），%.1f 秒后重试",
                    what, attempt + 1, self._attempts, last_error, delay,
                )
                await asyncio.sleep(delay)

        raise DataSourceError(
            f"sina {what}请求失败（已重试 {self._attempts} 次）：{last_error}"
        )


# ---------------------------------------------------------------------- 解析


def parse_quote(text: str) -> Quote:
    """把 `var hq_str_sh600519="..."` 文本转成 `Quote`。

    涨跌幅新浪不直接给，由现价 / 昨收补算；昨收缺失时保持 None，**不要硬造**。
    """
    if '"' not in text:
        raise DataSourceError("sina 行情响应格式异常")

    # 变量名里带着市场前缀（hq_str_sh600519），代码要从这里取
    var_name = text.split('"', 1)[0]  # 'var hq_str_sh600519='
    code = var_name.strip().rstrip("=").split("_")[-1]

    parts = text.split('"')[1].split(",")
    if len(parts) <= SIQ["date"]:
        raise DataSourceError("sina 行情响应字段不足（代码可能不存在或已停牌）")

    price = _num(parts[SIQ["last"]])
    prev_close = _num(parts[SIQ["prev_close"]])
    change = (
        price - prev_close
        if price is not None and prev_close is not None
        else None
    )
    change_pct = (
        round((price / prev_close - 1) * 100, 2)
        if price is not None and prev_close
        else None
    )
    volume_share = _num(parts[SIQ["volume"]])  # 股

    date = parts[SIQ["date"]]
    time_ = parts[SIQ["time"]]
    return Quote(
        symbol=code[2:],
        name=parts[SIQ["name"]],
        price=price or 0.0,
        prev_close=prev_close,
        open=_num(parts[SIQ["open"]]),
        high=_num(parts[SIQ["high"]]),
        low=_num(parts[SIQ["low"]]),
        change=change,
        change_pct=change_pct,
        volume=volume_share / 100 if volume_share is not None else None,  # 股 → 手
        amount=_num(parts[SIQ["amount"]]),
        source="sina",
        as_of=f"{date} {time_}" if date and time_ else "",
    )


def parse_sectors(text: str, *, kind: str, limit: int | None = None) -> SectorBoard:
    """把 JS 变量形式的板块数据转成 `SectorBoard`。

    原始形态：var S_Finance_bankuai_class = {"gn_hwqc":"gn_hwqc,华为汽车,97,...", ...}
    服务端给什么顺序就什么顺序进门，这里**按涨跌幅排序** —— 源端不支持
    fid 排序参数，排序语义只能由解析层兑现。
    """
    if "=" not in text:
        raise DataSourceError("sina 板块响应格式异常")
    body = text.split("=", 1)[1].strip().rstrip(";")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DataSourceError(f"sina 板块响应不是合法 JSON（可能被限流）: {exc}") from exc

    rows: list[SectorRow] = []
    for raw in data.values() if isinstance(data, dict) else []:
        parts = str(raw).split(",")
        if len(parts) <= SBS["leader_name"] or not parts[SBS["name"]]:
            continue
        stock_count = _num(parts[SBS["stock_count"]])
        rows.append(
            SectorRow(
                code=parts[SBS["code"]],
                name=parts[SBS["name"]],
                change_pct=_num(parts[SBS["change_pct"]]),
                stock_count=int(stock_count) if stock_count is not None else None,
                leader_name=parts[SBS["leader_name"]],
            )
        )

    rows.sort(key=lambda r: r.change_pct if r.change_pct is not None else -1e9, reverse=True)
    if limit is not None:
        rows = rows[: max(1, limit)]
    return SectorBoard(kind=kind, sort="change_pct", rows=rows, source="sina", as_of="")


def parse_moneyflow(rows: list[dict[str, Any]], *, symbol: str) -> MoneyflowSeries:
    """把资金流 JSON 行转成 `MoneyflowSeries`。

    主力口径对齐东财：主力 = 超大单 + 大单（r0_net + r1_net）。
    实测自洽：r0_net + r1_net + r2_net + r3_net = netamount。
    服务端从新到旧给行，**反转成从旧到新** —— 与 K 线的时间语义一致。
    """
    days: list[MoneyflowDay] = []
    for row in rows:
        r0_net = _num(row.get("r0_net"))
        r1_net = _num(row.get("r1_net"))
        main_net = (
            None
            if r0_net is None and r1_net is None
            else (r0_net or 0.0) + (r1_net or 0.0)
        )
        changeratio = _num(row.get("changeratio"))  # 比例，如 0.0186 = +1.86%
        days.append(
            MoneyflowDay(
                date=str(row.get("opendate") or ""),
                main_net=main_net,
                super_net=r0_net,
                big_net=r1_net,
                mid_net=_num(row.get("r2_net")),
                small_net=_num(row.get("r3_net")),
                close=_num(row.get("trade")),
                change_pct=round(changeratio * 100, 2) if changeratio is not None else None,
                turnover=_num(row.get("turnover")),
            )
        )

    days.reverse()
    return MoneyflowSeries(symbol=symbol, name=symbol, days=days, source="sina", as_of="")


def parse_sector_stocks(
    rows: list[dict[str, Any]],
    *,
    node: str,
    node_name: str,
    sort: str,
) -> SectorStockList:
    """把 getHQNodeData 的行转成 `SectorStockList`。

    两个单位换算（报告里没提醒、最容易错的地方）：
    - volume 是**股** → 手（÷100）；
    - mktcap / nmc 是**万元** → 元（×1e4）。
    停牌股 trade / sell 可能是 "0.000"，按缺失处理，不让 0 冒充价格。
    """
    stocks: list[SectorStockRow] = []
    for row in rows:
        raw_price = str(row.get("trade") or "")
        price = None if raw_price in ("0.000", "0", "") else _num(raw_price)
        volume_share = _num(row.get("volume"))
        market_cap = _num(row.get("mktcap"))
        float_cap = _num(row.get("nmc"))
        stocks.append(
            SectorStockRow(
                code=str(row.get("code") or ""),
                name=str(row.get("name") or ""),
                price=price,
                change_pct=_num(row.get("changepercent")),
                volume=volume_share / 100 if volume_share is not None else None,
                amount=_num(row.get("amount")),
                turnover=_num(row.get("turnoverratio")),
                pe=_num(row.get("per")),
                pb=_num(row.get("pb")),
                market_cap=market_cap * 1e4 if market_cap is not None else None,
                float_cap=float_cap * 1e4 if float_cap is not None else None,
            )
        )

    return SectorStockList(
        node=node, name=node_name, rows=stocks, sort=sort, source="sina", as_of=""
    )


def _num(value: Any, scale: float = 1.0) -> float | None:
    """空串 / '-' 一律 None，与东财、腾讯的解析同一约定。"""
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value) / scale
    except (TypeError, ValueError):
        return None
