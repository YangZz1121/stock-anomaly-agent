"""第四类：极端与边界场景（端到端）。

单个公式的边界在 test_price_profile / test_resolver 里逐条覆盖了，
这一组关心的是整条链路在边界上的行为：窗口落到非交易日、事件发布于
收盘之后、查询里全是噪声词、窗口内交易日不足等等。
"""

from __future__ import annotations

import pytest

from app.contracts import CheckResult, ResearchWindow
from app.orchestrator import NeedsWindowChoice, ResearchError
from tests.conftest import research


def test_non_trading_day_is_remapped_and_the_remap_is_disclosed():
    """窗口被挪动过就必须说出来，否则用户会以为看的是今天的数据。"""
    brief = research("贵州茅台", ResearchWindow.TODAY)
    window = brief.subject.window
    assert window.remap_note, "研究日期被改写却没有任何提示"
    assert window.actual_end in window.remap_note
    assert window.actual_start == window.actual_end


def test_after_close_news_cannot_explain_the_same_day_move():
    """收盘后发布的消息解释不了收盘前已经发生的价格变化。"""
    brief = research("贵州茅台", ResearchWindow.TODAY)
    late = next(
        d
        for d in brief.why_happened.drivers
        if any(
            c.key == "timing" and c.result == CheckResult.FAIL for c in d.checks
        )
    )
    timing = next(c for c in late.checks if c.key == "timing")
    assert "收盘之后" in timing.reasoning
    assert late.assessment is None, "时间检验不通过的因素不应进入影响评估"


def test_query_with_only_a_code_still_resolves():
    brief = research("300750", ResearchWindow.D3)
    assert brief.subject.stock.thscode == "300750.SZ"


@pytest.mark.parametrize(
    "query",
    [
        "宁德时代最近三天怎么一直跌啊急",
        "帮我看看 宁德时代 这几天的走势",
        "300750.SZ 最近 5 个交易日",
    ],
)
def test_noisy_queries_do_not_break_stock_resolution(query):
    brief = research(query, ResearchWindow.D3)
    assert brief.subject.stock.thscode == "300750.SZ"


def test_empty_query_is_rejected_cleanly():
    with pytest.raises((ResearchError, NeedsWindowChoice)):
        research("   ", ResearchWindow.TODAY)


def test_three_day_and_five_day_windows_produce_different_conclusions():
    """窗口口径会改变结论，所以两个窗口不能算出同一份区间数据。"""
    d3 = research("宁德时代", ResearchWindow.D3)
    d5 = research("宁德时代", ResearchWindow.D5)

    assert len(d3.what_happened.series) == 3
    assert len(d5.what_happened.series) == 5

    c3 = next(m for m in d3.what_happened.measures if m.key == "cumulative_pct")
    c5 = next(m for m in d5.what_happened.measures if m.key == "cumulative_pct")
    assert c3.value != c5.value


def test_max_drawdown_only_reported_for_the_five_day_window():
    """3 个交易日算回撤没有意义，产品不应该硬造这个指标。"""
    d3 = research("宁德时代", ResearchWindow.D3)
    d5 = research("宁德时代", ResearchWindow.D5)
    assert all(m.key != "max_drawdown" for m in d3.what_happened.measures)
    assert any(m.key == "max_drawdown" for m in d5.what_happened.measures)


def test_single_day_window_omits_window_only_measures():
    """今日窗口给的是当日六项指标，不该混进区间指标。"""
    brief = research("伊利股份", ResearchWindow.TODAY)
    keys = {m.key for m in brief.what_happened.measures}
    assert "pct_change" in keys
    assert "cumulative_pct" not in keys
    assert "path_efficiency" not in keys


def test_unexplained_residual_is_stated_rather_than_absorbed():
    """个股相对行业还有额外变化时，必须留成未解决问题，而不是塞给某个因素。"""
    brief = research("宁德时代", ResearchWindow.D5)
    residual = brief.what_happened.comparison.stock_vs_industry.value
    assert residual is not None and abs(residual) > 0.02
    assert any(
        "未被解释" in q or "无法由该因素解释" in q
        for q in brief.open_questions.questions
    )


def test_background_filings_are_not_proposed_as_drivers():
    """年报是慢变量，是暴露证据，不是"今天为什么跌"的原因。"""
    brief = research("宁德时代", ResearchWindow.D5)
    assert all("年度报告" not in d.name for d in brief.why_happened.drivers)

    exposure_cited = {
        ref.evidence_id
        for d in brief.why_happened.drivers
        if d.assessment
        for ref in d.assessment.exposure_refs
    }
    by_id = {e.id: e for e in brief.evidence}
    assert any("年度报告" in by_id[eid].claim for eid in exposure_cited), (
        "年报既没当驱动因素，也没被用作暴露证据，说明它被整个丢掉了"
    )


def test_open_questions_are_never_silently_empty():
    """「不知道」是结果的一部分；四个场景都必须有未解决问题或数据缺口。"""
    for query, window in [
        ("宁德时代", ResearchWindow.D5),
        ("贵州茅台", ResearchWindow.TODAY),
        ("伊利股份", ResearchWindow.TODAY),
    ]:
        brief = research(query, window)
        section = brief.open_questions
        assert section.questions or section.gaps, f"{query} 没有登记任何未解决问题"
