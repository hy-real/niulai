"""新浪源解析测试：不联网，验证字段下标、单位换算与 JS 变量剥壳。

真实接口调用不放进单元测试。样本来自 2026-10 实测抓取。
"""

from __future__ import annotations

import httpx
import pytest

from niulai.datasource import SinaDataSource
from niulai.datasource.sina import (
    parse_moneyflow,
    parse_quote,
    parse_sector_stocks,
    parse_sectors,
)
from niulai.errors import DataSourceError

# 行情按位置构造，下标即文档（SIQ 表）。逗号分隔。
QUOTE_FIELDS = {
    0: "贵州茅台",
    1: "1239.530",  # 今开
    2: "1235.580",  # 昨收
    3: "1258.620",  # 现价
    4: "1268.000",  # 最高
    5: "1236.050",  # 最低
    8: "3833098",  # 成交量（股！）
    9: "4797246636.000",  # 成交额（元）
    30: "2026-09-30",
    31: "15:00:03",
}
QUOTE_TEXT = (
    'var hq_str_sh600519="' + ",".join(QUOTE_FIELDS.get(i, "0") for i in range(34)) + '";'
)


def test_行情按下标解析():
    quote = parse_quote(QUOTE_TEXT)
    assert quote.symbol == "600519"  # 代码从变量名里取
    assert quote.name == "贵州茅台"
    assert quote.price == pytest.approx(1258.62)
    assert quote.prev_close == pytest.approx(1235.58)
    assert quote.open == pytest.approx(1239.53)
    assert quote.high == pytest.approx(1268.00)
    assert quote.low == pytest.approx(1236.05)


def test_涨跌幅由现价昨收补算():
    """新浪没有涨跌幅字段 —— 由现价/昨收补算，昨收缺失时保持 None。"""
    quote = parse_quote(QUOTE_TEXT)
    assert quote.change == pytest.approx(23.04)
    assert quote.change_pct == pytest.approx(1.86)


def test_成交量从股换算成手():
    """新浪的成交量单位是股，东财 / 腾讯都是手 —— 换算只能在解析层做一次。"""
    assert parse_quote(QUOTE_TEXT).volume == pytest.approx(38330.98)


def test_行情字段不足时报错():
    with pytest.raises(DataSourceError):
        parse_quote('var hq_str_sh000000="";')


# ---------------------------------------------------------------------- 板块

# 原始形态是 JS 变量，值是「板块代码,名称,家数,均价,涨跌额,涨跌幅,...,领涨股名」
SECTORS_TEXT = (
    'var S_Finance_bankuai_class={"gn_hwqc":"gn_hwqc,华为汽车,97,23.96,'
    '-0.287,-1.185,2069201242,31890354954,sz002454,10.000,5.610,0.510,松芝股份",'
    '"gn_BCdc":"gn_BCdc,BC电池,26,61.35,-0.368,-0.596,620775305,15634104984,'
    'sh600732,6.342,11.570,0.690,爱旭股份"};'
)


def test_板块剥壳解析并按涨跌幅排序():
    board = parse_sectors(SECTORS_TEXT, kind="concept", limit=10)
    assert board.source == "sina"
    assert board.sort == "change_pct"
    assert len(board.rows) == 2

    # 降序：BC 电池 -0.596 > 华为汽车 -1.185，BC 电池排第一
    top = board.rows[0]
    assert top.name == "BC电池"
    assert top.change_pct == pytest.approx(-0.596)
    assert top.stock_count == 26
    assert top.leader_name == "爱旭股份"
    assert top.main_net is None  # 新浪没有主力净流入，保持 None 不硬造


def test_板块limit截断():
    board = parse_sectors(SECTORS_TEXT, kind="concept", limit=1)
    assert len(board.rows) == 1


def test_板块渲染带来源与领涨股():
    text = parse_sectors(SECTORS_TEXT, kind="concept", limit=10).render()
    assert "概念板块" in text
    assert "松芝股份" in text
    assert "数据源：sina" in text


# ---------------------------------------------------------------------- 资金流

# 服务端从新到旧给行；主力 = r0_net + r1_net（东财口径），四档合计 = netamount
FLOW_ROWS = [
    {
        "opendate": "2026-09-30",
        "trade": "1258.6200",
        "changeratio": "0.0186471",
        "turnover": "30.6628",
        "netamount": "741042739.94",
        "r0_net": "795888790.66",
        "r1_net": "-51220001.15",
        "r2_net": "-3587566.00",
        "r3_net": "-38483.57",
    },
    {
        "opendate": "2026-09-29",
        "trade": "1235.5800",
        "changeratio": "-0.0067",
        "netamount": "-10000000.00",
        "r0_net": "1000000.00",
        "r1_net": "-11000000.00",
        "r2_net": "0",
        "r3_net": "0",
    },
]


def test_资金流主力口径对齐东财():
    """主力 = 超大单 + 大单。口径不一致会让模型得出相反的资金判断。"""
    day = parse_moneyflow(FLOW_ROWS, symbol="600519").days[0]  # 反转后 09-29 在前
    assert day.date == "2026-09-29"
    assert day.main_net == pytest.approx(1000000.00 - 11000000.00)
    assert day.super_net == pytest.approx(1000000.00)
    assert day.big_net == pytest.approx(-11000000.00)
    assert day.small_net == pytest.approx(0)


