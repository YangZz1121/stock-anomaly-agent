"""第一类：主链路。

覆盖交付物要求的"主链路可用"——用户提一个自然语言问题，产品能走完
三个阶段，产出结构完整、可反查证据的 Brief，并且 HTTP 与 SSE 两条接口
都能拿到同一份结果。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.contracts import (
    DriverCategory,
    DriverStatus,
    ResearchWindow,
    SourceTier,
)
from app.main import app
from tests.conftest import research


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_brief_has_all_five_sections():
    brief = research("宁德时代最近5个交易日怎么一直跌")

    assert brief.subject.stock.thscode == "300750.SZ"
    assert brief.subject.window.window == ResearchWindow.D5
    assert brief.what_happened.summary
    assert brief.why_happened.drivers
    assert brief.what_it_means.overall.display
    # 「不知道」也是结果，所以未解决问题区不允许为空壳：
    # 要么有问题，要么有数据缺口，要么明确说明二者都没有。
    assert brief.open_questions.note


def test_every_conclusion_traces_back_to_a_registered_evidence_id():
    """核心约束：结论只持有证据 ID，且这些 ID 必须真实存在于证据台账。"""
    brief = research("宁德时代最近5个交易日怎么一直跌")
    known = {e.id for e in brief.evidence}
    assert known

    cited = set()
    for driver in brief.why_happened.drivers:
        for ref in driver.supporting_refs + driver.contradicting_refs:
            cited.add(ref.evidence_id)
        for check in driver.checks:
            cited.update(ref.evidence_id for ref in check.evidence_refs)
        if driver.assessment:
            cited.update(ref.evidence_id for ref in driver.assessment.exposure_refs)

    assert cited, "Brief 没有引用任何证据，说明结论是凭空生成的"
    assert cited <= known, f"引用了不存在的证据 ID：{sorted(cited - known)}"


def test_window_profile_matches_the_series_it_reports():
    """展示的区间涨跌必须能由展示的 K 线自己算出来，不能两套数。"""
    brief = research("宁德时代", ResearchWindow.D5)
    series = brief.what_happened.series
    assert len(series) == 5

    cumulative = next(
        m for m in brief.what_happened.measures if m.key == "cumulative_pct"
    )
    expected = series[-1].close / series[0].prev_close - 1
    assert cumulative.value == pytest.approx(expected, abs=1e-9)


def test_industry_and_company_scenario_routes_to_both_investigations():
    """宁德时代场景是行业与公司因素叠加，研究优先级必须两者并重。"""
    brief = research("宁德时代", ResearchWindow.D5)
    categories = {d.category for d in brief.why_happened.drivers}
    assert DriverCategory.INDUSTRY in categories
    assert DriverCategory.COMPANY in categories
    assert brief.what_happened.comparison.stock_vs_industry.value is not None


def test_company_specific_scenario_surfaces_the_company_event():
    brief = research("伊利股份今天为什么大跌", ResearchWindow.TODAY)
    company = [
        d for d in brief.why_happened.drivers if d.category == DriverCategory.COMPANY
    ]
    assert company, "公司特有场景下没有产出任何公司因素"
    assert company[0].status == DriverStatus.SUPPORTED
    assert company[0].assessment is not None


def test_market_synchronized_scenario_routes_to_market_first():
    brief = research("贵州茅台今天怎么跌了", ResearchWindow.TODAY)
    assert brief.why_happened.drivers[0].category == DriverCategory.MARKET
    assert "市场" in brief.why_happened.priority_label


def test_reprints_of_one_source_count_as_one_independent_source():
    """多家媒体转载同一份原文，独立信源仍然只有一个。"""
    brief = research("宁德时代", ResearchWindow.D5)
    policy = next(
        d
        for d in brief.why_happened.drivers
        if d.category == DriverCategory.INDUSTRY and "准入" in d.name
    )
    by_id = {e.id: e for e in brief.evidence}
    cited = [by_id[ref.evidence_id] for ref in policy.supporting_refs]

    assert len(cited) > 1, "该场景刻意构造了多篇转载，用于验证去重"
    assert len({e.origin_key for e in cited}) == 1
    assert "1 个独立信源" in policy.relevance
    assert f"{len(cited)} 篇报道" in policy.relevance


def test_run_metrics_are_populated():
    brief = research("宁德时代", ResearchWindow.D5)
    m = brief.metrics
    assert m.time_to_verifiable_insight_ms > 0
    assert 0.0 <= m.evidence_coverage <= 1.0
    assert m.unsupported_inference_rate == 0.0
    assert m.evidence_count == len(brief.evidence)
    assert m.tool_calls > 0


def test_research_trace_records_every_tool_call():
    brief = research("宁德时代", ResearchWindow.D5)
    assert brief.trace.tool_calls
    for call in brief.trace.tool_calls:
        assert call.provider and call.tool
        # 轨迹会展示给用户，不能把密钥带出去
        assert "key" not in json.dumps(call.params, ensure_ascii=False).lower()


# --------------------------------------------------------------------- HTTP


def test_http_research_returns_a_complete_brief(client: TestClient):
    r = client.post(
        "/api/research",
        json={"query": "宁德时代最近5个交易日怎么一直跌", "window": "d5"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subject"]["stock"]["name"] == "宁德时代"
    assert body["kind"] == "report"
    assert body["why_happened"]["drivers"]
    assert body["evidence"]


def test_config_endpoint_declares_degraded_mode(client: TestClient):
    """跑在构造数据上时，产品必须主动承认自己处于降级状态。"""
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["notices"], "降级时没有给出任何提示"
    assert body["disclaimers"]


def test_sse_stream_emits_progress_then_brief(client: TestClient):
    with client.stream(
        "GET",
        "/api/research/stream",
        params={"query": "宁德时代最近5个交易日怎么一直跌", "window": "d5"},
    ) as r:
        assert r.status_code == 200
        events = [
            line[len("event: ") :]
            for line in r.iter_lines()
            if line.startswith("event: ")
        ]

    assert events[0] == "start"
    assert events[-1] == "done"
    assert "brief" in events
    assert events.count("step") >= 5
    # 进度必须在结果之前推送，否则流式就没有意义
    assert events.index("step") < events.index("brief")


def test_t4_source_can_never_support_a_conclusion():
    """未证实的网传消息只能当线索，不能当结论依据。"""
    brief = research("宁德时代", ResearchWindow.D5)
    rumor = next(
        (d for d in brief.why_happened.drivers if "网传" in d.name), None
    )
    assert rumor is not None
    assert rumor.status == DriverStatus.INSUFFICIENT
    assert rumor.assessment is None, "证据不足的因素不应进入第三阶段"

    tiers = {e.id: e.source_tier for e in brief.evidence}
    for ref in rumor.supporting_refs:
        assert tiers[ref.evidence_id] == SourceTier.T4_UNVERIFIED
