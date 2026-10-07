"""同花顺数据源测试：不联网，mock httpx。

真实接口调用不放进单元测试 —— 联网验证走 `python -m niulai ask "茅台现在多少钱"`。
响应样本取自 2026-10 的真实抓包。
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from niulai.datasource import ThsDataSource
from niulai.datasource.ths import (
    _envelope_error,
    _fill_change_pct,
    _guess_suffix,
    _try_to_thscode,
    aggregate_bars,
    parse_anomaly,
    parse_bar,
    parse_dragon_row,
    parse_financial,
    parse_limit_row,
    parse_quote,
    parse_valuation,
)
from niulai.errors import DataSourceError

_TZ = dt.timezone(dt.timedelta(hours=8))


def _ms(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, tzinfo=_TZ).timestamp() * 1000)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ths(handler, **kwargs) -> ThsDataSource:
    return ThsDataSource(api_key="test-key", client=_client(handler), **kwargs)


def _route(handler_map: dict[str, httpx.Response]):
    """按路径前缀路由的 mock handler。键是路径片段，值是响应或 callable(request)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for fragment, response in handler_map.items():
            if fragment in url:
                return response(request) if callable(response) else response
        raise AssertionError(f"mock 没有覆盖该路径：{url}")

    return handler


# 真实抓包样本（2026-10-07，贵州茅台）
SEARCH_ITEM = {
    "thscode": "600519.SH",
    "ticker": "600519",
    "name": "贵州茅台",
    "exchange": "SH",
    "asset_type": "a-share",
    "currency": "CNY",
    "list_date": None,
    "end_date": None,
    "last_trade_date": None,
    "last_delivery_date": None,
}

SNAPSHOT_ITEM = {
    "thscode": "600519.SH",
    "ticker": "600519",
    "volume": 3833098,  # 股
    "turnover": 4797246600,
    "last_price": 1258.62,
    "price_change": 23.04,
    "price_change_ratio_pct": 1.864711,
    "open_price": 1239.53,
    "high_price": 1268,
    "low_price": 1236.05,
    "prev_price": 1235.58,
}

SEARCH_OK = httpx.Response(200, json={"code": 0, "message": "success", "data": {"item": [SEARCH_ITEM]}})


def _envelope(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "message": "success", "data": data})


# ---------------------------------------------------------------------- 代码转换


def test_完整_thscode_原样通过():
    assert _try_to_thscode("600519.SH") == "600519.SH"
    assert _try_to_thscode("000858.sz") == "000858.SZ"


def test_带前缀的代码转换():
    assert _try_to_thscode("sh600519") == "600519.SH"
    assert _try_to_thscode("sz000858") == "000858.SZ"


def test_纯代码按首位猜后缀():
    assert _try_to_thscode("600519") == "600519.SH"
    assert _try_to_thscode("300750") == "300750.SZ"
    assert _try_to_thscode("830799") == "830799.BJ"
    assert _try_to_thscode("430047") == "430047.BJ"
    assert _try_to_thscode("920002") == "920002.BJ"  # 北交所新代码段，要在 9 之前判
    assert _try_to_thscode("900901") == "900901.SH"


def test_名称返回None交给检索():
    assert _try_to_thscode("贵州茅台") is None
    assert _try_to_thscode("60051") is None  # 位数不对也不猜


def test_猜不出后缀显式报错():
    """0-9 里只有 7 开头没有对应交易所（4/8 归北交所，9 归沪市）。"""
    with pytest.raises(DataSourceError):
        _guess_suffix("700000")


# ---------------------------------------------------------------------- 行情


def test_快照解析与单位换算():
    """THS 成交量是股，项目约定是手 —— 换算发生在数据源层。"""
    quote = parse_quote(SNAPSHOT_ITEM, name="贵州茅台")
    assert quote.symbol == "600519"
    assert quote.price == pytest.approx(1258.62)
    assert quote.prev_close == pytest.approx(1235.58)
    assert quote.change_pct == pytest.approx(1.864711)
    assert quote.volume == pytest.approx(38330.98)  # 股 → 手
    assert quote.amount == pytest.approx(4797246600)
    assert quote.source == "ths"