def test_资金流行序反转为从旧到新():
    """K 线与资金流的时间语义必须一致，模型按「最近一天」取行时不会拿错。"""
    days = parse_moneyflow(FLOW_ROWS, symbol="600519").days
    assert [d.date for d in days] == ["2026-09-29", "2026-09-30"]


def test_资金流涨跌幅与换手():
    day = parse_moneyflow(FLOW_ROWS, symbol="600519").days[1]
    assert day.change_pct == pytest.approx(1.86)
    assert day.turnover == pytest.approx(30.6628)
    assert day.close == pytest.approx(1258.62)


# ---------------------------------------------------------------------- 成分股

# 实测抓取的成分股行（gn_hwqc 华为汽车，按涨跌幅降序）。字段单位是新浪原始口径。
STOCK_ROWS = [
    {
        "symbol": "sz002454",
        "code": "002454",
        "name": "松芝股份",
        "trade": "5.610",
        "pricechange": 0.51,
        "changepercent": 10,
        "volume": 22094313,  # 股！
        "amount": 121091923,
        "per": 14.385,
        "pb": 1.231,
        "mktcap": 493160.33448,  # 万元！
        "nmc": 492195.501996,  # 万元！
        "turnoverratio": 2.51829,
    },
    {
        "symbol": "sh600081",
        "code": "600081",
        "name": "东风科技",
        "trade": "0.000",  # 停牌股：价格与买卖一价可能为 0
        "changepercent": 0,
        "volume": 0,
        "amount": 0,
        "per": 0,
        "pb": 0,
        "mktcap": 0,
        "nmc": 0,
        "turnoverratio": 0,
    },
]


def test_成分股字段与单位换算():
    stocks = parse_sector_stocks(STOCK_ROWS, node="gn_hwqc", node_name="华为汽车", sort="change_pct")
    assert stocks.node == "gn_hwqc"
    assert stocks.name == "华为汽车"

    top = stocks.rows[0]
    assert top.code == "002454"
    assert top.name == "松芝股份"
    assert top.price == pytest.approx(5.61)
    assert top.change_pct == pytest.approx(10)
    assert top.volume == pytest.approx(220943.13)  # 股 → 手
    assert top.amount == pytest.approx(121091923)
    assert top.turnover == pytest.approx(2.51829)
    assert top.pe == pytest.approx(14.385)
    assert top.pb == pytest.approx(1.231)
    assert top.market_cap == pytest.approx(493160.33448 * 1e4)  # 万元 → 元
    assert top.float_cap == pytest.approx(492195.501996 * 1e4)


def test_成分股停牌零价按缺失处理():
    """trade='0.000' 是停牌，不是真不要钱 —— 0 冒充价格会骗过模型。"""
    row = parse_sector_stocks(
        STOCK_ROWS, node="gn_hwqc", node_name="华为汽车", sort="change_pct"
    ).rows[1]
    assert row.price is None


def test_成分股渲染带板块名与市值():
    text = parse_sector_stocks(
        STOCK_ROWS, node="gn_hwqc", node_name="华为汽车", sort="change_pct"
    ).render()
    assert "华为汽车" in text
    assert "49.3" in text  # 总市值 493160 万 → 元 → 渲染成 49.3 亿


async def test_板块名称解析成节点代码():
    """名称 → 代码复用板块榜单数据（同源节点代码），不引节点树。"""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if "getHQNodeData" in url:
            assert "node=gn_hwqc" in url  # 名称已在服务端解析掉
            return httpx.Response(200, json=STOCK_ROWS)
        return httpx.Response(200, content=SECTORS_TEXT.encode("gbk"))

    source = SinaDataSource(client=_client(handler), attempts=1)
    stocks = await source.sector_stocks("华为汽车", limit=2)
    assert stocks.node == "gn_hwqc"
    assert len(stocks.rows) == 2
    assert any("newFLJK" in u for u in requests)  # 名称解析确实走了榜单


async def test_未知板块给可读报错():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SECTORS_TEXT.encode("gbk"))

    source = SinaDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError) as exc:
        await source.sector_stocks("不存在的板块")
    assert "不存在的板块" in str(exc.value)


# ---------------------------------------------------------------------- 重试


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_断连会自动重试():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, content=QUOTE_TEXT.encode("gbk"))

    source = SinaDataSource(client=_client(handler), attempts=3)
    assert (await source.quote("600519")).name == "贵州茅台"
    assert len(calls) == 2


async def test_GBK编码的行情能正确解码():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=QUOTE_TEXT.encode("gbk"))

    source = SinaDataSource(client=_client(handler), attempts=1)
    assert (await source.quote("600519")).name == "贵州茅台"


async def test_板块不支持按资金流排序时显式报错():
    """装会只会给出误导性顺序 —— 显式报错让路由层换东财。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不该发出请求")

    source = SinaDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError) as exc:
        await source.sectors(kind="concept", sort="moneyflow")
    assert "moneyflow" in str(exc.value)


async def test_名称解析显式拒绝():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不该发出请求")

    source = SinaDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError):
        await source.quote("茅台")
