"""第一阶段单日价格画像的口径校验。

每个用例的期望值都是手算出来的，不是从实现里反推的。
"""

from __future__ import annotations

import pytest

from app.engine.price_profile import (
    build_daily_profile,
    describe_close_position,
    describe_turnover,
)
from app.providers.base import Bar


def make_bar(**kwargs) -> Bar:
    base = dict(
        date="2026-09-25",
        open=100.0,
        high=102.0,
        low=95.0,
        close=96.0,
        prev_close=100.0,
        volume=1000.0,
        amount=1.0e8,
    )
    base.update(kwargs)
    return Bar(**base)


def test_six_core_measures_match_hand_calculation():
    bar = make_bar()
    history = [make_bar(date=f"2026-08-{d:02d}", amount=5.0e7) for d in range(1, 21)]
    p = build_daily_profile(bar, history, baseline_days=20)

    assert p.pct_change == pytest.approx(96.0 / 100.0 - 1)  # -4%
    assert p.gap_pct == pytest.approx(0.0)  # 100/100 - 1
    assert p.post_open_pct == pytest.approx(96.0 / 100.0 - 1)
    assert p.amplitude == pytest.approx((102.0 - 95.0) / 100.0)  # 7%
    assert p.close_position == pytest.approx((96.0 - 95.0) / (102.0 - 95.0))
    assert p.turnover_ratio == pytest.approx(1.0e8 / 5.0e7)  # 2.0x
    assert p.gaps == []


def test_gap_down_open_is_separated_from_post_open_move():
    """开盘缺口和开盘后变化必须分开，否则无法定位价格变化发生的时点。"""
    bar = make_bar(open=94.0, close=96.0, high=97.0, low=93.0, prev_close=100.0)
    p = build_daily_profile(bar, [])
    assert p.gap_pct == pytest.approx(-0.06)
    assert p.post_open_pct == pytest.approx(96.0 / 94.0 - 1)
    assert p.pct_change == pytest.approx(-0.04)


def test_missing_prev_close_produces_explicit_gap_not_zero():
    bar = make_bar(prev_close=None)
    p = build_daily_profile(bar, [])
    assert p.pct_change is None
    assert p.gap_pct is None
    assert p.amplitude is None
    assert any(g.field == "prev_close" for g in p.gaps)


def test_limit_up_one_word_board_has_no_close_position():
    """一字涨停：最高=最低=收盘，收盘位置无定义，不能当成 0 或 1。"""
    bar = make_bar(open=110.0, high=110.0, low=110.0, close=110.0, prev_close=100.0)
    p = build_daily_profile(bar, [])
    assert p.pct_change == pytest.approx(0.10)
    assert p.close_position is None
    assert any(g.field == "close_position" for g in p.gaps)


def test_intraday_never_compares_partial_turnover():
    """盘中不得把半天成交额与历史完整交易日成交额相比。"""
    bar = make_bar(is_intraday=True, amount=4.0e7)
    history = [make_bar(date=f"2026-08-{d:02d}", amount=5.0e7) for d in range(1, 21)]
    p = build_daily_profile(bar, history, baseline_days=20)
    assert p.turnover_ratio is None
    gap = next(g for g in p.gaps if g.field == "turnover_ratio")
    assert "盘中" in gap.reason
    assert p.caliber("pct_change").endswith("（盘中）")


def test_insufficient_history_suppresses_turnover_ratio():
    bar = make_bar()
    history = [make_bar(date="2026-08-01", amount=5.0e7)]
    p = build_daily_profile(bar, history, baseline_days=20)
    assert p.turnover_ratio is None
    assert any(g.field == "turnover_ratio" for g in p.gaps)


def test_zero_baseline_turnover_is_rejected():
    bar = make_bar()
    history = [make_bar(date=f"2026-08-{d:02d}", amount=0.0) for d in range(1, 21)]
    p = build_daily_profile(bar, history, baseline_days=20)
    assert p.turnover_ratio is None


def test_descriptors_degrade_gracefully():
    assert describe_turnover(None) == "成交活跃度不可用"
    assert describe_turnover(2.5) == "成交显著放大"
    assert describe_turnover(0.5) == "成交明显萎缩"
    assert describe_close_position(None) == "收盘位置不可用"
    assert describe_close_position(0.05) == "收于当日低位"
    assert describe_close_position(0.9) == "收于当日高位"
