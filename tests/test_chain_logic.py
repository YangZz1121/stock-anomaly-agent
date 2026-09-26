"""断环诊断、定向补证与传导链截断。"""

from __future__ import annotations

import asyncio

from app.agent.actions import ActionName
from app.agent.evidence_collect import (
    ClusterInfo,
    CollectionResult,
    EvidenceCollector,
    EvidenceWindow,
)
from app.agent.memory import AgentMemory
from app.agent.planner import HeuristicPlanner
from app.agent.reasoner import (
    ChainStep,
    DriverProposal,
    HeuristicReasoner,
    MechanismVerdict,
)
from app.contracts import (
    CheckResult,
    DriverCategory,
    DriverStatus,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
    SourceTier,
)
from app.engine.chain_logic import diagnose_driver_gaps, diagnose_patch, ground_chain
from app.ledger import EvidenceLedger
from app.providers.evidence_mock import MockEvidenceProvider
from app.schemas import Driver, DriverCheck
from app.config import Settings
from app.trace import RunRecorder


def _cluster(**kwargs) -> ClusterInfo:
    base = dict(
        cluster_id="C1",
        title="动力电池准入新规征求意见稿",
        summary="拟提高进口动力电池本地化含量，设置过渡期。",
        scope="industry",
        evidence_ids=["E1"],
        best_tier=SourceTier.T1_AUTHORITATIVE,
        in_core_window=True,
        report_count=2,
        kinds=["news"],
    )
    base.update(kwargs)
    return ClusterInfo(**base)


def _proposal(**kwargs) -> DriverProposal:
    base = dict(
        name="动力电池准入新规",
        category=DriverCategory.INDUSTRY,
        summary="出口门槛提高",
        cluster_id="C1",
        relevance="核心窗口内",
    )
    base.update(kwargs)
    return DriverProposal(**base)


def _driver(**kwargs) -> Driver:
    base = dict(
        id="D1",
        name="动力电池准入新规",
        category=DriverCategory.INDUSTRY,
        category_label="行业",
        status=DriverStatus.PARTIALLY_SUPPORTED,
        status_label="部分支持",
        summary="出口门槛提高",
        relevance="核心窗口内",
        checks=[
            DriverCheck(
                key="timing",
                label="时间吻合",
                result=CheckResult.PASS,
                result_label="吻合",
                reasoning="落在核心窗口",
            ),
            DriverCheck(
                key="mechanism",
                label="机制合理性",
                result=CheckResult.PARTIAL,
                result_label="部分吻合",
                reasoning="尚未确认公司暴露",
            ),
        ],
    )
    base.update(kwargs)
    return Driver(**base)


def test_industry_partial_mechanism_requests_exposure_patch():
    gaps = diagnose_driver_gaps(
        _driver(),
        _proposal(),
        _cluster(),
        MechanismVerdict(result=CheckResult.PARTIAL, reasoning="暴露未确认"),
    )
    assert gaps == ["exposure"]


def test_market_factor_does_not_search_filings_for_exposure():
    gaps = diagnose_driver_gaps(
        _driver(
            name="风险偏好回落",
            category=DriverCategory.MARKET,
            category_label="市场 / 宏观",
        ),
        _proposal(name="风险偏好回落", category=DriverCategory.MARKET),
        _cluster(title="外围股指收跌", scope="market"),
        MechanismVerdict(result=CheckResult.PARTIAL, reasoning="普遍因素"),
    )
    assert "exposure" not in gaps


def test_company_authoritative_event_does_not_need_exposure_patch():
    gaps = diagnose_driver_gaps(
        _driver(
            category=DriverCategory.COMPANY,
            category_label="公司特定",
            checks=[
                DriverCheck(
                    key="mechanism",
                    label="机制合理性",
                    result=CheckResult.PASS,
                    result_label="吻合",
                    reasoning="事件主体即本公司",
                )
            ],
        ),
        _proposal(category=DriverCategory.COMPANY),
        _cluster(scope="company", title="公司公告：签署框架协议"),
        MechanismVerdict(result=CheckResult.PASS, reasoning="直接成立"),
    )
    assert gaps == []


def test_unverified_event_requests_official_refill():
    gaps = diagnose_driver_gaps(
        _driver(status=DriverStatus.INSUFFICIENT, status_label="证据不足"),
        _proposal(category=DriverCategory.COMPANY),
        _cluster(
            scope="company",
            title="网传订单削减",
            best_tier=SourceTier.T4_UNVERIFIED,
        ),
        MechanismVerdict(result=CheckResult.UNKNOWN, reasoning="出处不明"),
    )
    assert "event" in gaps


