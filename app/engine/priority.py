"""市场 / 行业 / 个股三层对比与研究优先级路由。

这一层只回答一个问题：**Agent 应该先去调查什么**。

规划文档反复强调的约束在这里体现为代码注释与对外声明：三层对比的差值
不是因果贡献，不能读成"市场导致 -1%、行业导致 -6%、公司导致 -2%"。
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.contracts import (
    PRIORITY_LABELS,
    DataGap,
    ResearchPriority,
)
from app.engine import presenter
from app.schemas import LayerComparison

DISCLAIMER = (
    "三层对比仅用于确定研究优先级，不代表各层对本次价格变化的因果贡献；"
    "不能读作「市场贡献 X%、行业贡献 Y%、公司贡献 Z%」。"
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
) -> LayerComparison:
    caliber = f"{window_label}区间累计涨跌"

    industry_vs_market = (
        industry_pct - market_pct
        if industry_pct is not None and market_pct is not None
        else None
    )
    stock_vs_industry = (
        stock_pct - industry_pct
        if stock_pct is not None and industry_pct is not None
        else None
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
            "行业相对市场",
            industry_vs_market,
            presenter.pct_points(industry_vs_market),
            unit=presenter.UNIT_PCT_POINTS,
            caliber="行业区间涨跌 - 市场区间涨跌",
        ),
        stock_vs_industry=presenter.measure(
            "stock_vs_industry",
            "个股相对行业",
            stock_vs_industry,
            presenter.pct_points(stock_vs_industry),
            unit=presenter.UNIT_PCT_POINTS,
            caliber="个股区间涨跌 - 行业区间涨跌",
        ),
        disclaimer=DISCLAIMER,
    )


def decide_priority(
    *,
    stock_pct: Optional[float],
    industry_pct: Optional[float],
    market_pct: Optional[float],
    divergence_threshold: float,
    industry_move_threshold: float,
    market_move_threshold: float,
) -> Tuple[ResearchPriority, str, Optional[DataGap]]:
    """根据三层表现决定第二阶段的调查顺序。"""

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

    industry_vs_market = industry_pct - market_pct
    stock_vs_industry = stock_pct - industry_pct

    industry_moved = abs(industry_vs_market) >= industry_move_threshold
    stock_diverged = abs(stock_vs_industry) >= divergence_threshold
    market_moved = abs(market_pct) >= market_move_threshold

    fmt = presenter.pct
    fmt_pts = presenter.pct_points
    facts = (
        f"市场 {fmt(market_pct)}、行业 {fmt(industry_pct)}、个股 {fmt(stock_pct)}；"
        f"行业相对市场 {fmt_pts(industry_vs_market)}，"
        f"个股相对行业 {fmt_pts(stock_vs_industry)}。"
    )

    # 行业明显变化，同时个股进一步明显偏离 —— 两条线并行调查
    if industry_moved and stock_diverged:
        return (
            ResearchPriority.INDUSTRY_PLUS_COMPANY,
            facts + "行业整体出现明显变化，个股在此基础上进一步偏离行业，"
            "因此需要同时调查行业因素和公司特有因素。",
            None,
        )

    # 个股明显背离行业和市场 —— 优先查公司
    if stock_diverged:
        return (
            ResearchPriority.COMPANY_FIRST,
            facts + "行业与市场表现接近，个股明显偏离行业，"
            "优先调查公司特有因素，其次是交易与关注度因素。",
            None,
        )

    # 行业明显变化但大盘稳定 —— 优先查行业
    if industry_moved and not market_moved:
        return (
            ResearchPriority.INDUSTRY_FIRST,
            facts + "大盘相对稳定而行业整体出现明显变化，"
            "优先调查行业层面因素，再看公司，最后看市场。",
            None,
        )

    # 其余情况视为三层高度同步 —— 从市场 / 宏观查起
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
    }[priority]
