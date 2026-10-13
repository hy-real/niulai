"""数据源抽象。

核心约束：工具不认识具体数据源。工具只调 MarketDataService，Service 按
capability 挑源 —— "换数据源"= 加一个实现类 + 改一行配置，而不是动工具
代码。

字段单位约定（贯穿全项目，任何一层不做私自换算）：价格 = 元，成交量 = 手，
成交额 = 元，比率 = 百分数（-0.36 表示 -0.36%）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from ..errors import DataSourceError

# 一个数据源可以声明的能力，Service 按 这里 的值筛源：
#   quote       实时行情快照（最新价 / 涨跌 / 今开 / 最高 / 最低 / 成交量额）
#   kline       历史 K 线（日 / 周 / 月，支持前复权 / 后复权）
#   financial   定期报告主要财务指标（营收 / 归母净利 / ROE / 毛利率等）
#   moneyflow   个股资金流（主力 / 超大单 / 大单 / 中单 / 小单 每日净流入）
#   valuation   估值快照（PE TTM/MRQ、PB、PS、PCF）
#   hotlist     同花顺热榜（热股榜 / 飙升榜）—— 市场关注度
#   limitup     涨跌停 / 炸板股票池与连板天梯 —— 盘面情绪
#   dragontiger 龙虎榜（全部 / 机构榜 / 游资榜）
#   anomaly     个股异动原因（当日）
#   auction     集合竞价（个股竞价快照 / 短线风向标竞价基准）—— 开盘技术信号
#   index_quote 指数 / 板块行情快照（大盘与行业参照系）
#   index_kline 指数 / 板块历史 K 线（个股 vs 板块的走势对比）
Capability = Literal[
    "quote", "kline", "financial", "moneyflow",
    "valuation", "hotlist", "limitup", "dragontiger", "anomaly",
    "auction", "index_quote", "index_kline",
]


# ---------------------------------------------------------------- 格式化辅助


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _yi(value: float | None) -> str:
    """元 → 亿元。金融数据里"亿"比"元"好读，也短得多。"""
    return "—" if value is None else f"{value / 1e8:,.2f} 亿"


@dataclass(slots=True)
class Quote:
    """单只标的的行情快照。"""

    symbol: str  # 规整后的 6 位代码，如 600519
    name: str
    price: float
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    change: float | None = None
    change_pct: float | None = None
    volume: float | None = None
    amount: float | None = None
    source: str = ""
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """转成给**模型**看的紧凑文本。

        注意这不是最终给用户的文案 —— 模型会拿它再组织语言。
        所以这里追求"信息完整、无歧义"，不追求好看。
        """
        change_text = "—" if self.change is None else f"{self.change:+.2f}"

        lines = [
            f"{self.name}（{self.symbol}）",
            f"最新价：{_money(self.price)}",
            f"涨跌：{change_text}（{_pct(self.change_pct)}）",
            "今开 {o} / 最高 {h} / 最低 {l} / 昨收 {p}".format(
                o=_money(self.open),
                h=_money(self.high),
                l=_money(self.low),
                p=_money(self.prev_close),
            ),
        ]
        if self.volume is not None:
            lines.append(f"成交量：{self.volume:,.0f} 手")
        if self.amount is not None:
            lines.append(f"成交额：{self.amount / 1e8:,.2f} 亿元")

        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


@dataclass(slots=True)
class KlineBar:
    """一根 K 线。"""

    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float | None = None  # 手
    amount: float | None = None  # 元
    change_pct: float | None = None  # %
    turnover: float | None = None  # 换手率 %

    def to_row(self) -> str:
        """一行 CSV。比 JSON 省一半以上 token —— 60 根 K 线的差别很可观。"""
        return (
            f"{self.date},{self.open:.2f},{self.close:.2f},{self.high:.2f},"
            f"{self.low:.2f},{self.volume if self.volume is not None else ''},"
            f"{_pct_raw(self.change_pct)}"
        )


@dataclass(slots=True)
class KlineSeries:
    symbol: str
    name: str
    period: str  # daily / weekly / monthly
    adjust: str  # none / forward / backward
    bars: list[KlineBar]
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.bars:
            return f"{self.name}（{self.symbol}）没有取到 K 线数据。"

        first, last = self.bars[0], self.bars[-1]
        lines = [
            f"{self.name}（{self.symbol}）{PERIOD_LABEL.get(self.period, self.period)} K 线",
            f"区间：{first.date} → {last.date}　共 {len(self.bars)} 根　复权："
            f"{ADJUST_LABEL.get(self.adjust, self.adjust)}",
            f"字段：日期,开,收,高,低,成交量(手),涨跌幅(%)",
        ]
        lines.extend(bar.to_row() for bar in self.bars)
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


PERIOD_LABEL = {"daily": "日", "weekly": "周", "monthly": "月"}
ADJUST_LABEL = {"none": "不复权", "forward": "前复权", "backward": "后复权"}


@dataclass(slots=True)
class FinancialPeriod:
    """一期主要财务指标。缺失字段一律 None，不要用 0 冒充缺失。"""

    period: str  # 如「2026中报」
    report_date: str
    revenue: float | None = None  # 营业总收入（元）
    revenue_yoy: float | None = None  # %
    net_profit: float | None = None  # 归母净利润（元）
    net_profit_yoy: float | None = None  # %
    deduct_net_profit: float | None = None  # 扣非归母净利润（元）
    eps: float | None = None  # 基本每股收益（元）
    bps: float | None = None  # 每股净资产（元）
    roe: float | None = None  # 加权 ROE（%）
    gross_margin: float | None = None  # 销售毛利率（%）
    net_margin: float | None = None  # 销售净利率（%）
    debt_ratio: float | None = None  # 资产负债率（%）
    ocf_per_share: float | None = None  # 每股经营现金流（元）


@dataclass(slots=True)
class FinancialSummary:
    symbol: str
    name: str
    periods: list[FinancialPeriod]
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.periods:
            return f"{self.name}（{self.symbol}）没有取到财务数据。"

        lines = [f"{self.name}（{self.symbol}）主要财务指标"]
        for p in self.periods:
            lines.append("")
            lines.append(f"【{p.period}】报告期 {p.report_date[:10]}")
            lines.append(
                f"营业总收入：{_yi(p.revenue)}（同比 {_pct(p.revenue_yoy)}）　"
                f"归母净利润：{_yi(p.net_profit)}（同比 {_pct(p.net_profit_yoy)}）"
            )
            if p.deduct_net_profit is not None:
                lines.append(f"扣非归母净利润：{_yi(p.deduct_net_profit)}")
            lines.append(
                f"每股收益：{_money(p.eps)} 元　每股净资产：{_money(p.bps)} 元　"
                f"加权 ROE：{_money(p.roe)}%"
            )
            lines.append(
                f"毛利率：{_money(p.gross_margin)}%　净利率：{_money(p.net_margin)}%　"
                f"资产负债率：{_money(p.debt_ratio)}%"
            )
        lines.append("")
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        lines.append("注：以上为定期报告口径的累计值，同比为与上年同期对比。")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 估值


@dataclass(slots=True)
class ValuationRow:
    """一只股票的估值快照。负值原样保留 —— 亏损股 PE 为负是事实，不是错误。"""

    code: str
    name: str
    pe_ttm: float | None = None
    pe_mrq: float | None = None
    pb_mrq: float | None = None
    ps_ttm: float | None = None
    pcf_ttm: float | None = None


@dataclass(slots=True)
class ValuationList:
    rows: list[ValuationRow]
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.rows:
            return "没有取到估值数据。"
        lines = [
            "估值快照",
            "字段：名称(代码), PE(TTM), PE(MRQ), PB(MRQ), PS(TTM), PCF(TTM)",
        ]
        for r in self.rows:
            lines.append(
                f"{r.name}({r.code}), {_money(r.pe_ttm)}, {_money(r.pe_mrq)}, "
                f"{_money(r.pb_mrq)}, {_money(r.ps_ttm)}, {_money(r.pcf_ttm)}"
            )
        lines.append(
            "注：PE 为负表示公司亏损（TTM 口径），不代表估值便宜；"
            "银行 / 地产等重资产行业看 PB，高成长公司看 PS。"
        )
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 热榜


@dataclass(slots=True)
class HotRow:
    """热榜一行。热度值保留上游原始字符串（数值很大），排名变化正升负降。"""

    code: str
    name: str
    rank: int
    heat: str = ""
    rank_change: int | None = None
    trend: str = ""  # up / down / flat / unknown


@dataclass(slots=True)
class HotList:
    board: str  # hot / skyrocket
    period: str  # day / hour
    rows: list[HotRow]
    source: str = ""
    as_of: str = ""

    BOARD_LABEL = {"hot": "热股榜", "skyrocket": "飙升榜"}

    def render(self) -> str:
        if not self.rows:
            return f"{self.BOARD_LABEL.get(self.board, self.board)}没有取到数据。"
        lines = [
            f"同花顺{self.BOARD_LABEL.get(self.board, self.board)}"
            f"（{'24小时' if self.period == 'day' else '小时'}级，共 {len(self.rows)} 只）",
            "字段：排名, 名称(代码), 热度, 排名变化, 趋势",
        ]
        for r in self.rows:
            change = "—" if r.rank_change is None else f"{r.rank_change:+d}"
            lines.append(f"{r.rank}, {r.name}({r.code}), {r.heat}, {change}, {r.trend}")
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 资金流


@dataclass(slots=True)
class MoneyflowDay:
    """一个交易日的资金流向。金额一律**元**；主力 = 超大单 + 大单（东财口径）。"""

    date: str
    main_net: float | None = None  # 主力净流入（元）
    super_net: float | None = None  # 超大单净流入（元）
    big_net: float | None = None  # 大单净流入（元）
    mid_net: float | None = None  # 中单净流入（元）
    small_net: float | None = None  # 小单净流入（元）
    close: float | None = None  # 收盘价（元）
    change_pct: float | None = None  # 涨跌幅 %
    turnover: float | None = None  # 换手率 %


@dataclass(slots=True)
class MoneyflowSeries:
    symbol: str
    name: str
    days: list[MoneyflowDay]
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.days:
            return f"{self.name}（{self.symbol}）没有取到资金流数据。"

        lines = [
            f"{self.name}（{self.symbol}）个股资金流（最近 {len(self.days)} 个交易日）",
            "字段：日期, 收盘, 涨跌幅(%), 主力净流入(亿), 超大单(亿), 大单(亿), 中单(亿), 小单(亿)",
        ]
        for d in self.days:
            row = [
                d.date,
                _money(d.close),
                _pct(d.change_pct),
                _yi(d.main_net),
                _yi(d.super_net),
                _yi(d.big_net),
                _yi(d.mid_net),
                _yi(d.small_net),
            ]
            lines.append(", ".join(row))
        lines.append("注：主力 = 超大单 + 大单；正值净流入，负值净流出。")
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 涨跌停池


@dataclass(slots=True)
class LimitPoolRow:
    """涨跌停 / 炸板池一行。三种池字段富余不同，缺什么就 None / 空串。"""

    code: str
    name: str
    price: float | None = None
    change_pct: float | None = None
    time_text: str = ""  # 涨停时间 / 最后跌停时间（HH:MM）
    continue_text: str = ""  # 连板文本，如「首板」「5天4板」
    continue_cnt: int | None = None
    reason: str = ""  # 涨停原因
    seal_money: float | None = None  # 封单额（元）
    open_times: int | None = None  # 炸板开板次数
    turnover_pct: float | None = None  # 换手率 %


@dataclass(slots=True)
class LimitPool:
    kind: str  # up / down / break
    total: int | None = None
    rows: list[LimitPoolRow] = field(default_factory=list)
    source: str = ""
    as_of: str = ""

    KIND_LABEL = {"up": "涨停池", "down": "跌停池", "break": "炸板池"}

    def render(self) -> str:
        if not self.rows:
            return f"{self.KIND_LABEL.get(self.kind, self.kind)}没有取到数据。"
        total = f"，共 {self.total} 只" if self.total is not None else ""
        lines = [
            f"{self.KIND_LABEL.get(self.kind, self.kind)}（{total}）",
            "字段：名称(代码), 价格, 涨跌幅(%), 时间, 连板, 原因, 封单(亿), 换手(%)",
        ]
        for r in self.rows:
            seal = "—" if r.seal_money is None else f"{r.seal_money / 1e8:.2f}"
            turnover = _pct_raw(r.turnover_pct) or "—"
            lines.append(
                f"{r.name}({r.code}), {_money(r.price)}, {_pct(r.change_pct)}, "
                f"{r.time_text or '—'}, {r.continue_text or '—'}, {r.reason or '—'}, "
                f"{seal}, {turnover}"
            )
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


@dataclass(slots=True)
class LadderDay:
    """连板天梯的一个交易日。boards 键：two_board … seven_over。"""

    date: str
    boards: dict[str, list[str]]  # 板位 → 「名称(代码)」列表


@dataclass(slots=True)
class LimitLadder:
    days: list[LadderDay]
    source: str = ""
    as_of: str = ""

    BOARD_LABEL = {
        "two_board": "2板",
        "three_board": "3板",
        "four_board": "4板",
        "five_board": "5板",
        "six_board": "6板",
        "seven_over": "7板+",
    }

    def render(self) -> str:
        if not self.days:
            return "连板天梯没有取到数据。"
        labels = list(self.BOARD_LABEL)
        lines = ["连板天梯（最新在前；每板位最多展示 4 只）"]
        for day in self.days:
            cells = []
            for key in labels:
                names = day.boards.get(key) or []
                text = "、".join(names) if names else "—"
                cells.append(f"{self.BOARD_LABEL[key]}: {text}")
            lines.append(f"{day.date}　" + "　".join(cells))
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 龙虎榜


@dataclass(slots=True)
class DragonTigerRow:
    """龙虎榜股票维度一行。金额一律元；涨跌幅由上游小数形式换算为百分数。"""

    code: str
    name: str
    change_pct: float | None = None
    net_value: float | None = None  # 龙虎榜净买入（元）
    buy_value: float | None = None
    sell_value: float | None = None
    reason: str = ""  # 涨跌停原因
    range_days: int | None = None  # 1 当日榜 / 3 三日榜
    org_net_value: float | None = None  # 机构净买入（元）
    org_buy_num: int | None = None
    org_sell_num: int | None = None


@dataclass(slots=True)
class DragonTigerBoard:
    board_type: str  # all / org / hot_money
    trade_date: str = ""
    stock_count: int | None = None
    rows: list[DragonTigerRow] = field(default_factory=list)
    hot_money: list[tuple[str, float]] = field(default_factory=list)  # 游资名, 净买入(元)
    source: str = ""
    as_of: str = ""

    TYPE_LABEL = {"all": "全部", "org": "机构榜", "hot_money": "游资榜"}

    def render(self) -> str:
        label = self.TYPE_LABEL.get(self.board_type, self.board_type)
        count = self.stock_count if self.stock_count is not None else len(self.rows)
        lines = [f"龙虎榜 · {label}（{self.trade_date}，{count} 只股票）"]
        if self.hot_money:
            lines.append("字段：游资, 聚合净买入(亿)")
            for name, value in self.hot_money:
                lines.append(f"{name}, {value / 1e8:,.2f}")
        else:
            lines.append(
                "字段：名称(代码), 涨跌幅(%), 净买入(亿), 上榜区间, 机构(净买入亿, 买/卖家数), 原因"
            )
            for r in self.rows:
                if r.org_net_value is None:
                    org = "—"
                else:
                    org = (
                        f"{r.org_net_value / 1e8:+.2f}亿"
                        f"({r.org_buy_num or 0}买/{r.org_sell_num or 0}卖)"
                    )
                lines.append(
                    f"{r.name}({r.code}), {_pct(r.change_pct)}, {_yi(r.net_value)}, "
                    f"{r.range_days or '—'}日榜, {org}, {r.reason or '—'}"
                )
        lines.append("注：净买入为正表示买方金额大于卖方；3 日榜为近三个交易日累计口径。")
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 异动原因


@dataclass(slots=True)
class AnomalyRow:
    """一条异动解读。content 是上游生成的解读文本，给模型做参考。"""

    code: str
    name: str
    tag: str = ""  # 大涨 / 涨停 / 快速拉升 …
    content: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnomalyList:
    rows: list[AnomalyRow]
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.rows:
            return "这些标的今日没有异动记录。"
        lines = [f"当日异动原因（{len(self.rows)} 条）"]
        for r in self.rows:
            keywords = f"　关键词：{'、'.join(r.keywords)}" if r.keywords else ""
            lines.append(f"◆ {r.name}（{r.code}）[{r.tag}]{keywords}")
            lines.append(f"  {r.content}")
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


# ---------------------------------------------------------------------- 集合竞价


@dataclass(slots=True)
class AuctionRow:
    """一只标的的集合竞价快照。竞价量比 / 昨量比是开盘技术信号的核心。"""

    code: str
    name: str
    auction_price: float | None = None
    auction_pct: float | None = None  # 竞价涨跌幅 %
    auction_volume: float | None = None  # 手
    auction_amount: float | None = None  # 元
    unmatched: float | None = None  # 未匹配量（手）
    turnover_pct: float | None = None  # 竞价换手率 %
    yesterday_ratio_pct: float | None = None  # 竞价量 / 昨日总量的 %
    volume_ratio: float | None = None  # 竞价量比
    pre_close: float | None = None
    open_price: float | None = None


@dataclass(slots=True)
class AuctionSnapshot:
    phase: str = ""  # 集合竞价阶段（final / live …）
    rows: list[AuctionRow] = field(default_factory=list)
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.rows:
            return "没有取到集合竞价数据。"
        phase = f"，阶段：{self.phase}" if self.phase else ""
        lines = [
            f"集合竞价快照（共 {len(self.rows)} 只{phase}）",
            "字段：名称(代码), 竞价价, 竞价涨幅(%), 竞价量比, 竞价量/昨日量(%), "
            "竞价额(亿), 未匹配(手), 开盘, 前收",
        ]
        for r in self.rows:
            amount = "—" if r.auction_amount is None else f"{r.auction_amount / 1e8:.2f}"
            lines.append(
                f"{r.name}({r.code}), {_money(r.auction_price)}, {_pct(r.auction_pct)}, "
                f"{_money(r.volume_ratio)}, {_money(r.yesterday_ratio_pct)}, "
                f"{amount}, "
                f"{'—' if r.unmatched is None else f'{r.unmatched:,.0f}'}, "
                f"{_money(r.open_price)}, {_money(r.pre_close)}"
            )
        lines.append(
            "注：竞价量比 > 1 表示竞价成交量高于近期水平；高开 + 放量竞价是偏强信号，"
            "低开缩量偏弱。竞价数据只在交易日 9:15–9:25 产生，其他时间查到的是当日终态。"
        )
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


@dataclass(slots=True)
class BenchmarkRow:
    """短线风向标一行。tags 是数据方给的竞价标签（高开 / 放量 …）。"""

    code: str
    name: str
    auction_pct: float | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AuctionBenchmark:
    date: str = ""
    rows: list[BenchmarkRow] = field(default_factory=list)
    source: str = ""
    as_of: str = ""

    def render(self) -> str:
        if not self.rows:
            return f"{self.date or '当日'}没有取到短线风向标竞价数据（非交易日或接口无数据）。"
        lines = [
            f"短线风向标竞价基准（{self.date}，共 {len(self.rows)} 只）",
            "字段：名称(代码), 竞价涨幅(%), 标签",
        ]
        for r in self.rows:
            tags = "、".join(r.tags) if r.tags else "—"
            lines.append(f"{r.name}({r.code}), {_pct(r.auction_pct)}, {tags}")
        lines.append(
            "注：风向标是市场短线情绪的参照股集合（多为前期涨停 / 人气股），"
            "它们的集体竞价表现常被用来判断当日情绪强弱：整体高开放量 = 情绪偏强。"
        )
        lines.append(f"数据源：{self.source}　数据时间：{self.as_of}")
        return "\n".join(lines)


@runtime_checkable
class MarketDataSource(Protocol):
    """一个数据源要提供的东西：身份、能力、以及各能力的取数方法。

    只实现自己声明了的能力对应的方法即可 —— Service 只会调它声明过的那些。
    """

    id: str
    capabilities: set[Capability]

    # 各能力对应的取数方法与实现由具体源提供（如 EastmoneyDataSource），
    # 方法名与 Capability 同名：quote() / kline() / financial()，Service 靠 getattr 反射调用
    async def quote(self, symbol: str) -> Quote: ...


def _pct_raw(value: float | None) -> str:
    """CSV 里不用带 + 号，省一个字符。"""
    return "" if value is None else f"{value:.2f}"


# ---------------------------------------------------------------------- 代码转换


def guess_market_prefix(code: str) -> str:
    """按代码首位猜交易所前缀；猜不出来就要求调用方给出 sh / sz 前缀。"""
    if code[0] in "569":
        return "sh"
    if code[0] in "0123":
        return "sz"
    raise DataSourceError(f"无法判断 {code} 属于哪个交易所，请加上 sh / sz 前缀")


def to_market_code(symbol: str) -> str:
    """'600519' / 'sh600519' → 'sh600519'，腾讯源共用。

    名称解析（「茅台」→ 600519）只有东财有；不支持名称的兜底源收到名称时
    在这里显式抛错，由路由层降级到东财。
    """
    raw = symbol.strip()
    if not raw:
        raise DataSourceError("股票代码为空")
    lower = raw.lower()
    if len(lower) > 2 and lower[:2] in ("sh", "sz", "bj") and lower[2:].isdigit():
        return lower
    if raw.isdigit() and len(raw) == 6:
        return guess_market_prefix(raw) + raw
    raise DataSourceError(f"只支持股票代码，不支持名称「{symbol}」")