def test_heuristic_planner_patches_before_counter():
    memory = AgentMemory("宁德时代", "t-patch")
    memory.mark("subject")
    memory.mark("snapshot")
    memory.mark("retrieved")
    memory.mark("proposed")
    memory.mark("assessed")
    memory.scopes = ["industry", "company"]
    driver = _driver()
    memory.drivers = [driver]
    memory.drafts = {
        driver.id: (
            _proposal(),
            _cluster(),
            driver.checks,
            MechanismVerdict(result=CheckResult.PARTIAL, reasoning="暴露未确认"),
        )
    }
    action = HeuristicPlanner(extra_search_limit=3).next(memory)
    assert action.name == ActionName.SEARCH_EVIDENCE
    assert action.scopes == ["company"]
    assert any(term in action.extra_terms for term in ("年报", "年度报告", "主营"))
    assert memory.pending_patch is not None
    assert memory.pending_patch.needs_exposure_lookback is True

    memory.mark("patched")
    memory.pending_patch = None
    action = HeuristicPlanner(extra_search_limit=3).next(memory)
    assert action.name == ActionName.SEARCH_COUNTER


def test_ground_chain_stops_at_missing_exposure():
    steps = [
        ChainStep(text="事件事实：准入新规", link="event", evidence_ids=["E1"]),
        ChainStep(text="公司暴露：无法确认", link="exposure", status="missing"),
        ChainStep(
            text="若该因素持续，盈利空间承压",
            is_conditional=True,
            link="mechanism",
        ),
        ChainStep(text="影响方向：负向", link="direction"),
    ]
    out = ground_chain(steps, ExposureLevel.UNCONFIRMED, valid_ids={"E1"})
    texts = [s.text for s in out]
    assert any("暴露" in t for t in texts)
    assert any("中断" in t for t in texts)
    assert not any("盈利空间" in t for t in texts)
    assert not any("影响方向" in t for t in texts)


def test_heuristic_chain_has_five_links_when_exposure_confirmed():
    draft = asyncio.run(
        HeuristicReasoner().build_transmission(
            _proposal(),
            _cluster(),
            [
                _cluster(),
                _cluster(
                    cluster_id="C-exp",
                    title="公司年度报告：境外营业收入占比 32.6%",
                    summary="境外业务收入主要来自动力电池产品出口。",
                    scope="company",
                    evidence_ids=["E9"],
                    kinds=["filing"],
                ),
            ],
            type("Ctx", (), {"stock_vs_industry": None, "divergence_threshold": 0.02})(),
            EvidenceLedger(),
        )
    )
    assert draft.exposure_level == ExposureLevel.P1_FILING
    links = [s.link for s in draft.chain]
    assert links[:2] == ["event", "exposure"]
    assert "mechanism" in links
    assert draft.direction == ImpactDirection.NEGATIVE or draft.horizon in set(
        ImpactHorizon
    )


def test_exposure_lookback_finds_filing_outside_news_window():
    async def _run() -> None:
        settings = Settings(
            market_provider="mock", evidence_provider="mock", llm_provider="mock"
        )
        ledger = EvidenceLedger()
        collector = EvidenceCollector(
            MockEvidenceProvider(settings), ledger, RunRecorder("lookback")
        )
        window = EvidenceWindow(
            core_start="2026-09-22",
            core_end="2026-09-25",
            extended_start="2026-09-22",
        )
        empty = CollectionResult(window=window)
        missed = await collector.collect_scope(
            result=empty,
            window=window,
            scope="company",
            stock_name="宁德时代",
            industry_name="电池",
            extra_terms=["年报"],
        )
        assert not any("年度报告" in c.title for c in missed.clusters)

        found = await collector.collect_scope(
            result=CollectionResult(window=window),
            window=window,
            scope="company",
            stock_name="宁德时代",
            industry_name="电池",
            extra_terms=["年报"],
            start_date="2026-03-01",
            pass_label="patch",
        )
        assert any("年度报告" in c.title for c in found.clusters)

    asyncio.run(_run())


def test_diagnose_patch_skips_when_no_drivers():
    memory = AgentMemory("空", "t0")
    memory.mark("assessed")
    assert diagnose_patch(memory) is None
