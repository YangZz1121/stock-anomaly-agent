"""市场 / 行业 / 个股三层对比与研究优先级路由。

这一层只回答一个问题：**Agent 应该先去调查什么**。

规划文档反复强调的约束在这里体现为代码注释与对外声明：三层对比的差值
不是因果贡献，不能读成"市场导致 -1%、行业导致 -6%、公司导致 -2%"。

相对比较优先使用 Qlib 风格的 OLS β 残差；估不出 beta 时退回原始相减。
未见显著异动时路由到 ``NO_ANOMALY``，不再进入归因。
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.contracts import (
    PRIORITY_LABELS,
    DataGap,
    ResearchPriority,
)
from app.engine import presenter
from app.engine.anomaly import AnomalyGate
from app.engine.residual import ResidualBreakdown
from app.schemas import LayerComparison

DISCLAIMER = (
    "三层对比仅用于确定研究优先级，不代表各层对本次价格变化的因果贡献；"
    "不能读作「市场贡献 X%、行业贡献 Y%、公司贡献 Z%」。"
    "相对比较优先使用窗口前 OLS β 残差，估不出 beta 时退回原始涨跌相减。"
)


def build_comparison(
    *,
    stock_pct: Optional[float],
    industry_pct: Optional[float],
    market_pct: Optional[float],
    window_label: str,
    stock_evidence_id: Optional[str] = None,
    industry_evidence_id: Optional[str] = None,
    market_evidence_id: Optional[str] = None,
    residual: Optional[ResidualBreakdown] = None,
) -> LayerComparison:
    caliber = f"{window_label}区间累计涨跌"

    raw_industry_vs_market = (
        industry_pct - market_pct
        if industry_pct is not None and market_pct is not None
        else None
    )
    raw_stock_vs_industry = (
        stock_pct - industry_pct
        if stock_pct is not None and industry_pct is not None
        else None
    )

    industry_vs_market = (
        residual.industry_vs_market
        if residual is not None and residual.industry_vs_market is not None
        else raw_industry_vs_market
    )
    stock_vs_industry = (
        residual.stock_vs_industry
        if residual is not None and residual.stock_vs_industry is not None
        else raw_stock_vs_industry
    )
    residual_caliber = (
        "窗口累计涨跌 − β × 基准累计涨跌"
        if residual is not None and residual.method == "ols_beta"
        else "行业区间涨跌 - 市场区间涨跌"
    )
    stock_residual_caliber = (
        "窗口累计涨跌 − β × 行业累计涨跌"
        if residual is not None and residual.method == "ols_beta"
        else "个股区间涨跌 - 行业区间涨跌"
    )

    return LayerComparison(
        stock=presenter.measure(
            "stock_pct",
            "个股",
            stock_pct,
            presenter.pct(stock_pct),
            unit=presenter.UNIT_PCT,
            caliber=caliber,
            evidence_id=stock_evidence_id,
        ),
        industry=presenter.measure(
            "industry_pct",
            "行业指数",
            industry_pct,
            presenter.pct(industry_pct),
            unit=presenter.UNIT_PCT,
            caliber=caliber,
            evidence_id=industry_evidence_id,
        ),
        market=presenter.measure(
            "market_pct",
            "市场宽基指数",
            market_pct,
            presenter.pct(market_pct),
            unit=presenter.UNIT_PCT,
            caliber=caliber,
            evidence_id=market_evidence_id,
        ),
        industry_vs_market=presenter.measure(
            "industry_vs_market",
            "行业相对市场（残差）",
            industry_vs_market,
            presenter.pct_points(industry_vs_market),
            unit=presenter.UNIT_PCT_POINTS,
            caliber=residual_caliber,
        ),
        stock_vs_industry=presenter.measure(
            "stock_vs_industry",
            "个股相对行业（残差）",
            stock_vs_industry,
            presenter.pct_points(stock_vs_industry),
            unit=presenter.UNIT_PCT_POINTS,
            caliber=stock_residual_caliber,
        ),
        industry_vs_market_raw=presenter.measure(
            "industry_vs_market_raw",
            "行业相对市场（原始相减）",
            raw_industry_vs_market,
            presenter.pct_points(raw_industry_vs_market),
            unit=presenter.UNIT_PCT_POINTS,
            caliber="行业区间涨跌 - 市场区间涨跌",
        ),
        stock_vs_industry_raw=presenter.measure(
            "stock_vs_industry_raw",
            "个股相对行业（原始相减）",
            raw_stock_vs_industry,
            presenter.pct_points(raw_stock_vs_industry),
            unit=presenter.UNIT_PCT_POINTS,
            caliber="个股区间涨跌 - 行业区间涨跌",
        ),
        beta_market=presenter.measure(
            "beta_market",
            "个股对市场 β",
            residual.beta_market if residual else None,
            _beta_display(residual.beta_market if residual else None),
            unit=presenter.UNIT_RATIO,
            caliber="窗口前重叠日收益 OLS",
        ),
        beta_industry=presenter.measure(
            "beta_industry",
            "个股对行业 β",
            residual.beta_industry if residual else None,
            _beta_display(residual.beta_industry if residual else None),
            unit=presenter.UNIT_RATIO,
            caliber="窗口前重叠日收益 OLS",
        ),
        residual_note=residual.note if residual else None,
        disclaimer=DISCLAIMER,
    )


def _beta_display(value: Optional[float]) -> str:
    if value is None:
        return presenter.UNAVAILABLE
    return f"{value:.2f}"


def decide_priority(
    *,
    stock_pct: Optional[float],
    industry_pct: Optional[float],
    market_pct: Optional[float],
    divergence_threshold: float,
    industry_move_threshold: float,
    market_move_threshold: float,
    residual: Optional[ResidualBreakdown] = None,
    anomaly: Optional[AnomalyGate] = None,
) -> Tuple[ResearchPriority, str, Optional[DataGap]]:
    """根据三层表现决定第二阶段的调查顺序。"""

    if anomaly is not None and not anomaly.is_anomaly:
        return (
            ResearchPriority.NO_ANOMALY,
            anomaly.note,
            None,
        )

    if stock_pct is None:
        return (
            ResearchPriority.COMPANY_FIRST,
            "缺少个股区间涨跌数据，无法进行三层比较，默认从公司层面开始调查。",
            DataGap(
                field="stock_pct",
                reason="个股区间涨跌不可用",
                impact="研究优先级退化为默认顺序，不具备比较依据",
            ),
        )

    if industry_pct is None or market_pct is None:
        missing = "行业指数" if industry_pct is None else "市场指数"
        return (
            ResearchPriority.COMPANY_FIRST,
            f"{missing}表现不可用，无法判断个股是否与行业 / 市场同步，"
            f"因此先从公司层面开始调查。",
            DataGap(
                field="industry_pct" if industry_pct is None else "market_pct",
                reason=f"{missing}区间涨跌不可用",
                impact="无法判断同步或背离，研究优先级依据不完整",
            ),
        )

    raw_industry_vs_market = industry_pct - market_pct
    raw_stock_vs_industry = stock_pct - industry_pct
    industry_vs_market = (
        residual.industry_vs_market
        if residual is not None and residual.industry_vs_market is not None
        else raw_industry_vs_market
    )
    stock_vs_industry = (
        residual.stock_vs_industry
        if residual is not None and residual.stock_vs_industry is not None
        else raw_stock_vs_industry
    )

    industry_moved = abs(industry_vs_market) >= industry_move_threshold
    stock_diverged = abs(stock_vs_industry) >= divergence_threshold
    market_moved = abs(market_pct) >= market_move_threshold

    fmt = presenter.pct
    fmt_pts = presenter.pct_points
    method = "残差" if residual is not None and residual.method == "ols_beta" else "原始相减"
    facts = (
        f"市场 {fmt(market_pct)}、行业 {fmt(industry_pct)}、个股 {fmt(stock_pct)}；"
        f"行业相对市场（{method}）{fmt_pts(industry_vs_market)}，"
        f"个股相对行业（{method}）{fmt_pts(stock_vs_industry)}。"
    )

    if industry_moved and stock_diverged:
        return (
            ResearchPriority.INDUSTRY_PLUS_COMPANY,
            facts + "行业整体出现明显变化，个股在此基础上进一步偏离行业，"
            "因此需要同时调查行业因素和公司特有因素。",
            None,
        )

    if stock_diverged:
        return (
            ResearchPriority.COMPANY_FIRST,
            facts + "行业与市场表现接近，个股明显偏离行业，"
            "优先调查公司特有因素，其次是交易与关注度因素。",
            None,
        )

    if industry_moved and not market_moved:
        return (
            ResearchPriority.INDUSTRY_FIRST,
            facts + "大盘相对稳定而行业整体出现明显变化，"
            "优先调查行业层面因素，再看公司，最后看市场。",
            None,
        )

    return (
        ResearchPriority.MARKET_FIRST,
        facts + "市场、行业与个股变化方向和幅度较为接近，"
        "优先调查市场与宏观层面因素，再向行业和公司收敛。",
        None,
    )


def priority_label(priority: ResearchPriority) -> str:
    return PRIORITY_LABELS[priority.value]


def scope_order(priority: ResearchPriority):
    """把优先级翻译成实际的检索顺序。"""
    return {
        ResearchPriority.MARKET_FIRST: ["market", "industry", "company"],
        ResearchPriority.INDUSTRY_FIRST: ["industry", "company", "market"],
        ResearchPriority.COMPANY_FIRST: ["company", "industry", "market"],
        ResearchPriority.INDUSTRY_PLUS_COMPANY: ["industry", "company", "market"],
        ResearchPriority.NO_ANOMALY: [],
    }[priority]
