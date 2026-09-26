"""多日价格形态分类的校验。"""

from __future__ import annotations

from typing import List

import pytest

from app.contracts import PricePattern
from app.engine.patterns import build_window_profile
from app.providers.base import Bar

THRESHOLDS = dict(
    concentration_threshold=0.60,
    consistency_threshold=0.70,
    path_efficiency_threshold=0.70,
    reversal_min_segment_pct=0.02,
)


def series(returns: List[float], start: float = 100.0) -> List[Bar]:
    """按给定日收益序列构造 K 线。"""
    bars: List[Bar] = []
    prev = start
    for i, r in enumerate(returns):
        close = prev * (1 + r)
        bars.append(
            Bar(
                date=f"2026-09-{21 + i:02d}",
                open=prev,
                high=max(prev, close) * 1.002,
                low=min(prev, close) * 0.998,
                close=close,
                prev_close=prev,
                amount=1e8,
            )
        )
        prev = close
    return bars


def test_single_shock_when_one_day_dominates():
    # 一天 -8%，其余四天各 -0.3%，集中度约 87%
    profile = build_window_profile(
        series([-0.003, -0.003, -0.08, -0.003, -0.003]), **THRESHOLDS
    )
    assert profile.pattern == PricePattern.SINGLE_SHOCK
    assert profile.concentration > 0.60
    assert profile.max_single_day.date == "2026-09-23"
    assert "单日集中度" in profile.pattern_reason


def test_sustained_when_all_days_move_same_direction():
    profile = build_window_profile(
        series([-0.021, -0.018, -0.012, -0.020, -0.015]), **THRESHOLDS
    )
    assert profile.pattern == PricePattern.SUSTAINED
    assert profile.direction_consistency == pytest.approx(1.0)
    assert profile.path_efficiency > 0.9
    assert profile.same_direction_days == 5


def test_reversal_detected_on_five_day_window():
    # 前两日明显上涨，后三日明显下跌
    profile = build_window_profile(
        series([0.035, 0.028, -0.030, -0.025, -0.022]), **THRESHOLDS
    )
    assert profile.pattern == PricePattern.REVERSAL
    assert profile.reversal_split_date == "2026-09-22"


def test_tiny_oscillation_is_not_a_reversal():
    """两段幅度都很小的来回波动属于噪声，不应被识别为反转。"""
    profile = build_window_profile(
        series([0.002, 0.001, -0.002, -0.001, -0.002]), **THRESHOLDS
    )
    assert profile.reversal_split_date is None
    assert profile.pattern != PricePattern.REVERSAL


def test_mixed_when_direction_flips_without_clean_split():
    profile = build_window_profile(
        series([-0.03, 0.028, -0.025, 0.02, -0.022]), **THRESHOLDS
    )
    assert profile.pattern == PricePattern.MIXED
    assert profile.path_efficiency < 0.70


def test_concentration_formula_is_share_of_absolute_moves():
    profile = build_window_profile(series([-0.06, 0.02, -0.02]), **THRESHOLDS)
    # 0.06 / (0.06 + 0.02 + 0.02) = 0.6
    assert profile.concentration == pytest.approx(0.6, abs=0.01)


def test_path_efficiency_near_one_for_straight_line():
    straight = build_window_profile(series([-0.02, -0.02, -0.02]), **THRESHOLDS)
    assert straight.path_efficiency == pytest.approx(1.0, abs=1e-9)


def test_suspended_days_are_excluded_and_reported():
    """停牌日（无收盘价）不计入统计，并且必须显式报告。"""
    bars = series([-0.02, -0.02, -0.02])
    bars.insert(2, Bar(date="2026-09-22T", close=None, prev_close=None))
    profile = build_window_profile(bars, **THRESHOLDS)
    assert len(profile.daily_returns) == 3
    assert any(g.field == "daily_returns" for g in profile.gaps)


def test_empty_window_yields_no_pattern_conclusion():
    profile = build_window_profile([], **THRESHOLDS)
    assert profile.cumulative_pct is None
    assert any(g.field == "window_profile" for g in profile.gaps)


def test_flat_window_reports_gaps_instead_of_fake_numbers():
    profile = build_window_profile(series([0.0, 0.0, 0.0]), **THRESHOLDS)
    assert profile.concentration is None
    assert profile.direction_consistency is None
    fields = {g.field for g in profile.gaps}
    assert {"concentration", "direction_consistency"} <= fields


def test_max_drawdown_only_for_five_day_window():
    three = build_window_profile(series([-0.02, -0.02, -0.02]), **THRESHOLDS)
    five = build_window_profile(
        series([0.03, -0.02, -0.03, -0.01, -0.02]), **THRESHOLDS
    )
    assert three.max_drawdown is None
    assert five.max_drawdown < 0


def test_threshold_is_configurable_not_hardcoded():
    """把集中度阈值调高后，同一组数据不应再被判为单日冲击。"""
    data = series([-0.003, -0.003, -0.05, -0.003, -0.003])
    strict = dict(THRESHOLDS)
    strict["concentration_threshold"] = 0.95
    assert build_window_profile(data, **THRESHOLDS).pattern == PricePattern.SINGLE_SHOCK
    assert build_window_profile(data, **strict).pattern != PricePattern.SINGLE_SHOCK
