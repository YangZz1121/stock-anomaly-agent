"""第二类：数据缺失与接口失败。

这一组测试的共同断言是同一句话：**失败必须可见**。
产品可以少说、可以说不知道，但不允许在数据缺失时静默给出一个
看起来完全正常的结论。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.contracts import FetchStatus, ResearchWindow
from app.main import app
from app.orchestrator import NeedsWindowChoice, ResearchError
from tests.conftest import research


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _gap_fields(brief) -> set:
    return {g.field for g in brief.open_questions.gaps} | {
        g.field for g in brief.what_happened.gaps
    }


def test_unknown_stock_fails_loudly_instead_of_guessing():
    with pytest.raises(ResearchError) as exc:
        research("这家公司根本不存在", ResearchWindow.TODAY)
    assert exc.value.code == "stock_not_found"
    assert exc.value.hint


def test_missing_window_asks_the_user_instead_of_picking_one():
    """窗口直接决定结论，不能替用户默认选一个。"""
    with pytest.raises(NeedsWindowChoice) as exc:
        research("宁德时代")
    assert exc.value.code == "need_window"
    assert "3" in exc.value.hint and "5" in exc.value.hint


def test_index_failure_degrades_priority_and_shows_the_gap():
    """行业与市场指数都拿不到时，三层对比必须显示为不可用。"""
    brief = research("宁德时代", ResearchWindow.D5, faults=["index_daily_bars"])
    comparison = brief.what_happened.comparison

    assert comparison.industry.display == "数据不可用"
    assert comparison.market.display == "数据不可用"
    assert comparison.industry.status != FetchStatus.OK
    assert comparison.stock_vs_industry.value is None

    gaps = _gap_fields(brief)
    assert "index_daily_bars_industry" in gaps
    assert "index_daily_bars_market" in gaps


def test_industry_relative_measures_are_not_invented_when_industry_is_missing():
    """缺失不能被 0 冒充：相对差必须是 None，而不是 0.00pct。"""
    brief = research("宁德时代", ResearchWindow.D5, faults=["index_daily_bars"])
    for key in ("stock_vs_industry", "industry_vs_market"):
        measure = getattr(brief.what_happened.comparison, key)
        assert measure.value is None
        assert measure.display == "数据不可用"
        assert measure.display != "0.00pct"


def test_evidence_search_failure_does_not_produce_confident_drivers():
    """资讯接口整体失败时，不允许凭价格形态编出原因。"""
    brief = research("宁德时代", ResearchWindow.D5, faults=["search_events"])

    assert brief.why_happened.drivers == []
    assert brief.what_it_means.overall.can_summarize is False
    assert _gap_fields(brief), "证据检索失败却没有登记任何数据缺口"
    # 第一阶段是纯行情计算，不该被资讯失败带崩
    assert brief.what_happened.summary


def test_partial_evidence_failure_keeps_the_rest_and_records_the_gap():
    """只有公司范围检索失败时，其余范围的结论仍然成立，但缺口要登记。"""
    brief = research(
        "宁德时代", ResearchWindow.D5, faults=["search_events:company"]
    )
    names = " ".join(d.name for d in brief.why_happened.drivers)
    assert "公司公告" not in names
    assert brief.why_happened.drivers, "不该因为一个范围失败就整体放弃"
    assert _gap_fields(brief)


def test_failed_tool_calls_are_visible_in_metrics_and_trace():
    brief = research("宁德时代", ResearchWindow.D5, faults=["index_daily_bars"])
    assert brief.metrics.failed_tool_calls > 0

    failed = [c for c in brief.trace.tool_calls if c.status != FetchStatus.OK]
    assert failed
    assert all(c.note for c in failed), "失败的调用必须写明失败原因"


def test_missing_price_data_blocks_the_whole_research():
    """连行情都拿不到时，产品必须直接报错，而不是渲染一个空壳页面。"""
    with pytest.raises(ResearchError) as exc:
        research("宁德时代", ResearchWindow.D5, faults=["daily_bars"])
    assert exc.value.code
    assert exc.value.message


def test_calendar_failure_is_reported_rather_than_assumed():
    """交易日历失败时不能退回"按自然日算"，那会悄悄改变窗口口径。"""
    with pytest.raises(ResearchError):
        research("宁德时代", ResearchWindow.D5, faults=["trading_days"])


# --------------------------------------------------------------------- HTTP


def test_http_missing_window_returns_422_with_recovery_hint(client: TestClient):
    r = client.post("/api/research", json={"query": "宁德时代"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "need_window"
    assert detail["hint"]


def test_http_unknown_stock_returns_422(client: TestClient):
    r = client.post(
        "/api/research", json={"query": "不存在的公司", "window": "today"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "need_stock"


def test_sse_reports_errors_as_events_not_as_a_dead_connection(client: TestClient):
    """流式接口出错时必须发 error 事件并正常收尾，否则前端会一直转圈。"""
    with client.stream(
        "GET", "/api/research/stream", params={"query": "不存在的公司"}
    ) as r:
        events = [
            line[len("event: ") :]
            for line in r.iter_lines()
            if line.startswith("event: ")
        ]
    assert "error" in events
    assert events[-1] == "done"
