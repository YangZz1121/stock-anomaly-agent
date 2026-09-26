"""研究优先级路由的校验。

优先级只决定"先查什么"，因此这里同时确认：它绝不被表述成因果贡献。
"""

from __future__ import annotations

from app.contracts import ResearchPriority
from app.engine.priority import DISCLAIMER, build_comparison, decide_priority, scope_order

THRESHOLDS = dict(
    divergence_threshold=0.02,
    industry_move_threshold=0.02,
    market_move_threshold=0.01,
)


def decide(stock, industry, market):
    return decide_priority(
        stock_pct=stock, industry_pct=industry, market_pct=market, **THRESHOLDS
    )


def test_synchronized_layers_route_to_market_first():
    priority, reason, gap = decide(-0.021, -0.020, -0.018)
    assert priority == ResearchPriority.MARKET_FIRST
    assert gap is None
    assert "市场" in reason


def test_industry_moves_while_market_is_stable_routes_to_industry():
    priority, reason, _ = decide(-0.045, -0.040, -0.003)
    assert priority == ResearchPriority.INDUSTRY_FIRST


def test_stock_diverging_from_stable_industry_routes_to_company():
    priority, reason, _ = decide(-0.081, -0.006, -0.003)
    assert priority == ResearchPriority.COMPANY_FIRST
    assert "公司特有因素" in reason


def test_industry_move_plus_further_divergence_routes_to_both():
    # 行业跑输市场 6pct，个股再跑输行业 2.4pct
    priority, reason, _ = decide(-0.096, -0.072, -0.012)
    assert priority == ResearchPriority.INDUSTRY_PLUS_COMPANY
    assert "同时调查" in reason


def test_missing_industry_data_degrades_with_explicit_gap():
    priority, reason, gap = decide(-0.05, None, -0.01)
    assert priority == ResearchPriority.COMPANY_FIRST
    assert gap is not None and gap.field == "industry_pct"
    assert "不可用" in reason


def test_missing_stock_data_reports_gap():
    _, _, gap = decide(None, -0.02, -0.01)
    assert gap is not None and gap.field == "stock_pct"


def test_scope_order_follows_priority():
    assert scope_order(ResearchPriority.MARKET_FIRST)[0] == "market"
    assert scope_order(ResearchPriority.INDUSTRY_FIRST)[0] == "industry"
    assert scope_order(ResearchPriority.COMPANY_FIRST)[0] == "company"
    assert scope_order(ResearchPriority.NO_ANOMALY) == []


def test_comparison_computes_relative_gaps_and_keeps_disclaimer():
    comp = build_comparison(
        stock_pct=-0.09,
        industry_pct=-0.07,
        market_pct=-0.01,
        window_label="最近 5 个交易日",
    )
    assert comp.industry_vs_market.display == "-6.00pct"
    assert comp.stock_vs_industry.display == "-2.00pct"
    assert comp.disclaimer == DISCLAIMER
    assert "因果贡献" in comp.disclaimer


def test_comparison_shows_unavailable_instead_of_zero():
    comp = build_comparison(
        stock_pct=-0.09, industry_pct=None, market_pct=-0.01, window_label="今日"
    )
    assert comp.industry.display == "数据不可用"
    assert comp.stock_vs_industry.display == "数据不可用"
    assert comp.stock_vs_industry.value is None