def test_快照缺失字段保持None():
    item = {k: v for k, v in SNAPSHOT_ITEM.items() if k not in ("prev_price", "volume")}
    quote = parse_quote(item, name="x")
    assert quote.prev_close is None
    assert quote.volume is None


async def test_纯代码行情会补一次检索换名称():
    """快照不返回名称，所以纯代码输入也要走一次 search。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "tickers/search" in url:
            assert "q=600519" in url
            return SEARCH_OK
        return _envelope({"item": [SNAPSHOT_ITEM]})

    quote = await _ths(handler).quote("600519")
    assert quote.name == "贵州茅台"
    assert quote.price == pytest.approx(1258.62)
    assert len(calls) == 2


async def test_名称输入经检索解析成代码():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tickers/search" in url:
            assert "q=%E8%8C%85%E5%8F%B0" in url  # 「茅台」URL 编码
            return SEARCH_OK
        return _envelope({"item": [SNAPSHOT_ITEM]})

    quote = await _ths(handler).quote("茅台")
    assert quote.symbol == "600519"
    assert quote.name == "贵州茅台"


async def test_检索无结果给人话报错():
    handler = _route({"tickers/search": _envelope({"item": []})})
    with pytest.raises(DataSourceError) as exc:
        await _ths(handler).quote("不存在的股票")
    assert "找不到" in str(exc.value)


# ---------------------------------------------------------------------- K 线

KLINE_ITEMS = [
    {"date_ms": _ms(2026, 9, 21), "open_price": 1262.99, "high_price": 1265.88, "low_price": 1256.10, "close_price": 1257.12, "volume": 2489100, "turnover": 3135849108.0},
    {"date_ms": _ms(2026, 9, 22), "open_price": 1259.00, "high_price": 1259.95, "low_price": 1250.80, "close_price": 1252.57, "volume": 2501700, "turnover": 3135910045.0},
]


def test_k线解析与单位换算():
    bar = parse_bar(KLINE_ITEMS[0])
    assert bar is not None
    assert bar.date == "2026-09-21"
    assert bar.close == pytest.approx(1257.12)
    assert bar.volume == pytest.approx(24891)  # 股 → 手
    assert bar.amount == pytest.approx(3135849108.0)


def test_k线四价缺失整根丢弃():
    assert parse_bar({"date_ms": _ms(2026, 9, 21), "open_price": 1.0}) is None


def test_周线聚合取组内极值与末日():
    daily = [
        parse_bar({"date_ms": _ms(2026, 9, 21), "open_price": 10.0, "high_price": 11.0, "low_price": 9.0, "close_price": 10.5, "volume": 1000, "turnover": 1000.0}),
        parse_bar({"date_ms": _ms(2026, 9, 23), "open_price": 10.5, "high_price": 12.0, "low_price": 10.0, "close_price": 11.5, "volume": 2000, "turnover": 2000.0}),
        parse_bar({"date_ms": _ms(2026, 9, 24), "open_price": 11.5, "high_price": 13.0, "low_price": 11.0, "close_price": 12.0, "volume": 3000, "turnover": 3000.0}),
        parse_bar({"date_ms": _ms(2026, 9, 28), "open_price": 12.0, "high_price": 12.5, "low_price": 11.5, "close_price": 12.2, "volume": 4000, "turnover": 4000.0}),
    ]
    weekly = aggregate_bars(daily, "weekly")
    assert len(weekly) == 2
    first, second = weekly
    assert first.date == "2026-09-24"  # 组内最后一根的日期
    assert first.open == pytest.approx(10.0)
    assert first.close == pytest.approx(12.0)
    assert first.high == pytest.approx(13.0)
    assert first.low == pytest.approx(9.0)
    assert first.volume == pytest.approx(60)  # 6000 股 → 60 手
    assert second.date == "2026-09-28"


def test_涨跌幅由相邻收盘价补出():
    bars = [parse_bar(item) for item in KLINE_ITEMS]
    bars = [b for b in bars if b is not None]
    _fill_change_pct(bars)
    assert bars[0].change_pct is None  # 首根没有前收盘
    expected = (1252.57 - 1257.12) / 1257.12 * 100
    assert bars[1].change_pct == pytest.approx(expected)


async def test_k线请求带上窗口参数():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "historical" in url:
            captured.update(dict(request.url.params))
            return _envelope({"item": KLINE_ITEMS})
        return SEARCH_OK

    series = await _ths(handler).kline("600519", period="daily", limit=10)
    assert captured["thscode"] == "600519.SH"
    assert captured["interval"] == "1d"
    assert captured["adjust"] == "forward"
    assert int(captured["start"]) < int(captured["end"])
    assert series.name == "贵州茅台"
    assert series.source == "ths"
    assert len(series.bars) == 2


async def test_周线由日线聚合而来():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "historical" in url:
            return _envelope({"item": KLINE_ITEMS})
        return SEARCH_OK

    series = await _ths(handler).kline("600519", period="weekly", limit=2)
    assert series.period == "weekly"
    assert all("W" not in b.date for b in series.bars)


async def test_k线空数据给人话报错():
    handler = _route({"historical": _envelope({"item": []}), "tickers/search": SEARCH_OK})
    with pytest.raises(DataSourceError) as exc:
        await _ths(handler, attempts=1).kline("600519")
    assert "K 线" in str(exc.value)


# ---------------------------------------------------------------------- 财务

INCOME_ROWS = [
    {  # 最近一期
        "thscode": "600519.SH", "ticker": "600519", "period": "quarterly",
        "fiscal_year": 2026, "fiscal_period": "Q2",
        "report_date_ms": _ms(2026, 8, 28), "period_end_ms": _ms(2026, 6, 30),
        "currency": "CNY",
        "operating_income": 922.78e8, "operating_costs": 96.45e8,
        "net_profit": 460.0e8, "parent_holder_net_profit": 445.17e8,
        "basic_eps": 35.57,
    },
    {
        "thscode": "600519.SH", "ticker": "600519", "period": "quarterly",
        "fiscal_year": 2026, "fiscal_period": "Q1",
        "report_date_ms": _ms(2026, 4, 25), "period_end_ms": _ms(2026, 3, 31),
        "operating_income": 500.0e8, "operating_costs": 50.0e8,
        "basic_eps": 20.0,
    },
    {  # 上年同期 —— 同比的分母
        "thscode": "600519.SH", "ticker": "600519", "period": "quarterly",
        "fiscal_year": 2025, "fiscal_period": "Q2",
        "report_date_ms": _ms(2025, 8, 29), "period_end_ms": _ms(2025, 6, 30),
        "operating_income": 900.0e8, "parent_holder_net_profit": 430.0e8,
    },
]

BALANCE_ROWS = [
    {
        "thscode": "600519.SH", "ticker": "600519", "period": "quarterly",
        "fiscal_year": 2026, "fiscal_period": "Q2",
        "assets_total": 2500e8, "total_debt": 380e8,
        "holder_equity_total": 2000e8,
    },
]


def test_财务字段映射与比率计算():
    summary = parse_financial(
        INCOME_ROWS, balance_rows=BALANCE_ROWS, periods=2, name="贵州茅台"
    )
    assert summary.name == "贵州茅台"
    assert len(summary.periods) == 2

    latest = summary.periods[0]
    assert latest.period == "2026中报"
    assert latest.report_date == "2026-08-28"
    assert latest.revenue == pytest.approx(922.78e8)
    assert latest.net_profit == pytest.approx(445.17e8)
    assert latest.eps == pytest.approx(35.57)
    # 毛利率 = (营收 - 营业成本) / 营收
    assert latest.gross_margin == pytest.approx((922.78e8 - 96.45e8) / 922.78e8 * 100)
    # 资产负债率 = 负债 / 资产
    assert latest.debt_ratio == pytest.approx(380 / 2500 * 100)


def test_财务同比由上年同期算出():
    summary = parse_financial(
        INCOME_ROWS, balance_rows=BALANCE_ROWS, periods=1, name="x"
    )
    latest = summary.periods[0]
    assert latest.revenue_yoy == pytest.approx((922.78 / 900.0 - 1) * 100)
    assert latest.net_profit_yoy == pytest.approx((445.17 / 430.0 - 1) * 100)


def test_财务缺失同比与比率保持None():
    summary = parse_financial(INCOME_ROWS, balance_rows=[], periods=2, name="x")
    q1 = summary.periods[1]  # 2026Q1 没有上年同期数据、没有资产负债表
    assert q1.revenue_yoy is None
    assert q1.debt_ratio is None


async def test_财务走利润表加资产负债表两个请求():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "income-statements" in url:
            return _envelope({"item": INCOME_ROWS})
        if "balance-sheets" in url:
            return _envelope({"item": BALANCE_ROWS})
        return SEARCH_OK

    summary = await _ths(handler).financial("600519", periods=2)
    assert len(summary.periods) == 2
    assert len(calls) == 3  # 检索 + 利润表 + 资产负债表


# ---------------------------------------------------------------------- 信封与重试


def test_信封错误分类():
    assert _envelope_error({"code": 0}, what="行情") is None
    # 不可重试
    for code in (1001, 1002, 1003, 1004, 2001, 2003, 3001, 9999):
        assert _envelope_error({"code": code, "message": "x"}, what="行情")[0] is False
    # 可重试
    for code in (4001, 5001, 5002, 5003):
        assert _envelope_error({"code": code, "message": "x"}, what="行情")[0] is True
    # 缺信封
    assert _envelope_error({"foo": 1}, what="行情")[0] is False


def test_信封2001报错里带配置指引():
    _, message = _envelope_error({"code": 2001, "message": "unauthorized"}, what="行情")
    assert "NIULAI_THS_API_KEY" in message


async def test_限流业务码自动重试():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tickers/search" in url:
            return SEARCH_OK
        calls.append(url)
        if len(calls) < 2:
            return httpx.Response(200, json={"code": 4001, "message": "rate limited"})
        return _envelope({"item": [SNAPSHOT_ITEM]})

    quote = await _ths(handler).quote("600519")
    assert quote.price == pytest.approx(1258.62)
    assert len(calls) == 2


async def test_认证错误不重试():
    """Key 错试一百次也是错，立即失败。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tickers/search" in url:
            return SEARCH_OK
        calls.append(url)
        return httpx.Response(200, json={"code": 2001, "message": "unauthorized"})

    with pytest.raises(DataSourceError) as exc:
        await _ths(handler, attempts=3).quote("600519")
    assert "NIULAI_THS_API_KEY" in str(exc.value)
    assert len(calls) == 1


