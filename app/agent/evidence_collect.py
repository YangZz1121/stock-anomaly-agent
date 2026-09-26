"""证据检索与登记。

负责把"去哪里找、找什么、找到的算不算数"这件事做完，输出结构化的
事件聚类给推理层。推理层看到的不是新闻列表，而是已经判过级、聚过类、
数过独立来源的事件。

双层证据窗口（规划文档第 8 章）：
* 核心窗口 = 研究窗口开始前 1 个交易日 → 研究窗口结束日，优先级最高；
* 扩展窗口 = 核心窗口再向前 7 个自然日，用于捕捉周末发布和持续发酵的信息。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.contracts import DataGap, EvidenceKind, SourceTier
from app.engine.reason_tags import normalize_reason_tags
from app.engine.source_tier import (
    classify_kind,
    classify_tier,
    cluster_events,
    count_independent_sources,
)
from app.ledger import EvidenceLedger
from app.providers.base import RawEvent
from app.timeutil import is_within, shift_days
from app.trace import RunRecorder

SCOPE_LABELS = {"market": "市场 / 宏观", "industry": "行业", "company": "公司"}

# 每个范围的基础检索词，与标的名 / 行业名拼接后送进检索层
SCOPE_TERMS = {
    "market": "市场 风险偏好 宏观 货币政策 海外 汇率 大宗商品",
    "industry": "行业 政策 供需 价格 竞争 监管 上下游",
    "company": "公告 业绩 订单 产品 经营 管理层",
}


class EvidenceWindow(BaseModel):
    core_start: str
    core_end: str
    extended_start: str

    def contains_core(self, day: Optional[str]) -> bool:
        return is_within((day or "")[:10], self.core_start, self.core_end)

    def contains_extended(self, day: Optional[str]) -> bool:
        return is_within((day or "")[:10], self.extended_start, self.core_end)

    def describe(self) -> str:
        return (
            f"核心证据窗口 {self.core_start} ~ {self.core_end}（优先级最高）；"
            f"扩展窗口回溯至 {self.extended_start}，用于捕捉周末发布与持续发酵的信息。"
            f"商品价格、汇率等持续性变量只要在研究窗口内仍然有效，不受扩展窗口硬切断。"
        )


class ClusterInfo(BaseModel):
    """一个事件聚类。这是推理层能看到的最小信息单元。"""

    cluster_id: str
    title: str
    summary: str
    scope: str
    evidence_ids: List[str] = Field(default_factory=list)
    independent_sources: int = 0
    best_tier: SourceTier = SourceTier.T3_GENERAL
    earliest_published: Optional[str] = None
    latest_published: Optional[str] = None
    in_core_window: bool = False
    report_count: int = 0
    kinds: List[str] = Field(default_factory=list)

    @property
    def can_support_conclusion(self) -> bool:
        return self.best_tier != SourceTier.T4_UNVERIFIED

    @property
    def is_background(self) -> bool:
        """是否属于背景事实而非事件。

        年报、招股说明书这类正式披露是慢变量信息（规划 13.3），用来确认
        公司主营业务、市场和原材料等业务基础，不能当成解释本次价格变化的
        事件。因此它们只作为公司暴露的证据，不进入候选驱动因素。
        """
        return bool(self.kinds) and set(self.kinds) <= {EvidenceKind.FILING.value}


class CollectionResult(BaseModel):
    window: EvidenceWindow
    clusters: List[ClusterInfo] = Field(default_factory=list)
    clue_pool: List[str] = Field(default_factory=list)
    gaps: List[DataGap] = Field(default_factory=list)
    scopes_searched: List[str] = Field(default_factory=list)

    def by_scope(self, scope: str) -> List[ClusterInfo]:
        return [c for c in self.clusters if c.scope == scope]


def build_evidence_window(
    window_days: List[str], trading_days: List[str], extended_days: int
) -> EvidenceWindow:
    start = window_days[0]
    end = window_days[-1]
    idx = trading_days.index(start) if start in trading_days else 0
    core_start = trading_days[idx - 1] if idx > 0 else start
    return EvidenceWindow(
        core_start=core_start,
        core_end=end,
        extended_start=shift_days(core_start, -extended_days),
    )


class EvidenceCollector:
    def __init__(self, provider: Any, ledger: EvidenceLedger, recorder: RunRecorder):
        self._provider = provider
        self._ledger = ledger
        self._recorder = recorder

    async def collect(
        self,
        *,
        window: EvidenceWindow,
        scopes: List[str],
        stock_name: str,
        industry_name: Optional[str],
        clue_keywords: List[str],
    ) -> CollectionResult:
        result = CollectionResult(window=window)
        all_events: List[RawEvent] = []

        async def _search(scope: str):
            query = _build_query(scope, stock_name, industry_name, clue_keywords)
            started = time.perf_counter()
            res = await self._provider.search_events(
                query=query,
                start_date=window.extended_start,
                end_date=window.core_end,
                scope=scope,
                limit=20,
                context={
                    "stock_name": stock_name,
                    "industry_name": industry_name,
                    "required_terms": _required_terms(scope, stock_name, industry_name),
                },
            )
            return scope, query, res, int((time.perf_counter() - started) * 1000)

        searched = await asyncio.gather(*[_search(scope) for scope in scopes])
        for scope, query, res, latency_ms in searched:
            self._recorder.record_tool(
                "search_events",
                {
                    "scope": scope,
                    "query": query,
                    "start": window.extended_start,
                    "end": window.core_end,
                },
                res.status,
                res.provider,
                latency_ms=latency_ms,
                note=res.note,
            )
            result.scopes_searched.append(scope)

            if not res.ok:
                result.gaps.append(
                    DataGap(
                        field=f"evidence.{scope}",
                        reason=f"{SCOPE_LABELS.get(scope, scope)}范围的资讯检索未返回结果：{res.note}",
                        impact=f"无法评估{SCOPE_LABELS.get(scope, scope)}层面的候选驱动因素",
                        source=res.source,
                    )
                )
                continue

            for event in res.value or []:
                event.scope = event.scope or scope
                all_events.append(event)

        result.clusters = self._register(all_events, window)
        result.clue_pool = clue_keywords
        return result

    async def collect_scope(
        self,
        *,
        result: CollectionResult,
        window: EvidenceWindow,
        scope: str,
        stock_name: str,
        industry_name: Optional[str],
        extra_terms: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        pass_label: str = "additional",
    ) -> CollectionResult:
        """对单个范围再检索一次，新事件合并进已有结果。

        已登记过的 claim 会被跳过，避免二次检索把同一条新闻再变成新驱动因素。
        start_date 用于公司暴露补证：年报等慢变量可以超出常规资讯窗口。
        """
        query = _build_query(scope, stock_name, industry_name, extra_terms or [])
        started = time.perf_counter()
        start = start_date or window.extended_start
        res = await self._provider.search_events(
            query=query,
            start_date=start,
            end_date=window.core_end,
            scope=scope,
            limit=20,
            context={
                "stock_name": stock_name,
                "industry_name": industry_name,
                "required_terms": _required_terms(scope, stock_name, industry_name),
            },
        )
        self._recorder.record_tool(
            "search_events",
            {
                "scope": scope,
                "query": query,
                "start": start,
                "end": window.core_end,
                "pass": pass_label,
            },
            res.status,
            res.provider,
            latency_ms=int((time.perf_counter() - started) * 1000),
            note=res.note,
        )
        if scope not in result.scopes_searched:
            result.scopes_searched.append(scope)
        if not res.ok:
            result.gaps.append(
                DataGap(
                    field=f"evidence.{scope}",
                    reason=f"{SCOPE_LABELS.get(scope, scope)}范围的补充检索未返回结果：{res.note}",
                    impact=f"无法补充{SCOPE_LABELS.get(scope, scope)}层面的证据",
                    source=res.source,
                )
            )
            return result

        fresh = []
        for event in res.value or []:
            event.scope = event.scope or scope
            claim = f"{event.title}｜{event.summary}" if event.summary else event.title
            if self._ledger.claim_id(claim) is None:
                fresh.append(event)
        if fresh:
            result.clusters.extend(self._register(fresh, window))
            result.clusters.sort(
                key=lambda c: (
                    not c.in_core_window,
                    _tier_rank(c.best_tier),
                    -c.independent_sources,
                )
            )
        return result

    # ------------------------------------------------------------------

    def _register(
        self, events: List[RawEvent], window: EvidenceWindow
    ) -> List[ClusterInfo]:
        # 先按事件聚类，再逐条登记，保证同一事件的多篇报道共享 cluster_id
        grouped = cluster_events(events)
        clusters: List[ClusterInfo] = []

        for cluster_id, members in grouped.items():
            members = sorted(members, key=lambda e: (e.published_at or "", e.event_id))
            evidence_ids: List[str] = []
            kinds: List[str] = []
            best_tier = SourceTier.T4_UNVERIFIED

            for event in members:
                tier = classify_tier(event.source_name, event.source_tier_hint)
                if _tier_rank(tier) < _tier_rank(best_tier):
                    best_tier = tier
                kind = classify_kind(event)
                kinds.append(kind.value)
                ev = self._ledger.register(
                    kind=kind,
                    claim=f"{event.title}｜{event.summary}" if event.summary else event.title,
                    source_tier=tier,
                    source_name=event.source_name,
                    source_url=event.source_url,
                    published_at=event.published_at,
                    provider=getattr(self._provider, "name", "unknown"),
                    cluster_id=cluster_id,
                    origin_key=event.origin_key or event.source_name,
                    raw_ref={
                        "event_id": event.event_id,
                        "title": event.title,
                        "summary": event.summary,
                        "scope": event.scope,
                        "in_core_window": window.contains_core(event.published_at),
                        **(event.raw or {}),
                    },
                )
                evidence_ids.append(ev.id)

            published = [e.published_at for e in members if e.published_at]
            head = members[0]
            clusters.append(
                ClusterInfo(
                    cluster_id=cluster_id,
                    title=head.title,
                    summary=head.summary,
                    scope=head.scope or "company",
                    evidence_ids=evidence_ids,
                    independent_sources=count_independent_sources(members),
                    best_tier=best_tier,
                    earliest_published=min(published) if published else None,
                    latest_published=max(published) if published else None,
                    in_core_window=any(
                        window.contains_core(e.published_at) for e in members
                    ),
                    report_count=len(members),
                    kinds=sorted(set(kinds)),
                )
            )

        # 核心窗口内、来源等级高、独立信源多的事件排在前面
        clusters.sort(
            key=lambda c: (
                not c.in_core_window,
                _tier_rank(c.best_tier),
                -c.independent_sources,
            )
        )
        return clusters


def register_clue_pool(
    ledger: EvidenceLedger, reasons, provider_name: str
) -> List[str]:
    """把第三方当日异动解读登记为线索，等级压到最低。

    这类内容是别人的结论而不是原始证据，因此即使它出现在权威数据接口里，
    也只能作为检索线索使用，不允许独立支撑本产品的结论。
    """
    clues: List[str] = []
    for reason in reasons or []:
        ledger.register(
            kind=EvidenceKind.ANOMALY_TAG,
            claim=f"第三方异动解读：{reason.content}",
            source_tier=SourceTier.T4_UNVERIFIED,
            source_name="第三方异动解读",
            provider=provider_name,
            raw_ref={
                "thscode": reason.thscode,
                "tag_name": reason.tag_name,
                "keywords": reason.keywords,
            },
        )
        clues.extend(reason.keywords or [])
        if reason.content:
            clues.append(reason.content[:40])
    tagged = normalize_reason_tags(reasons)
    # 标签优先，再拼登记过程中收集的关键词，保序去重
    seen = set()
    out = []
    for c in tagged + clues:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _required_terms(
    scope: str, stock_name: str, industry_name: Optional[str]
) -> List[str]:
    """该范围的检索结果必须与哪些实体相关。

    市场 / 宏观事件本来就不针对某家公司，因此不设实体门槛；
    行业和公司范围如果不设门槛，通用检索词会把无关板块的新闻全部召回。
    """
    if scope == "company":
        return [stock_name] if stock_name else []
    if scope == "industry":
        return [t for t in (industry_name, stock_name) if t]
    return []


def _build_query(
    scope: str, stock_name: str, industry_name: Optional[str], clues: List[str]
) -> str:
    parts: List[str] = []
    if scope == "company":
        parts.append(stock_name)
    elif scope == "industry" and industry_name:
        parts.append(industry_name)
    parts.append(SCOPE_TERMS.get(scope, ""))
    parts.extend(clues[:6])
    return " ".join(p for p in parts if p).strip()


_TIER_ORDER = {
    SourceTier.T1_AUTHORITATIVE: 0,
    SourceTier.MARKET_DATA: 0,
    SourceTier.T2_PROFESSIONAL: 1,
    SourceTier.T3_GENERAL: 2,
    SourceTier.T4_UNVERIFIED: 3,
}


def _tier_rank(tier: SourceTier) -> int:
    return _TIER_ORDER.get(tier, 9)
