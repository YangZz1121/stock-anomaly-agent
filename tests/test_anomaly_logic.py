"""异动闸门、残差对比与横截面门槛。"""

from __future__ import annotations

import pytest

from app.agent.evidence_collect import ClusterInfo, EvidenceWindow
from app.agent.validation import MarketContext, check_cross_section
from app.contracts import (
    CheckResult,
    DriverCategory,
    ResearchPriority,
    ResearchWindow,
    SourceTier,
)
from app.engine.anomaly import evaluate_anomaly
from app.engine.ashare import classify_board, detect_limit_state
from app.engine.priority import decide_priority
from app.engine.reason_tags import normalize_reason_tags
from app.engine.residual import estimate_residuals, ols_beta
from app.providers.base import AnomalyReason, Bar
from tests.conftest import research


def _bar(date: str, close: float, prev: float, amount: float = 1e8) -> Bar:
    return Bar(
        date=date,
        open=prev,
        high=max(prev, close),
        low=min(prev, close),
        close=close,
        prev_close=prev,
        amount=amount,
    )


def _window() -> EvidenceWindow:
    return EvidenceWindow(
        core_start="2026-09-24", core_end="2026-09-25", extended_start="2026-09-17"
    )


def _ctx(**kwargs) -> MarketContext:
    base = dict(
        window=ResearchWindow.TODAY,
        window_days=["2026-09-25"],
        evidence_window=_window(),
        stock_pct=-0.06,
        industry_pct=-0.03,
        market_pct=-0.0005,
        stock_vs_industry=-0.03,
        industry_vs_market=-0.0295,
        divergence_threshold=0.02,
        industry_move_threshold=0.02,
        market_move_threshold=0.01,
        stock_name="测试股份",
    )
    base.update(kwargs)
    return MarketContext(**base)


def test_board_rules_match_tradingagents_astock():
    assert classify_board("600519.SH", "贵州茅台").limit_pct == 0.10
    assert classify_board("300750.SZ", "宁德时代").limit_pct == 0.20
    assert classify_board("688981.SH").limit_pct == 0.20
    assert classify_board("830799.BJ").limit_pct == 0.30
    assert classify_board("600000.SH", "ST示例").limit_pct == 0.05


def test_limit_up_is_detected():
    bar = _bar("2026-09-25", 110.0, 100.0)
    bar.high = 110.0
    bar.low = 110.0
    assert detect_limit_state(bar, 0.10) == "limit_up_one_word"


def test_ols_beta_recovers_known_slope():
    x = [0.01, -0.01, 0.02, -0.02, 0.015, -0.015, 0.01, -0.01, 0.005, -0.005]
    y = [1.5 * xi for xi in x]
    assert ols_beta(y, x, min_obs=10) == pytest.approx(1.5, abs=1e-9)


def _series(prefix: str, returns, start: float = 100.0):
    bars = []
    prev = start
    for i, ret in enumerate(returns, start=1):
        close = prev * (1 + ret)
        bars.append(_bar(f"{prefix}-{i:02d}", close, prev))
        prev = close
    return bars


def test_residual_uses_lookback_beta_not_raw_subtract():
    market_rets = [0.01, -0.01, 0.02, -0.02, 0.015, -0.015, 0.01, -0.01, 0.005, -0.005, 0.012, -0.008]
    hist_mkt = _series("2026-08", market_rets)
    hist_stock = _series("2026-08", [1.5 * r for r in market_rets])
    result = estimate_residuals(
        stock_history=hist_stock,
        market_history=hist_mkt,
        industry_history=hist_mkt,
        stock_cum=-0.032,
        market_cum=-0.02,
        industry_cum=-0.02,
        min_obs=10,
    )
    assert result.method == "ols_beta"
    assert result.beta_market == pytest.approx(1.5, abs=1e-6)
    assert result.stock_vs_market == pytest.approx(-0.002, abs=1e-6)


def test_market_cross_section_requires_real_market_move():
    cluster = ClusterInfo(
        cluster_id="C1",
        title="风险偏好回落",
        summary="",
        scope="market",
        best_tier=SourceTier.T2_PROFESSIONAL,
        in_core_window=True,
    )
    tiny = check_cross_section(cluster, DriverCategory.MARKET, _ctx())
    assert tiny.result == CheckResult.PARTIAL
    moved = check_cross_section(
        cluster, DriverCategory.MARKET, _ctx(market_pct=-0.018, industry_pct=-0.02)
    )
    assert moved.result == CheckResult.PASS


def test_industry_cross_section_uses_relative_residual():
    cluster = ClusterInfo(
        cluster_id="C2",
        title="行业政策",
        summary="",
        scope="industry",
        best_tier=SourceTier.T1_AUTHORITATIVE,
        in_core_window=True,
    )
    followed = check_cross_section(
        cluster,
        DriverCategory.INDUSTRY,
        _ctx(
            stock_pct=-0.03,
            industry_pct=-0.03,
            market_pct=-0.03,
            industry_vs_market=-0.001,
        ),
    )
    assert followed.result == CheckResult.PARTIAL
    real = check_cross_section(
        cluster,
        DriverCategory.INDUSTRY,
        _ctx(
            stock_pct=-0.05,
            industry_pct=-0.045,
            market_pct=-0.005,
            industry_vs_market=-0.04,
        ),
    )
    assert real.result == CheckResult.PASS


def test_quiet_move_is_not_an_anomaly():
    bars = [_bar("2026-09-25", 101.5, 100.0)]
    history = _series("2026-08", [0.01 if i % 2 else -0.01 for i in range(16)])
    gate = evaluate_anomaly(
        thscode="600519.SH",
        name="贵州茅台",
        window_bars=bars,
        history_bars=history,
        stock_pct=0.015,
        market_pct=0.003,
        industry_vs_market=0.011,
        stock_vs_industry=0.001,
    )
    assert gate.is_anomaly is False
    priority, _, _ = decide_priority(
        stock_pct=0.015,
        industry_pct=0.014,
        market_pct=0.003,
        divergence_threshold=0.02,
        industry_move_threshold=0.02,
        market_move_threshold=0.01,
        anomaly=gate,
    )
    assert priority == ResearchPriority.NO_ANOMALY


def test_exchange_three_day_deviation_is_an_anomaly():
    """tick-stock-panel：主板 3 日 ±20%。"""
    trail = [
        _bar("2026-09-23", 100.0, 100.0),
        _bar("2026-09-24", 110.0, 100.0),
        _bar("2026-09-25", 122.0, 110.0),
    ]
    gate = evaluate_anomaly(
        thscode="600519.SH",
        name="贵州茅台",
        window_bars=trail[-1:],
        history_bars=trail[:-1],
        stock_pct=0.11,
        market_pct=0.002,
        industry_vs_market=0.001,
        stock_vs_industry=0.001,
    )
    assert gate.is_anomaly is True
    assert any("3 日" in item for item in gate.exchange_hits)


def test_reason_tags_prefer_tag_name():
    clues = normalize_reason_tags(
        [
            AnomalyReason(
                thscode="300750.SZ",
                stock_name="宁德时代",
                content="第三方长文解读" * 3,
                tag_name="出口政策",
                keywords=["动力电池", "准入"],
            )
        ]
    )
    assert clues[0] == "出口政策"
    assert "动力电池" in clues


def test_existing_catl_scenario_still_enters_attribution():
    brief = research("宁德时代", ResearchWindow.D5)
    assert brief.what_happened.anomaly is not None
    assert brief.what_happened.anomaly.is_anomaly is True
    assert brief.why_happened.priority != ResearchPriority.NO_ANOMALY
    assert brief.what_happened.comparison.residual_note