async def test_5xx会重试():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tickers/search" in url:
            return SEARCH_OK
        calls.append(url)
        if len(calls) < 2:
            return httpx.Response(503)
        return _envelope({"item": [SNAPSHOT_ITEM]})

    quote = await _ths(handler).quote("600519")
    assert len(calls) == 2


async def test_传输错误自动重试():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tickers/search" in url:
            return SEARCH_OK
        calls.append(url)
        if len(calls) < 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return _envelope({"item": [SNAPSHOT_ITEM]})

    quote = await _ths(handler, attempts=3).quote("600519")
    assert len(calls) == 3


def test_构造时缺Key直接拒绝():
    with pytest.raises(ValueError):
        ThsDataSource(api_key="")


# ------------------------------------------------------- 市场视野（全局）

VALUATION_ITEM = {
    "thscode": "600519.SH",
    "ticker": "600519",
    "name": "贵州茅台",
    "pe_ttm": 19.32,
    "pe_mrq": 17.67,
    "pb_mrq": 6.26,
    "ps_ttm": 9.08,
    "pcf_ttm": None,  # 缺失保持 None
}

HOT_ITEM = {
    "thscode": "603822.SH",
    "ticker": "603822",
    "name": "嘉澳环保",
    "rank": 1,
    "heat": "1941909",  # 上游是字符串，保留原样
    "rank_change": 7,
    "rank_trend": "up",
}

