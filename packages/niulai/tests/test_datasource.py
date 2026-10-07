"""数据源解析测试：不联网，只验证字段缩放与代码推断。

真实接口调用不放进单元测试 —— 单元测试不该依赖外部服务的可用性。
联网验证走 `python -m niulai ask "茅台现在多少钱"`。
"""

from __future__ import annotations

import httpx
import pytest

from niulai.datasource import EastmoneyDataSource
from niulai.datasource.eastmoney import (
    _guess_market,
    _num,
    _to_secucode,
    parse_financial,
    parse_kline,
    parse_moneyflow,
    parse_quote,
)
from niulai.errors import DataSourceError

# 真实抓取到的一份样本：贵州茅台，小数点后 2 位
SAMPLE = {
    "f43": 125257,  # 最新价
    "f44": 125995,  # 最高
    "f45": 125080,  # 最低
    "f46": 125900,  # 今开
    "f47": 25017,  # 成交量（手）
    "f48": 3135910045.0,  # 成交额（元）
    "f57": "600519",
    "f58": "贵州茅台",
    "f59": 2,  # 小数位
    "f60": 125712,  # 昨收
    "f169": -455,  # 涨跌额
    "f170": -36,  # 涨跌幅
}


def test_价格字段按小数位缩放():
    quote = parse_quote(SAMPLE)
    assert quote.price == pytest.approx(1252.57)
    assert quote.prev_close == pytest.approx(1257.12)
    assert quote.open == pytest.approx(1259.00)
    assert quote.high == pytest.approx(1259.95)
    assert quote.low == pytest.approx(1250.80)


def test_涨跌字段缩放():
    quote = parse_quote(SAMPLE)
    assert quote.change == pytest.approx(-4.55)
    assert quote.change_pct == pytest.approx(-0.36)


def test_成交量与成交额不缩放():
    """这是最容易写错的一处：只有价格类字段需要缩放。"""
    quote = parse_quote(SAMPLE)
    assert quote.volume == pytest.approx(25017)
    assert quote.amount == pytest.approx(3135910045.0)


def test_缺失值用减号表示():
    assert _num("-") is None
    assert _num(None) is None
    assert _num("") is None


def test_按代码首位推断交易所():
    assert _guess_market("600519") == "1"
    assert _guess_market("000001") == "0"
    assert _guess_market("300750") == "0"
    with pytest.raises(DataSourceError):
        _guess_market("888888")


def test_render_包含关键字段且带单位():
    text = parse_quote(SAMPLE).render()
    assert "贵州茅台" in text
    assert "600519" in text
    assert "1252.57" in text
    assert "亿元" in text


# ---------------------------------------------------------------------- K 线

# 真实抓取到的样本。**字段顺序是固定的**，按位置取值而不是按名字猜 ——
# 顺序搞错不会报错，只会让开高低收全部错位，这种错最难发现。
KLINE_SAMPLE = {
    "code": "600519",
    "name": "贵州茅台",
    "klines": [
        "2026-09-18,1262.99,1257.12,1265.88,1256.10,24891,3135849108.00,0.77,-0.78,-9.86,0.20",
        "2026-09-21,1259.00,1252.57,1259.95,1250.80,25017,3135910045.00,0.73,-0.36,-4.55,0.20",
    ],
}


def test_k线按位置解析():
    series = parse_kline(KLINE_SAMPLE, period="daily", adjust="forward")
    assert series.name == "贵州茅台"
    assert len(series.bars) == 2

    bar = series.bars[1]
    assert bar.date == "2026-09-21"
    assert bar.open == pytest.approx(1259.00)
    assert bar.close == pytest.approx(1252.57)
    assert bar.high == pytest.approx(1259.95)
    assert bar.low == pytest.approx(1250.80)
    assert bar.volume == pytest.approx(25017)
    assert bar.amount == pytest.approx(3135910045.0)
    assert bar.change_pct == pytest.approx(-0.36)
    assert bar.turnover == pytest.approx(0.20)


