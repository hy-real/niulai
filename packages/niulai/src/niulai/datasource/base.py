"""数据源抽象。

核心约束：**工具不认识具体数据源。**

工具只调 `MarketDataService`，Service 按 capability 挑源。
于是"换数据源"= 加一个实现类 + 改一行配置，而不是去动工具代码。

字段单位约定（贯穿全项目，别在某一层偷偷换算）：
价格 = 元，成交量 = 手，成交额 = 元，比率 = 百分数（`-0.36` 表示 -0.36%）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from ..errors import DataSourceError

# 一个数据源可以声明的能力，Service 按 这里 的值筛源：
#   quote      实时行情快照（最新价 / 涨跌 / 今开 / 最高 / 最低 / 成交量额）
#   kline      历史 K 线（日 / 周 / 月，支持前复权 / 后复权）
#   financial  定期报告主要财务指标（营收 / 归母净利 / ROE / 毛利率等）
#   moneyflow  个股资金流（主力 / 超大单 / 大单 / 中单 / 小单 每日净流入）
#   sector     板块行情榜（概念 / 行业，涨跌幅与主力净流入排序）
#   sector_stocks  板块成分股列表（板块内逐股行情 + 估值）
Capability = Literal["quote", "kline", "financial", "moneyflow", "sector", "sector_stocks"]


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
    """一期主要财务指标。缺失字段一律 None，**不要用 0 冒充缺失**。"""

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


# ---------------------------------------------------------------------- 板块


@dataclass(slots=True)
class SectorRow:
    """一个板块的榜单行。缺什么就 None，两个源的富余程度不同。"""

    code: str  # 如 BK1296 / gn_hwqc
    name: str
    change_pct: float | None = None  # 板块涨跌幅 %
    main_net: float | None = None  # 主力净流入（元），新浪没有
    leader_name: str = ""  # 领涨股名称
    leader_code: str = ""  # 领涨股代码
    leader_change_pct: float | None = None  # 领涨股涨跌幅 %
    stock_count: int | None = None  # 板块内个股总数
    stock_up: int | None = None  # 上涨家数
    stock_down: int | None = None  # 下跌家数


@dataclass(slots=True)
class SectorBoard:
    """板块榜单。**榜的排序由数据源给出**，这里不做二次排序 —— 保持与用户
    在源网站上看到的顺序一致，排序参数的语义由源自己兑现。"""

    kind: str  # concept / industry
    sort: str  # change_pct / moneyflow
    rows: list[SectorRow]
    source: str = ""
    as_of: str = ""

    KIND_LABEL = {"concept": "概念板块", "industry": "行业板块"}
    SORT_LABEL = {"change_pct": "按涨跌幅", "moneyflow": "按主力净流入"}

    def render(self) -> str:
        if not self.rows:
            return f"{self.KIND_LABEL.get(self.kind, self.kind)}没有取到数据。"

        lines = [
            f"{self.KIND_LABEL.get(self.kind, self.kind)}（{self.SORT_LABEL.get(self.sort, self.sort)}排序，"
            f"共 {len(self.rows)} 个）",
            "字段：板块(代码), 涨跌幅(%), 主力净流入(亿), 个股(涨/跌或总数), 领涨股(涨跌幅%)",
        ]
        for r in self.rows:
            count = (
                f"{r.stock_up}/{r.stock_down}"
                if r.stock_up is not None and r.stock_down is not None
                else (str(r.stock_count) if r.stock_count is not None else "—")
            )
            leader = f"{r.leader_name}({_pct(r.leader_change_pct)})" if r.leader_name else "—"
            main = "—" if r.main_net is None else f"{r.main_net / 1e8:.2f}"
            lines.append(
                f"{r.name}({r.code}), {_pct(r.change_pct)}, {main}, {count}, {leader}"
            )
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


# ---------------------------------------------------------------------- 板块成分股


@dataclass(slots=True)
class SectorStockRow:
    """板块内一只股票的快照。估值字段（pe / pb / 市值）新浪成分股接口独有。"""

    code: str  # 6 位代码
    name: str
    price: float | None = None  # 最新价（元）
    change_pct: float | None = None  # 涨跌幅 %
    volume: float | None = None  # 成交量（手）
    amount: float | None = None  # 成交额（元）
    turnover: float | None = None  # 换手率 %
    pe: float | None = None  # 市盈率
    pb: float | None = None  # 市净率
    market_cap: float | None = None  # 总市值（元）
    float_cap: float | None = None  # 流通市值（元）


@dataclass(slots=True)
class SectorStockList:
    node: str  # 板块节点代码，如 gn_hwqc
    name: str  # 板块名称，如 华为汽车
    rows: list[SectorStockRow]
    sort: str = ""  # change_pct / amount / turnover
    source: str = ""
    as_of: str = ""

    SORT_LABEL = {"change_pct": "按涨跌幅", "amount": "按成交额", "turnover": "按换手率"}

    def render(self) -> str:
        if not self.rows:
            return f"板块「{self.name}」（{self.node}）没有取到成分股数据。"

        lines = [
            f"板块「{self.name}」（{self.node}）成分股"
            f"（{self.SORT_LABEL.get(self.sort, self.sort) or '默认排序'}，共 {len(self.rows)} 只）",
            "字段：代码, 名称, 最新价, 涨跌幅(%), 成交额(亿), 换手率(%), 市盈率, 市净率, 总市值(亿)",
        ]
        for r in self.rows:
            cap = "—" if r.market_cap is None else f"{r.market_cap / 1e8:,.1f}"
            amount = "—" if r.amount is None else f"{r.amount / 1e8:.2f}"
            pe = "—" if r.pe is None else f"{r.pe:.1f}"
            pb = "—" if r.pb is None else f"{r.pb:.2f}"
            lines.append(
                f"{r.code}, {r.name}, {_money(r.price)}, {_pct(r.change_pct)}, "
                f"{amount}, {_pct_raw(r.turnover) or '—'}, {pe}, {pb}, {cap}"
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
    """按代码首位猜交易所前缀。猜不出来就让用户写 sh / sz 前缀，不要瞎猜。"""
    if code[0] in "569":
        return "sh"
    if code[0] in "0123":
        return "sz"
    raise DataSourceError(f"无法判断 {code} 属于哪个交易所，请加上 sh / sz 前缀")


def to_market_code(symbol: str) -> str:
    """'600519' / 'sh600519' → 'sh600519'，腾讯 / 新浪共用。

    名称解析（「茅台」→ 600519）只有东财有；不支持名称的兜底源收到名称时
    在这里显式抛错，由路由层降级到东财 —— 不要瞎猜。
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
