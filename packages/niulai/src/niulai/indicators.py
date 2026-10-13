"""技术指标：纯 Python 确定性计算，零第三方依赖。

立场是"计算在代码，判断在模型"：让 LLM 从 150 根 K 线里心算 MA60 或 MACD
金叉是会算错的，而且错得很安静。这里用确定性代码把指标数值和交叉信号算好，
模型只负责解读数字。

口径约定（尽量贴国内行情软件的习惯）：
- MACD(12,26,9)：DIF = EMA12 − EMA26，DEA = DIF 的 EMA9，柱 = 2 × (DIF − DEA)；
- KDJ(9,3,3)：通达信式 SMA 递推（K、D 初值 50），J = 3K − 2D；
- RSI：Wilder 平滑（国内软件多用 SMA(1/N) 递推，两者数值接近，方向判断一致）；
- 布林(20,2)：中轨 MA20，标准差为总体口径（ddof=0）；
- 量比 = 今日成交量 / 之前 5 日均量。

输入取自 KlineBar（前复权收盘价是默认口径）。数据不足时返回 None / 空串，
不用 0 冒充缺失 —— 与 datasource 层同一约定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .datasource.base import KlineBar, KlineSeries

# 指标需要的最少 K 线数：KDJ / 布林是 20，MACD 热身约 35。
MIN_BARS = 20


@dataclass(slots=True)
class CrossEvent:
    """一次金叉 / 死叉信号。"""

    kind: str  # 金叉 / 死叉
    date: str
    bars_ago: int  # 距最新一根 K 线过了几根


@dataclass(slots=True)
class IndicatorReport:
    """一只标的的一套指标快照。缺失字段一律 None。"""

    symbol: str
    name: str
    period: str
    adjust: str
    bar_count: int
    last_date: str
    close: float
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma_layout: str = ""  # 多头排列 / 空头排列 / 交织
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_hist: float | None = None  # 已经乘过 2 的柱值
    macd_cross: CrossEvent | None = None
    kdj_k: float = 50.0  # KDJ 递推首值 50，任何窗口都有定义，因此非可选
    kdj_d: float = 50.0
    kdj_j: float = 50.0
    kdj_cross: CrossEvent | None = None
    rsi6: float | None = None
    rsi12: float | None = None
    boll_mid: float | None = None
    boll_upper: float | None = None
    boll_lower: float | None = None
    boll_pct_b: float | None = None
    vol_ratio_5d: float | None = None  # 今日量 / 前 5 日均量
    vol_trend_pct: float | None = None  # 近 5 日均量较前 5 日均量的变化 %
    pv_ratio: float | None = None  # 近 20 日上涨日均量 / 下跌日均量
    streak: str = ""  # 连涨 3 天 / 连跌 2 天 / 交替震荡
    chg_20d: float | None = None  # 近 20 根 K 线涨跌幅 %
    dist_high_60d: float | None = None  # 距 60 根内最高收盘 %（负数=低于高点）
    dist_low_60d: float | None = None  # 距 60 根内最低收盘 %


def compute_indicators(series: KlineSeries) -> IndicatorReport:
    """从一段 K 线序列算出全部指标。数据太少时给出降级结果而不是报错。"""
    bars = series.bars
    closes = [b.close for b in bars]
    report = IndicatorReport(
        symbol=series.symbol,
        name=series.name,
        period=series.period,
        adjust=series.adjust,
        bar_count=len(bars),
        last_date=bars[-1].date if bars else "",
        close=closes[-1] if closes else float("nan"),
    )
    if len(bars) < MIN_BARS:
        return report

    report.ma5 = _sma(closes, 5)
    report.ma10 = _sma(closes, 10)
    report.ma20 = _sma(closes, 20)
    report.ma60 = _sma(closes, 60)
    report.ma_layout = _ma_layout(report)

    dif, dea = _macd_lines(closes)
    if dif and dea:
        report.macd_dif = dif[-1]
        report.macd_dea = dea[-1]
        report.macd_hist = (dif[-1] - dea[-1]) * 2
        report.macd_cross = _last_cross(dif, dea, [b.date for b in bars])

    k, d = _kdj_lines(bars)
    report.kdj_k, report.kdj_d, report.kdj_j = k[-1], d[-1], 3 * k[-1] - 2 * d[-1]
    report.kdj_cross = _last_cross(k, d, [b.date for b in bars])

    report.rsi6 = _rsi(closes, 6)
    report.rsi12 = _rsi(closes, 12)

    report.boll_mid, report.boll_upper, report.boll_lower = _boll(closes, 20, 2)
    if report.boll_upper is not None and report.boll_lower is not None:
        width = report.boll_upper - report.boll_lower
        if width > 0:
            report.boll_pct_b = (closes[-1] - report.boll_lower) / width * 100

    _fill_volume(report, bars)
    _fill_position(report, closes)
    return report


# ---------------------------------------------------------------------- 数值计算


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _ema_series(values: list[float], n: int) -> list[float]:
    """EMA 递推，首值用原始值做种子（与国内软件习惯一致）。"""
    alpha = 2 / (n + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def _macd_lines(closes: list[float]) -> tuple[list[float], list[float]]:
    if len(closes) < 35:  # 26 + 9 的热身都不够时，数值没意义
        return [], []
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26, strict=True)]
    dea = _ema_series(dif, 9)
    return dif, dea


def _kdj_lines(bars: list[KlineBar]) -> tuple[list[float], list[float]]:
    """KDJ(9,3,3)，通达信式递推。窗口不足 9 根时用现有数据（越早越接近中性值）。"""
    k, d = 50.0, 50.0
    ks: list[float] = []
    ds: list[float] = []
    for i, bar in enumerate(bars):
        window = bars[max(0, i - 8) : i + 1]
        low = min(b.low for b in window)
        high = max(b.high for b in window)
        rsv = 50.0 if high == low else (bar.close - low) / (high - low) * 100
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        ks.append(k)
        ds.append(d)
    return ks, ds


def _rsi(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(change, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-change, 0.0)) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def _boll(closes: list[float], n: int, k: float) -> tuple[float | None, float | None, float | None]:
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    variance = sum((v - mid) ** 2 for v in window) / n
    sd = math.sqrt(variance)
    return mid, mid + k * sd, mid - k * sd


def _last_cross(fast: list[float], slow: list[float], dates: list[str]) -> CrossEvent | None:
    """找最近一次快线上穿（金叉）或下穿（死叉）慢线的位置。"""
    last: CrossEvent | None = None
    for i in range(1, min(len(fast), len(slow))):
        prev_diff = fast[i - 1] - slow[i - 1]
        diff = fast[i] - slow[i]
        if prev_diff <= 0 < diff:
            last = CrossEvent("金叉", dates[i], len(fast) - 1 - i)
        elif prev_diff >= 0 > diff:
            last = CrossEvent("死叉", dates[i], len(fast) - 1 - i)
    return last


def _ma_layout(report: IndicatorReport) -> str:
    vals = [report.ma5, report.ma10, report.ma20]
    if any(v is None for v in vals):
        return ""
    if all(a > b for a, b in zip(vals, vals[1:], strict=False)):  # type: ignore[arg-type]
        return "多头排列" if (report.ma60 is None or report.ma20 > report.ma60) else "短期多头排列"
    if all(a < b for a, b in zip(vals, vals[1:], strict=False)):  # type: ignore[arg-type]
        return "空头排列" if (report.ma60 is None or report.ma20 < report.ma60) else "短期空头排列"
    return "均线交织"


def _fill_volume(report: IndicatorReport, bars: list[KlineBar]) -> None:
    volumes = [b.volume for b in bars if b.volume is not None]
    # 成交量缺失的根数过多就不给量能结论，避免拿残数据说"缩量"
    if len(volumes) < 11 or len(volumes) < len(bars) - 2:
        return
    today = volumes[-1]
    prev5 = volumes[-6:-1]
    avg_prev5 = sum(prev5) / 5
    if avg_prev5 > 0:
        report.vol_ratio_5d = today / avg_prev5
    recent5 = sum(volumes[-5:]) / 5
    prior5 = sum(volumes[-10:-5]) / 5
    if prior5 > 0:
        report.vol_trend_pct = (recent5 / prior5 - 1) * 100

    # 近 20 根：上涨日均量 / 下跌日均量（量价配合度）
    window = bars[-20:]
    up: list[float] = []
    down: list[float] = []
    for prev, cur in zip(window, window[1:], strict=False):
        if prev.volume is None or cur.volume is None:
            continue
        (up if cur.close > prev.close else down).append(cur.volume)
    if up and down:
        report.pv_ratio = (sum(up) / len(up)) / (sum(down) / len(down))


def _fill_position(report: IndicatorReport, closes: list[float]) -> None:
    if len(closes) >= 21:
        report.chg_20d = (closes[-1] / closes[-21] - 1) * 100
    if len(closes) >= 60:
        window = closes[-60:]
        high, low = max(window), min(window)
        if high > 0:
            report.dist_high_60d = (closes[-1] / high - 1) * 100
        if low > 0:
            report.dist_low_60d = (closes[-1] / low - 1) * 100
    up_streak = down_streak = 0
    for prev, cur in zip(reversed(closes[1:]), reversed(closes[:-1]), strict=True):
        if cur > prev:
            if down_streak:
                break
            up_streak += 1
        elif cur < prev:
            if up_streak:
                break
            down_streak += 1
        else:
            break
    if up_streak >= 2:
        report.streak = f"连涨 {up_streak} 根"
    elif down_streak >= 2:
        report.streak = f"连跌 {down_streak} 根"
    else:
        report.streak = "交替震荡"


# ---------------------------------------------------------------------- 渲染


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}" if value < 0 else f"{value:.2f}"


def _cross_text(prefix: str, cross: CrossEvent | None) -> str:
    if cross is None:
        return f"{prefix}近期无交叉"
    unit = "根 K 线前"
    return f"{prefix}{cross.kind}（{cross.bars_ago} {unit}，{cross.date}）"


def render_indicators(series: KlineSeries) -> str:
    """把指标快照渲染成给模型看的紧凑文本。"""
    if len(series.bars) < MIN_BARS:
        return (
            f"{series.name}（{series.symbol}）K 线数据不足（{len(series.bars)} 根，"
            f"至少需要 {MIN_BARS} 根），无法计算技术指标。可以调大 limit 后重试。"
        )

    r = compute_indicators(series)
    lines = [
        f"{r.name}（{r.symbol}）技术指标 · {r.period} · {r.adjust} · "
        f"基于 {r.bar_count} 根 K 线（截至 {r.last_date}）",
        f"最新收盘：{r.close:.2f}",
    ]

    ma_parts = [
        f"MA{n} {_num(v)}"
        for n, v in ((5, r.ma5), (10, r.ma10), (20, r.ma20), (60, r.ma60))
    ]
    layout = f"，{r.ma_layout}" if r.ma_layout else ""
    lines.append("均线：" + " / ".join(ma_parts) + layout)

    if r.macd_dif is not None:
        lines.append(
            f"MACD(12,26,9)：DIF {_num(r.macd_dif)} / DEA {_num(r.macd_dea)}"
            f" / 柱 {_num(r.macd_hist)}；{_cross_text('', r.macd_cross)}"
        )
    lines.append(
        f"KDJ(9,3,3)：K {r.kdj_k:.1f} / D {r.kdj_d:.1f} / J {r.kdj_j:.1f}；"
        f"{_cross_text('', r.kdj_cross)}"
    )
    rsi = f"RSI：RSI6 {_num(r.rsi6)} / RSI12 {_num(r.rsi12)}"
    lines.append(rsi)
    if r.boll_mid is not None and r.boll_upper is not None and r.boll_lower is not None:
        boll_text = (
            f"布林(20,2)：上轨 {r.boll_upper:.2f} / 中轨 {r.boll_mid:.2f} / "
            f"下轨 {r.boll_lower:.2f}"
        )
        if r.boll_pct_b is not None:
            boll_text += f"（%B {r.boll_pct_b:.0f}%）"
        lines.append(boll_text)

    if r.vol_ratio_5d is not None:
        volume_text = f"今日量为前 5 日均量的 {r.vol_ratio_5d:.2f} 倍"
        if r.vol_ratio_5d >= 1.5:
            volume_text += "（明显放量）"
        elif r.vol_ratio_5d <= 0.7:
            volume_text += "（明显缩量）"
        if r.vol_trend_pct is not None:
            volume_text += f"；近 5 日均量较前 5 日 {r.vol_trend_pct:+.0f}%"
        if r.pv_ratio is not None:
            volume_text += f"；近 20 日上涨日均量/下跌日均量 = {r.pv_ratio:.2f}"
        lines.append("量能：" + volume_text)

    position_parts = []
    if r.chg_20d is not None:
        position_parts.append(f"近 20 根 {_num(r.chg_20d)}%")
    if r.dist_high_60d is not None:
        position_parts.append(f"距 60 根内最高 {r.dist_high_60d:+.1f}%")
    if r.dist_low_60d is not None:
        position_parts.append(f"距 60 根内最低 {r.dist_low_60d:+.1f}%")
    if r.streak:
        position_parts.append(r.streak)
    if position_parts:
        lines.append("位置：" + "；".join(position_parts))

    lines.append("注：指标为本地确定性计算（收盘价口径），数值是事实；交叉信号只描述发生与否，"
                 "不构成买卖建议，解读请结合行情、资金与估值。")
    return "\n".join(lines)


__all__ = ["CrossEvent", "IndicatorReport", "MIN_BARS", "compute_indicators", "render_indicators"]
