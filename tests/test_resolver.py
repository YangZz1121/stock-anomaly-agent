"""输入解析与交易日窗口映射的校验。"""

from __future__ import annotations

from app.contracts import ResearchWindow
from app.engine.resolver import parse_query, resolve_window

DAYS = [
    "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
    "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
]


def test_parses_company_name_window_and_direction():
    q = parse_query("宁德时代今天为什么跌了这么多？")
    assert q.name_hint == "宁德时代"
    assert q.window == ResearchWindow.TODAY
    assert q.direction_hint == "用户认为价格下跌"


def test_parses_multi_day_window():
    assert parse_query("宁德时代最近3个交易日怎么一直跌").window == ResearchWindow.D3
    assert parse_query("茅台最近5天走势").window == ResearchWindow.D5
    assert parse_query("伊利股份最近几天怎么了").window == ResearchWindow.D5


def test_bare_stock_name_leaves_window_unspecified():
    """只输入股票名时不替用户猜窗口，由前端提示选择。"""
    q = parse_query("宁德时代")
    assert q.name_hint == "宁德时代"
    assert q.window is None


def test_extracts_various_code_formats():
    assert parse_query("300750.SZ 今天怎么了").code == "300750.SZ"
    assert parse_query("sz300750 异动").code == "300750.SZ"
    assert parse_query("看看600519").code == "600519"


def test_alias_maps_nicknames_to_official_names():
    assert parse_query("宁王今天怎么了").name_hint == "宁德时代"
    assert parse_query("茅台最近5天走势").name_hint == "贵州茅台"
    assert parse_query("CATL").name_hint == "宁德时代"


def test_extracts_multiple_company_names():
    q = parse_query("茅台和伊利今天异动对比")
    assert q.name_hints == ["贵州茅台", "伊利股份"]
    assert q.search_keys == ["贵州茅台", "伊利股份"]


def test_window_maps_to_last_trading_day_when_today_is_holiday():
    res = resolve_window(
        ResearchWindow.TODAY, DAYS, baseline_days=5, reference_day="2026-09-27"
    )
    assert res is not None
    assert res.info.actual_end == "2026-09-25"
    assert res.info.remapped is True
    assert "2026-09-25" in res.info.remap_note


def test_today_window_on_trading_day_is_not_remapped():
    res = resolve_window(
        ResearchWindow.TODAY, DAYS, baseline_days=5, reference_day="2026-09-25"
    )
    assert res.info.remapped is False
    assert res.info.trading_days == ["2026-09-25"]


def test_three_and_five_day_windows_use_trading_days_not_calendar_days():
    """跨周末时，3 个交易日不等于 3 个自然日。"""
    r3 = resolve_window(
        ResearchWindow.D3, DAYS, baseline_days=2, reference_day="2026-09-22"
    )
    # 09-19/09-20 是周末，窗口向前跨过周末取到 09-18
    assert r3.info.trading_days == ["2026-09-18", "2026-09-21", "2026-09-22"]

    r5 = resolve_window(
        ResearchWindow.D5, DAYS, baseline_days=2, reference_day="2026-09-25"
    )
    assert r5.info.trading_days == DAYS[-5:]


def test_lookback_start_reserves_room_for_baseline():
    res = resolve_window(
        ResearchWindow.TODAY, DAYS, baseline_days=5, reference_day="2026-09-25"
    )
    assert res.lookback_start < res.info.actual_start


def test_missing_calendar_blocks_research():
    """日历不可用时返回 None，上层必须终止而不是自己编日期。"""
    assert resolve_window(ResearchWindow.TODAY, [], reference_day="2026-09-25") is None


def test_insufficient_history_blocks_window():
    short = ["2026-09-24", "2026-09-25"]
    assert (
        resolve_window(ResearchWindow.D5, short, reference_day="2026-09-25") is None
    )
