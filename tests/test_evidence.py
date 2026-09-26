"""证据系统的校验：来源分级、事件聚类、独立来源、支撑资格。"""

from __future__ import annotations

from app.contracts import EvidenceKind, SourceTier, SupportLevel, EvidenceRef
from app.engine.source_tier import (
    classify_kind,
    classify_tier,
    cluster_events,
    count_independent_sources,
)
from app.ledger import EvidenceLedger
from app.providers.base import RawEvent


def event(**kwargs) -> RawEvent:
    base = dict(
        event_id="E",
        title="标题",
        summary="摘要",
        published_at="2026-09-24",
        source_name="财联社",
    )
    base.update(kwargs)
    return RawEvent(**base)


# --------------------------------------------------------------------------
# 来源分级
# --------------------------------------------------------------------------


def test_authoritative_sources_are_tier_one():
    assert classify_tier("国务院办公厅") == SourceTier.T1_AUTHORITATIVE
    assert classify_tier("公司公告") == SourceTier.T1_AUTHORITATIVE
    assert classify_tier("上交所") == SourceTier.T1_AUTHORITATIVE


def test_professional_media_are_tier_two():
    assert classify_tier("财联社") == SourceTier.T2_PROFESSIONAL
    assert classify_tier("证券时报") == SourceTier.T2_PROFESSIONAL


def test_unverified_sources_are_tier_four():
    assert classify_tier("社交平台匿名帖") == SourceTier.T4_UNVERIFIED
    assert classify_tier("某自媒体") == SourceTier.T4_UNVERIFIED
    assert classify_tier("网传消息") == SourceTier.T4_UNVERIFIED


def test_unknown_source_defaults_to_tier_three():
    assert classify_tier("某财经资讯站") == SourceTier.T3_GENERAL


def test_evidence_kind_distinguishes_filing_from_news():
    assert classify_kind(event(source_name="公司年度报告")) == EvidenceKind.FILING
    assert classify_kind(event(source_name="公司公告")) == EvidenceKind.ANNOUNCEMENT
    assert classify_kind(event(source_name="国务院")) == EvidenceKind.POLICY
    assert classify_kind(event(source_name="财联社")) == EvidenceKind.NEWS


# --------------------------------------------------------------------------
# 事件聚类与独立来源
# --------------------------------------------------------------------------


def test_reprints_of_one_policy_form_a_single_cluster():
    events = [
        event(event_id="1", source_name="主管部门官方网站", cluster_hint="C1"),
        event(event_id="2", source_name="财联社", cluster_hint="C1"),
        event(event_id="3", source_name="证券时报", cluster_hint="C1"),
    ]
    clusters = cluster_events(events)
    assert len(clusters) == 1
    assert len(clusters["C1"]) == 3


def test_ten_reprints_of_one_origin_count_as_one_independent_source():
    """10 家媒体转载同一份原文，本质上仍是 1 个独立信源。"""
    events = [
        event(event_id=str(i), source_name=f"媒体{i}", origin_key="ORIGIN-A")
        for i in range(10)
    ]
    assert count_independent_sources(events) == 1


def test_distinct_origins_count_separately():
    events = [
        event(event_id="1", source_name="财联社", origin_key="ORIGIN-A"),
        event(event_id="2", source_name="证券时报", origin_key="ORIGIN-B"),
    ]
    assert count_independent_sources(events) == 2


def test_similar_titles_cluster_without_explicit_hint():
    events = [
        event(event_id="1", title="动力电池准入新规征求意见稿发布"),
        event(event_id="2", title="动力电池准入新规征求意见稿发布解读"),
        event(event_id="3", title="白酒板块资金流出明显"),
    ]
    clusters = cluster_events(events)
    assert len(clusters) == 2


# --------------------------------------------------------------------------
# 台账
# --------------------------------------------------------------------------


def test_ledger_assigns_stable_ids_and_dedups_identical_claims():
    ledger = EvidenceLedger()
    a = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="动力电池准入新规发布",
        source_tier=SourceTier.T2_PROFESSIONAL,
        source_name="财联社",
    )
    b = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="动力电池准入新规发布",
        source_tier=SourceTier.T1_AUTHORITATIVE,
        source_name="主管部门",
    )
    assert a.id == b.id == "E1"
    assert len(ledger) == 1
    # 重复登记时保留可信度更高的来源
    assert ledger.get("E1").source_tier == SourceTier.T1_AUTHORITATIVE


def test_tier_four_evidence_cannot_support_a_conclusion():
    ledger = EvidenceLedger()
    rumor = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="网传订单削减",
        source_tier=SourceTier.T4_UNVERIFIED,
        source_name="社交平台匿名帖",
    )
    refs = [EvidenceRef(evidence_id=rumor.id, support=SupportLevel.SUPPORTS)]
    assert rumor.can_support_conclusion is False
    assert ledger.refs_can_support(refs) is False


def test_tier_two_evidence_can_support_a_conclusion():
    ledger = EvidenceLedger()
    news = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="政策发布",
        source_tier=SourceTier.T2_PROFESSIONAL,
        source_name="财联社",
    )
    refs = [EvidenceRef(evidence_id=news.id, support=SupportLevel.SUPPORTS)]
    assert ledger.refs_can_support(refs) is True


def test_weakening_evidence_does_not_count_as_support():
    ledger = EvidenceLedger()
    ev = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="事件",
        source_tier=SourceTier.T1_AUTHORITATIVE,
        source_name="国务院",
    )
    refs = [EvidenceRef(evidence_id=ev.id, support=SupportLevel.WEAKENS)]
    assert ledger.refs_can_support(refs) is False


def test_dangling_evidence_references_are_dropped():
    """模型编造的证据编号必须被丢弃，不能进入结论。"""
    ledger = EvidenceLedger()
    real = ledger.register(
        kind=EvidenceKind.NEWS,
        claim="真实证据",
        source_tier=SourceTier.T2_PROFESSIONAL,
        source_name="财联社",
    )
    refs = [
        EvidenceRef(evidence_id=real.id, support=SupportLevel.SUPPORTS),
        EvidenceRef(evidence_id="E99", support=SupportLevel.SUPPORTS),
    ]
    kept = ledger.filter_valid_refs(refs)
    assert [r.evidence_id for r in kept] == [real.id]


def test_independent_source_count_excludes_market_data():
    ledger = EvidenceLedger()
    ledger.register_market_fact(
        claim="个股区间涨跌 -9.6%", source="fuyao:/prices", provider="fuyao"
    )
    ledger.register(
        kind=EvidenceKind.NEWS,
        claim="政策发布",
        source_tier=SourceTier.T2_PROFESSIONAL,
        source_name="财联社",
        origin_key="ORIGIN-A",
    )
    assert ledger.total_independent_sources() == 1
