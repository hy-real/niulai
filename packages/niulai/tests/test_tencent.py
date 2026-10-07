"""腾讯源解析测试：不联网，验证下标映射、单位换算与代码推断。

真实接口调用不放进单元测试。联网验证走临时脚本或
`python -m niulai ask "600519 现在多少钱"`。
"""

from __future__ import annotations

import httpx
import pytest

from niulai.datasource import TencentDataSource
from niulai.datasource.base import to_market_code
from niulai.datasource.tencent import parse_kline, parse_quote
from niulai.errors import DataSourceError

# 行情文本按字段位置构造，下标即文档：TQ 表里的每个键都能在这里对号。
# 未列出的位置填 "0" 占位（腾讯返回里也有大量无语义字段）。
QUOTE_FIELDS = {
    1: "贵州茅台",
    2: "600519",
    3: "1252.57",  # 最新价
    4: "1257.12",  # 昨收
    5: "1259.00",  # 今开
    6: "25017",  # 成交量（手）
    30: "20261007150003",  # 时间
    31: "-4.55",  # 涨跌
    32: "-0.36",  # 涨跌幅 %
    33: "1259.95",  # 最高
    34: "1250.80",  # 最低
    37: "313591.00",  # 成交额（万元！）
}
QUOTE_TEXT = 'v_sh600519="' + "~".join(QUOTE_FIELDS.get(i, "0") for i in range(50)) + '";'


def test_行情按下标解析():
    quote = parse_quote(QUOTE_TEXT)
    assert quote.symbol == "600519"
    assert quote.name == "贵州茅台"
    assert quote.price == pytest.approx(1252.57)
    assert quote.prev_close == pytest.approx(1257.12)
    assert quote.open == pytest.approx(1259.00)
    assert quote.high == pytest.approx(1259.95)
    assert quote.low == pytest.approx(1250.80)
    assert quote.change == pytest.approx(-4.55)
    assert quote.change_pct == pytest.approx(-0.36)
    assert quote.volume == pytest.approx(25017)


def test_成交额从万元换算成元():
    """腾讯行情的成交额单位是万元，全项目统一用元 —— 换算只能在解析层做一次。"""
    quote = parse_quote(QUOTE_TEXT)
    assert quote.amount == pytest.approx(313591.00 * 1e4)


def test_行情时间格式化():
    assert parse_quote(QUOTE_TEXT).as_of == "2026-10-07 15:00:03"


def test_行情字段不足时报错():
    """停牌 / 不存在的代码，腾讯会返回字段残缺的空壳，不能让 0 冒充行情。"""
    with pytest.raises(DataSourceError):
        parse_quote('v_sh600519="1~贵州茅台~600519";')


def test_行情渲染带来源():
    text = parse_quote(QUOTE_TEXT).render()
    assert "贵州茅台" in text
    assert "31.36 亿" in text  # 万元 → 元 → 亿元的渲染链路


# ---------------------------------------------------------------------- K 线

# 行是「日期,开,收,高,低,量」6 列 —— 顺序与东财相同，但**没有**成交额/涨跌幅列。
KLINE_PAYLOAD = {
    "code": 0,
    "data": {
        "sh600519": {
            "qfqday": [
                ["2026-09-18", "1262.99", "1257.12", "1265.88", "1256.10", "24891.00"],
                ["2026-09-21", "1259.00", "1252.57", "1259.95", "1250.80", "25017.00"],
            ],
            "qt": {"sh600519": ["1", "贵州茅台", "600519"] + ["0"] * 46},
            "mx_price": [],
        }
    },
}


def test_k线按位置解析():
    series = parse_kline(KLINE_PAYLOAD, code="sh600519", period="daily", adjust="forward")
    assert series.symbol == "600519"
    assert series.name == "贵州茅台"
    assert len(series.bars) == 2

    bar = series.bars[1]
    assert bar.date == "2026-09-21"
    assert bar.open == pytest.approx(1259.00)
    assert bar.close == pytest.approx(1252.57)
    assert bar.high == pytest.approx(1259.95)
    assert bar.low == pytest.approx(1250.80)
    assert bar.volume == pytest.approx(25017)
    assert bar.amount is None  # 腾讯 K 线没有成交额列


def test_k线涨跌幅由前收盘补算_首根为None():
    """腾讯不返回涨跌幅，用相邻收盘价补算；首根没有参照，保持 None 不硬造。"""
    bars = parse_kline(
        KLINE_PAYLOAD, code="sh600519", period="daily", adjust="forward"
    ).bars
    assert bars[0].change_pct is None
    assert bars[1].change_pct == pytest.approx(-0.36)


def test_k线坏行被跳过():
    payload = {
        "data": {"sh600519": {"qfqday": ["垃圾行", ["2026-09-21", "1", "2", "3", "4", "5"]]}},
    }
    series = parse_kline(payload, code="sh600519", period="daily", adjust="forward")
    assert len(series.bars) == 1


# ---------------------------------------------------------------------- 代码


def test_代码转换():
    assert to_market_code("600519") == "sh600519"
    assert to_market_code("000001") == "sz000001"
    assert to_market_code("SH600519") == "sh600519"
    assert to_market_code("sz000001") == "sz000001"
    with pytest.raises(DataSourceError):
        to_market_code("茅台")  # 名称解析只有东财有，兜底源显式拒绝
    with pytest.raises(DataSourceError):
        to_market_code("")


# ---------------------------------------------------------------------- 重试


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_断连会自动重试():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        body = QUOTE_TEXT.encode("gbk")  # 行情接口是 GBK 编码
        return httpx.Response(200, content=body)

    source = TencentDataSource(client=_client(handler), attempts=3)
    quote = await source.quote("600519")
    assert quote.name == "贵州茅台"
    assert len(calls) == 3


async def test_GBK编码的行情能正确解码():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=QUOTE_TEXT.encode("gbk"))

    source = TencentDataSource(client=_client(handler), attempts=1)
    assert (await source.quote("600519")).name == "贵州茅台"


async def test_空壳行情给可读报错():
    """代码不存在时腾讯返回字段残缺的空壳，报错要能看懂。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'v_sh000000="1~~~~~";')

    source = TencentDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError) as exc:
        await source.quote("000000")
    assert "000000" in str(exc.value)


async def test_k线请求失败给可读报错():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    source = TencentDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError) as exc:
        await source.kline("600519", limit=10)
    assert "K 线" in str(exc.value)
