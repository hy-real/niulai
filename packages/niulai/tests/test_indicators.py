"""技术指标测试：全部离线，用合成 K 线验证数值方向与信号判定。

指标是确定性代码，所以这里的断言可以很硬 —— 不用容忍模糊。
"""

from __future__ import annotations

from niulai.datasource.base import KlineBar, KlineSeries
from niulai.indicators import _last_cross, compute_indicators, render_indicators
from niulai.tools.indicator import StockIndicatorTool


def _bars(closes: list[float], volumes: list[float] | None = None) -> list[KlineBar]:
    """从收盘价序列合成 K 线：开=昨收，高低各留 1%，量默认 1 万手递增。"""
    bars = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i > 0 else close
        volume = volumes[i] if volumes else 10_000.0 + i * 100
        bars.append(
            KlineBar(
                date=f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
                open=prev,
                close=close,
                high=max(prev, close) * 1.01,
                low=min(prev, close) * 0.99,
                volume=volume,
            )
        )
    return bars


def _series(closes: list[float], volumes: list[float] | None = None) -> KlineSeries:
    return KlineSeries(
        symbol="600519", name="贵州茅台", period="daily", adjust="forward",
        bars=_bars(closes, volumes),
    )


def _rising(n: int = 80) -> KlineSeries:
    return _series([10 * (1 + 0.01 * i) for i in range(n)])


def _falling_then_rising(n_down: int = 35, n_up: int = 20) -> KlineSeries:
    closes = [20 * (1 - 0.005 * i) for i in range(n_down)]
    closes += [closes[-1] * (1 + 0.02 * (i + 1)) for i in range(n_up)]
    return _series(closes)


# ---------------------------------------------------------------------- 单项指标


def test_单边上涨_均线多头排列():
    r = compute_indicators(_rising())
    assert r.ma5 and r.ma10 and r.ma20 and r.ma60
    assert r.ma5 > r.ma10 > r.ma20 > r.ma60
    assert r.ma_layout == "多头排列"
    assert r.macd_dif is not None and r.macd_dif > 0
    assert r.chg_20d is not None and r.chg_20d > 0
    assert r.dist_high_60d is not None and r.dist_high_60d >= 0  # 收在最高附近


def test_单边上涨_MACD金叉出现过():
    r = compute_indicators(_rising())
    assert r.macd_cross is not None
    assert r.macd_cross.kind == "金叉"


def test_下跌后急涨_信号在急涨段转金叉():
    r = compute_indicators(_falling_then_rising())
    # 急涨段足够长，MACD 与 KDJ 至少应出现金叉（或刚走平尚无交叉，但不能是死叉）
    for cross in (r.macd_cross, r.kdj_cross):
        assert cross is None or cross.kind == "金叉"


def test_量能_放量与缩量():
    base = [10_000.0] * 30
    spike = base + [50_000.0]
    r = compute_indicators(_series([10.0] * 31, spike))
    assert r.vol_ratio_5d is not None and r.vol_ratio_5d > 4

    dry = base + [1_000.0]
    r2 = compute_indicators(_series([10.0] * 31, dry))
    assert r2.vol_ratio_5d is not None and r2.vol_ratio_5d < 0.2


def test_成交量缺失_不报错也不给量能结论():
    bars = [
        KlineBar(date=f"d{i}", open=1, close=10 * (1 + 0.01 * i), high=11, low=1, volume=None)
        for i in range(80)
    ]
    series = KlineSeries(symbol="x", name="某股", period="daily", adjust="forward", bars=bars)
    r = compute_indicators(series)
    assert r.vol_ratio_5d is None
    assert "量能" not in render_indicators(series)


def test_完全走平_不炸():
    """价格纹丝不动：RSV 分母为 0、布林带宽为 0，都不该崩。"""
    r = compute_indicators(_series([10.0] * 60))
    assert r.kdj_k == 50.0
    assert r.boll_pct_b is None  # 带宽为 0，%B 无定义，如实缺失


def test_数据不足_如实说而不是硬算():
    r = compute_indicators(_series([10.0, 11.0, 12.0, 13.0, 14.0]))
    assert r.ma5 is None and r.macd_dif is None
    text = render_indicators(_series([10.0, 11.0, 12.0, 13.0, 14.0]))
    assert "数据不足" in text
    assert "调大 limit" in text


# ---------------------------------------------------------------------- 交叉判定


def test_金叉死叉判定_精确到位置():
    dates = ["d0", "d1", "d2", "d3", "d4"]
    fast = [0.0, 0.0, 1.0, 2.0, 3.0]
    slow = [0.5, 0.5, 0.5, 0.5, 0.5]
    cross = _last_cross(fast, slow, dates)
    assert cross is not None
    assert cross.kind == "金叉"
    assert cross.date == "d2"
    assert cross.bars_ago == 2

    fast_down = [3.0, 2.0, 0.0, 0.0, 0.0]
    cross2 = _last_cross(fast_down, slow, dates)
    assert cross2 is not None
    assert cross2.kind == "死叉"
    assert cross2.date == "d2"


def test_无交叉返回None():
    assert _last_cross([1.0, 2.0, 3.0], [0.5, 0.5, 0.5], ["d0", "d1", "d2"]) is None


# ---------------------------------------------------------------------- 工具层


class _FakeService:
    """只实现 kline 的最小 Service 替身。"""

    def __init__(self, series: KlineSeries) -> None:
        self._series = series
        self.last_kwargs: dict | None = None

    async def kline(self, symbol, period="daily", adjust="forward", limit=150):
        self.last_kwargs = {"symbol": symbol, "period": period, "adjust": adjust, "limit": limit}
        return self._series


async def test_工具_默认参数走日线前复权150根():
    service = _FakeService(_rising())
    output = await StockIndicatorTool(service).run({"symbol": "600519"})
    assert service.last_kwargs == {
        "symbol": "600519", "period": "daily", "adjust": "forward", "limit": 150
    }
    assert "技术指标" in output
    assert "MACD" in output
    assert "均线" in output


async def test_工具_参数夹取与字符串容错():
    service = _FakeService(_rising())
    await StockIndicatorTool(service).run({"symbol": "茅台", "limit": "9999"})
    assert service.last_kwargs is not None
    assert service.last_kwargs["limit"] == 250

    await StockIndicatorTool(service).run({"symbol": "茅台", "limit": "abc"})
    assert service.last_kwargs["limit"] == 150


async def test_工具_能力声明与其余行情工具一致():
    capabilities = StockIndicatorTool(_FakeService(_rising())).capabilities
    assert capabilities == frozenset({"read", "network"})
