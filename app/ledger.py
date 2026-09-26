"""证据台账。

产品里所有能被引用的事实都必须先登记到台账，拿到一个 ``E<n>`` 编号。
结论只持有编号，不持有正文，这样才能保证每条结论都能反查回原始字段。

台账同时负责两件规划文档里强调的事：

* 事件聚类（9.1）—— 国务院原文 + 多家转载属于同一个事件，不能算多个事件。
* 独立来源判定（9.4）—— 转载同一信源的 10 篇报道，仍然只算 1 个独立来源。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.contracts import (
    Evidence,
    EvidenceKind,
    EvidenceRef,
    SourceTier,
    SupportLevel,
)

_PUNCT = re.compile(r"[\s，。、；：！？,.;:!?\"'“”‘’()（）\[\]【】《》\-—_/\\|]+")


def normalize_claim(text: str) -> str:
    """把标题归一化成可比较的形式，用于同一事件的转载识别。"""
    return _PUNCT.sub("", (text or "").strip().lower())


class EvidenceLedger:
    """一次研究运行内的证据台账。"""

    def __init__(self) -> None:
        self._items: Dict[str, Evidence] = {}
        self._order: List[str] = []
        self._seq = 0
        # normalized claim -> evidence id，用于识别重复登记
        self._claim_index: Dict[str, str] = {}
        # cluster_id -> 该聚类下出现过的 origin_key 集合
        self._cluster_origins: Dict[str, set] = {}

    def claim_id(self, claim: str) -> Optional[str]:
        """同一 claim 是否已经登记过。二次检索用来跳过重复事件。"""
        return self._claim_index.get(normalize_claim(claim))

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        kind: EvidenceKind,
        claim: str,
        source_tier: SourceTier,
        source_name: str,
        source_url: Optional[str] = None,
        published_at: Optional[str] = None,
        provider: str = "mock",
        raw_ref: Optional[Dict[str, Any]] = None,
        cluster_id: Optional[str] = None,
        origin_key: Optional[str] = None,
        unit: Optional[str] = None,
        caliber: Optional[str] = None,
    ) -> Evidence:
        """登记一条证据。同一 claim 重复登记时复用已有编号。"""
        existing_id = self.claim_id(claim)
        if existing_id is not None:
            existing = self._items[existing_id]
            # 重复登记时，保留可信度更高的来源
            if _tier_rank(source_tier) < _tier_rank(existing.source_tier):
                existing.source_tier = source_tier
                existing.source_name = source_name
                existing.source_url = source_url or existing.source_url
            self._track_origin(existing.cluster_id, origin_key)
            return existing

        self._seq += 1
        eid = f"E{self._seq}"
        norm = normalize_claim(claim)
        resolved_cluster = cluster_id or f"C{self._seq}"
        evidence = Evidence(
            id=eid,
            kind=kind,
            claim=claim,
            source_tier=source_tier,
            source_name=source_name,
            source_url=source_url,
            published_at=published_at,
            provider=provider,
            raw_ref=raw_ref,
            cluster_id=resolved_cluster,
            origin_key=origin_key or source_name,
            unit=unit,
            caliber=caliber,
        )
        self._items[eid] = evidence
        self._order.append(eid)
        self._claim_index[norm] = eid
        self._track_origin(resolved_cluster, evidence.origin_key)
        return evidence

    def register_market_fact(
        self,
        *,
        claim: str,
        source: str,
        provider: str,
        as_of: Optional[str] = None,
        raw_ref: Optional[Dict[str, Any]] = None,
        unit: Optional[str] = None,
        caliber: Optional[str] = None,
    ) -> Evidence:
        """登记一条来自行情接口的结构化事实。"""
        return self.register(
            kind=EvidenceKind.MARKET_DATA,
            claim=claim,
            source_tier=SourceTier.MARKET_DATA,
            source_name=source,
            published_at=as_of,
            provider=provider,
            raw_ref=raw_ref,
            unit=unit,
            caliber=caliber,
        )

    def _track_origin(self, cluster_id: Optional[str], origin_key: Optional[str]) -> None:
        if not cluster_id:
            return
        bucket = self._cluster_origins.setdefault(cluster_id, set())
        if origin_key:
            bucket.add(origin_key)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, evidence_id: str) -> Optional[Evidence]:
        return self._items.get(evidence_id)

    def all(self) -> List[Evidence]:
        return [self._items[i] for i in self._order]

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._items

    def clusters(self) -> Dict[str, List[Evidence]]:
        out: Dict[str, List[Evidence]] = {}
        for eid in self._order:
            ev = self._items[eid]
            out.setdefault(ev.cluster_id or eid, []).append(ev)
        return out

    def independent_sources(self, cluster_id: str) -> int:
        """该事件聚类下有多少个真正独立的信源。"""
        return len(self._cluster_origins.get(cluster_id, set()))

    def total_independent_sources(self) -> int:
        seen = set()
        for ev in self.all():
            if ev.kind == EvidenceKind.MARKET_DATA:
                continue
            if ev.origin_key:
                seen.add(ev.origin_key)
        return len(seen)

    # ------------------------------------------------------------------
    # 结论支撑判定
    # ------------------------------------------------------------------

    def refs_can_support(self, refs: List[EvidenceRef]) -> bool:
        """这些引用里是否存在至少一条「可以支撑结论」的证据。

        四级来源（自媒体 / 匿名 / 无原始出处）只能当检索线索，
        即便它态度上支持该论点，也不计入。
        """
        for ref in refs:
            if ref.support not in (SupportLevel.SUPPORTS, SupportLevel.WEAKLY_SUPPORTS):
                continue
            ev = self.get(ref.evidence_id)
            if ev is not None and ev.can_support_conclusion:
                return True
        return False

    def resolve(self, refs: List[EvidenceRef]) -> List[Evidence]:
        out = []
        for ref in refs:
            ev = self.get(ref.evidence_id)
            if ev is not None:
                out.append(ev)
        return out

    def filter_valid_refs(self, refs: List[EvidenceRef]) -> List[EvidenceRef]:
        """丢弃引用了不存在证据编号的悬空引用。

        这是防 LLM 幻觉的关键一环：模型经常编造 ``E99`` 这样的编号。
        """
        return [r for r in refs if r.evidence_id in self._items]


_TIER_ORDER = {
    SourceTier.T1_AUTHORITATIVE: 0,
    SourceTier.MARKET_DATA: 0,
    SourceTier.T2_PROFESSIONAL: 1,
    SourceTier.T3_GENERAL: 2,
    SourceTier.T4_UNVERIFIED: 3,
}


def _tier_rank(tier: SourceTier) -> int:
    return _TIER_ORDER.get(tier, 9)