LIMIT_UP_ITEM = {
    "thscode": "603986.SH",
    "ticker": "603986",
    "name": "兆易创新",
    "last_price": 118.23,
    "price_change_ratio_pct": 10.0008,
    "limit_up_time": "09:34",
    "limit_up_reason": "存储芯片",
    "continue_day_text": "2连板",
    "continue_day_cnt": 2,
    "seal_money": 123456789.12,
}

DRAGON_ITEM = {
    "thscode": "002407.SZ",
    "ticker": "002407",
    "name": "多氟多",
    "change": 0.09994,  # 小数形式 → 百分数
    "net_value": 1786253128.23,
    "buy_value": 2674755016.05,
    "sell_value": 888501887.82,
    "limit_reason": "半导体级氢氟酸涨价",
    "range_days": 3,
    "org_net_value": 5.0e8,
    "org_buy_num": 3,
    "org_sell_num": 1,
}

ANOMALY_ITEM = {
    "stock_name": "贵州茅台",
    "analysis_content": "公司股价出现异动。",
    "keyword_list": ["白酒", "消费"],
    "thscode": "600519.SH",
    "tag_name": "大涨",
}


def test_估值解析():
    row = parse_valuation(VALUATION_ITEM)
    assert row.code == "600519"
    assert row.name == "贵州茅台"
    assert row.pe_ttm == pytest.approx(19.32)
    assert row.pcf_ttm is None