def test_k线保持服务端给的先后顺序():
    """东财从旧到新返回。顺序就是时间语义，不能自己重排。"""
    series = parse_kline(KLINE_SAMPLE, period="daily", adjust="forward")
    assert [b.date for b in series.bars] == ["2026-09-18", "2026-09-21"]


def test_k线字段不足的行被跳过而不是崩掉():
    data = {"code": "x", "name": "x", "klines": ["坏行", "2026-09-21,1,2,3,4,5,6,7,8,9,10"]}
    series = parse_kline(data, period="daily", adjust="forward")
    assert len(series.bars) == 1


def test_k线缺失值不缩放也不炸():
    data = {"code": "x", "name": "x", "klines": ["2026-09-21,1,2,3,4,-,-,-,-,-,-"]}
    bar = parse_kline(data, period="daily", adjust="forward").bars[0]
    assert bar.volume is None
    assert bar.amount is None
    assert bar.change_pct is None


def test_k线渲染写清周期与复权方式():
    """前复权在长期高分红股上会出现负价，所以复权方式必须写在数据里。"""
    text = parse_kline(KLINE_SAMPLE, period="daily", adjust="forward").render()
    assert "日 K 线" in text or "日" in text
    assert "前复权" in text
    assert "2026-09-18 → 2026-09-21" in text
    assert "日期,开,收,高,低" in text


def test_k线为空时给一句人话():
    series = parse_kline({"code": "x", "name": "x"}, period="daily", adjust="forward")
    assert "没有取到" in series.render()


# ---------------------------------------------------------------------- 财务

FINANCE_SAMPLE = [
    {
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "REPORT_DATE": "2026-06-30 00:00:00",
        "REPORT_DATE_NAME": "2026中报",
        "TOTALOPERATEREVE": 92278072083.21,
        "TOTALOPERATEREVETZ": 1.30,
        "PARENTNETPROFIT": 44516880421.86,
        "PARENTNETPROFITTZ": -1.95,
        "EPSJB": 35.57,
        "BPS": 200.99,
        "ROEJQ": 16.75,
        "XSMLL": 89.56,
        "XSJLL": 50.75,
        "ZCFZL": 15.19,
    },
    {
        "SECURITY_CODE": "600519",
        "SECURITY_NAME_ABBR": "贵州茅台",
        "REPORT_DATE": "2026-03-31 00:00:00",
        "REPORT_DATE_NAME": "2026一季报",
        "TOTALOPERATEREVE": 50000000000.0,
        "KCFJCXSYJLR": None,  # 这类缺失在东财很常见（银行 / 保险几乎全 None）
    },
]


def test_财务字段映射():
    summary = parse_financial(FINANCE_SAMPLE)
    assert summary.name == "贵州茅台"
    assert len(summary.periods) == 2

    latest = summary.periods[0]
    assert latest.period == "2026中报"
    assert latest.report_date == "2026-06-30 00:00:00"
    assert latest.revenue == pytest.approx(92278072083.21)
    assert latest.revenue_yoy == pytest.approx(1.30)
    assert latest.net_profit == pytest.approx(44516880421.86)
    assert latest.eps == pytest.approx(35.57)
    assert latest.roe == pytest.approx(16.75)


def test_财务缺失字段保持None而不是零():
    """填 0 会让模型说出"这家公司毛利率 0%"这种荒谬结论。"""
    period = parse_financial(FINANCE_SAMPLE).periods[1]
    assert period.deduct_net_profit is None
    assert period.eps is None
    assert period.roe is None


def test_财务渲染用亿元并标注口径():
    text = parse_financial(FINANCE_SAMPLE).render()
    assert "922.78 亿" in text
    assert "累计值" in text  # 一季报天然小于年报，不说明会被误读成业绩暴跌


def test_财务为空时给一句人话():
    assert "没有取到" in parse_financial([]).render()


# ---------------------------------------------------------------------- 代码转换


def test_secucode_按交易所拼后缀():
    """财务接口要的是 `600519.SH`，而行情接口要的是 `1.600519`，中间要转。"""
    assert _to_secucode("1.600519", "600519") == "600519.SH"
    assert _to_secucode("0.000858", "000858") == "000858.SZ"
    assert _to_secucode("0.300750", "300750") == "300750.SZ"
    assert _to_secucode("0.830799", "830799") == "830799.BJ"
    assert _to_secucode("0.430047", "430047") == "430047.BJ"


# ---------------------------------------------------------------------- 重试

# 东财限流时**直接掐断连接**，不给 HTTP 状态码。实测连发 5 次，
# 前 3 次 200、后 2 次 RemoteProtocolError —— 不重试用户第一次撞上就失败。


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_限流断连会自动重试():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"data": SAMPLE})

    source = EastmoneyDataSource(client=_client(handler), attempts=3)
    quote = await source.quote("600519")

    assert quote.name == "贵州茅台"
    assert len(calls) == 3  # 前两次被掐断，第三次拿到


async def test_非JSON响应也会重试():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(200, text="<html>风控页面</html>")
        return httpx.Response(200, json={"data": SAMPLE})

    source = EastmoneyDataSource(client=_client(handler), attempts=3)
    assert (await source.quote("600519")).price == pytest.approx(1252.57)
    assert len(calls) == 2


async def test_重试用尽后给出可读的报错():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    source = EastmoneyDataSource(client=_client(handler), attempts=2)
    with pytest.raises(DataSourceError) as exc:
        await source.quote("600519")

    message = str(exc.value)
    assert "已重试 2 次" in message
    assert "限流" in message


async def test_4xx不重试():
    """404 / 400 试一百次也是一样的结果，重试只是浪费用户时间。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404, text="not found")

    source = EastmoneyDataSource(client=_client(handler), attempts=3)
    with pytest.raises(DataSourceError):
        await source.quote("600519")
    assert len(calls) == 1


async def test_5xx会重试():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": SAMPLE})

    source = EastmoneyDataSource(client=_client(handler), attempts=3)
    assert (await source.quote("600519")).symbol == "600519"
    assert len(calls) == 2


async def test_搜索接口也走重试():
    """名称搜索是「茅台」能变成 600519 的唯一路径，它更不该脆。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "suggest" in url:
            # 第一次掐断，第二次成功
            if len([c for c in calls if "suggest" in c]) < 2:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(
                200,
                json={"QuotationCodeTable": {"Data": [{"Code": "600519", "MktNum": "1"}]}},
            )
        return httpx.Response(200, json={"data": SAMPLE})

    source = EastmoneyDataSource(client=_client(handler), attempts=3)
    reply = await source.quote("茅台")
    assert reply.symbol == "600519"
    assert len([c for c in calls if "suggest" in c]) == 2  # 搜索重试了一次
    assert len(calls) == 3  # 加上一次行情


async def test_k线请求失败会给可读报错():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    source = EastmoneyDataSource(client=_client(handler), attempts=1)
    with pytest.raises(DataSourceError) as exc:
        await source.kline("600519", limit=10)
    assert "K 线" in str(exc.value)


# ---------------------------------------------------------------------- 资金流


def test_资金流主力等于大单加超大单():
    """东财口径：主力 = 超大单 + 大单。顺序错位不会报错只会让数字错 —— 靠自洽锁住。"""
    data = {
        "code": "600519",
        "name": "贵州茅台",
        "klines": [
            "2026-09-30,533565520.0,-539065.0,-533026464.0,121427904.0,412137616.0,1258.62,1.86",
        ],
    }
    day = parse_moneyflow(data, fallback_symbol="600519").days[0]
    assert day.main_net == pytest.approx(day.big_net + day.super_net)
    assert day.close == pytest.approx(1258.62)
    assert day.change_pct == pytest.approx(1.86)


def test_资金流渲染带口径说明():
    data = {
        "code": "600519",
        "name": "贵州茅台",
        "klines": ["2026-09-30,533565520.0,-539065.0,-533026464.0,121427904.0,412137616.0,1258.62,1.86"],
    }
    text = parse_moneyflow(data, fallback_symbol="600519").render()
    assert "贵州茅台" in text
    assert "主力 = 超大单 + 大单" in text  # 口径不写明，模型会把主力和小单加在一起
    assert "5.34 亿" in text