def test_热榜解析热度保留字符串():
    from niulai.datasource.ths import HotList, HotRow

    row = HotRow(
        code=HOT_ITEM["ticker"], name=HOT_ITEM["name"], rank=HOT_ITEM["rank"],
        heat=HOT_ITEM["heat"], rank_change=HOT_ITEM["rank_change"], trend=HOT_ITEM["rank_trend"],
    )
    text = HotList(board="skyrocket", period="day", rows=[row], source="ths").render()
    assert "飙升榜" in text
    assert "1941909" in text  # 字符串热度不转数值，避免大数精度问题
    assert "+7" in text


def test_涨停池解析连板与封单():
    row = parse_limit_row(LIMIT_UP_ITEM)
    assert row.name == "兆易创新"
    assert row.continue_text == "2连板"
    assert row.continue_cnt == 2
    assert row.reason == "存储芯片"
    assert row.seal_money == pytest.approx(123456789.12)


def test_龙虎榜小数涨跌幅换算为百分数():
    row = parse_dragon_row(DRAGON_ITEM)
    assert row.change_pct == pytest.approx(9.994)
    assert row.net_value == pytest.approx(1786253128.23)
    assert row.range_days == 3
    assert row.org_buy_num == 3


def test_异动解析带关键词():
    row = parse_anomaly(ANOMALY_ITEM)
    assert row.code == "600519"
    assert row.tag == "大涨"
    assert row.keywords == ["白酒", "消费"]


async def test_热榜请求参数与截取():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "hot-stock-list" in url:
            captured.update(dict(request.url.params))
            return _envelope({"item": [HOT_ITEM] * 5})
        raise AssertionError(f"意外路径：{url}")

    board_list = await _ths(handler).hotlist(board="hot", period="hour", limit=3)
    assert captured["period"] == "hour"
    assert len(board_list.rows) == 3
    assert board_list.rows[0].rank == 1


async def test_涨跌停池不支持的模式直接报错():
    source = _ths(_route({}))
    with pytest.raises(DataSourceError):
        await source.hotlist(board="bad")
    with pytest.raises(DataSourceError):
        await source.limitpool(kind="bad")
    with pytest.raises(DataSourceError):
        await source.dragon_tiger(board_type="bad")


async def test_龙虎榜游资榜走聚合字段():
    def handler(request: httpx.Request) -> httpx.Response:
        if "dragon-tiger-list" in str(request.url):
            assert dict(request.url.params)["board_type"] == "hot_money"
            return _envelope({
                "board_type": "hot_money",
                "trade_date": "2026-10-06",
                "stock_items": [],
                "hot_money_items": [{"name": "量化打板", "buying": 52000000.0}],
            })
        raise AssertionError

    board = await _ths(handler).dragon_tiger(board_type="hot_money")
    assert board.hot_money == [("量化打板", 52000000.0)]
    assert "量化打板" in board.render()


async def test_异动按逗号分隔多标的解析():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "anomaly-analysis-stock" in url:
            captured.update(dict(request.url.params))
            return _envelope({"item": [ANOMALY_ITEM]})
        if "tickers/search" in url:
            return SEARCH_OK
        raise AssertionError(f"意外路径：{url}")

    result = await _ths(handler).anomaly("600519, 茅台")  # 名称 + 代码混合，去重后只剩一个
    assert captured["thscodes"] == "600519.SH"
    assert result.rows[0].name == "贵州茅台"
