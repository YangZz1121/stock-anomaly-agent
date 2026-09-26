"""工具执行器：把规划器选出的动作落到已有 pipeline / reasoner 上。

模型只决定「下一步做什么」。取数、计价、验证、装配仍然走原来的确定性代码。
"""

from __future__ import annotations

import asyncio
from typing import List

from app.agent.actions import ActionName, AgentAction
from app.agent.assemble import (
    apply_guardrails,
    build_assessment,
    build_open_questions,
    build_reasoner,
    build_what_happened,
    compute_metrics,
    merge_refs,
    register_index_fact,
)
from app.agent.drivers import (
    assemble_driver,
    build_checks,
    decide_status,
    enters_stage_three,
    split_evidence_refs,
)
from app.agent.evidence_collect import (
    EvidenceCollector,
    build_evidence_window,
    register_clue_pool,
)
from app.agent.memory import AgentMemory
from app.agent.validation import MarketContext
from app.config import Settings
from app.contracts import ResearchPriority
from app.engine import guardrails, presenter
from app.engine.priority import build_comparison, decide_priority, priority_label, scope_order
from app.engine.verdict import summarize_overall
from app.pipeline import fetch_snapshot, resolve_subject
from app.providers.registry import ProviderBundle
from app.schemas import (
    OverallVerdict,
    ResearchBrief,
    ResearchTrace,
    SubjectSection,
    WhatItMeansSection,
    WhyHappenedSection,
)
from app.timeutil import now_iso
from app.trace import RunRecorder

COUNTER_TERMS = ["澄清", "否认", "不及预期", "证伪", "辟谣"]


class ToolRuntime:
    def __init__(
        self,
        request,
        providers: ProviderBundle,
        settings: Settings,
        recorder: RunRecorder,
    ) -> None:
        self.request = request
        self.providers = providers
        self.settings = settings
        self.recorder = recorder

    async def execute(self, action: AgentAction, memory: AgentMemory) -> None:
        memory.record(action)
        name = action.name
        if name == ActionName.RESOLVE_SUBJECT:
            await self._resolve(memory)
        elif name == ActionName.FETCH_SNAPSHOT:
            await self._snapshot(memory)
        elif name == ActionName.SEARCH_EVIDENCE:
            await self._search(memory, action)
        elif name == ActionName.PROPOSE_DRIVERS:
            await self._propose(memory)
        elif name == ActionName.ASSESS_MECHANISMS:
            await self._assess(memory)
        elif name == ActionName.SEARCH_COUNTER:
            await self._counter(memory)
        elif name == ActionName.BUILD_TRANSMISSIONS:
            await self._transmit(memory)
        elif name == ActionName.ASSEMBLE_BRIEF:
            self._assemble(memory)
        else:  # pragma: no cover
            raise RuntimeError(f"未知动作：{name}")

    async def _resolve(self, memory: AgentMemory) -> None:
        subject = await resolve_subject(
            self.request.query,
            self.request.window,
            self.providers,
            self.settings,
            self.recorder,
            search_key=self.request.search_key,
        )
        memory.subject = subject
        memory.window = (
            self.request.window or subject.parsed.window or subject.resolution.info.window
        )
        memory.mark("subject")

    async def _snapshot(self, memory: AgentMemory) -> None:
        subject = memory.subject
        assert subject is not None
        snapshot = await fetch_snapshot(
            subject, self.providers, self.settings, self.recorder, memory.window
        )
        memory.snapshot = snapshot
        memory.gaps.extend(snapshot.gaps)

        stock = subject.stock
        resolution = subject.resolution
        memory.stock_evidence_id = memory.ledger.register_market_fact(
            claim=(
                f"{stock.name}（{stock.thscode}）在 {resolution.info.actual_start} ~ "
                f"{resolution.info.actual_end} 的区间累计涨跌为 {presenter.pct(snapshot.stock_cum)}"
            ),
            source=f"{self.providers.labels['market']} · 日 K",
            provider=getattr(self.providers.market, "name", "unknown"),
            as_of=resolution.info.actual_end,
            unit="百分比",
            caliber="区间末收盘价 / 区间首日前收盘价 - 1",
            raw_ref={
                "thscode": stock.thscode,
                "bars": [b.model_dump() for b in snapshot.window_bars],
            },
        ).id

        if snapshot.market_cum is not None:
            memory.market_evidence_id = register_index_fact(
                memory.ledger,
                self.providers,
                self.settings.market_index_code,
                self.settings.market_index_name,
                resolution.window_days,
                snapshot.market_cum,
                snapshot.market_selected,
            )
        if snapshot.industry_cum is not None:
            industry = subject.industry
            memory.industry_evidence_id = register_index_fact(
                memory.ledger,
                self.providers,
                industry.index_code or "",
                industry.index_name or industry.index_code or "",
                resolution.window_days,
                snapshot.industry_cum,
                snapshot.industry_selected,
            )

        memory.comparison = build_comparison(
            stock_pct=snapshot.stock_cum,
            industry_pct=snapshot.industry_cum,
            market_pct=snapshot.market_cum,
            window_label=resolution.info.label,
            stock_evidence_id=memory.stock_evidence_id,
            industry_evidence_id=memory.industry_evidence_id,
            market_evidence_id=memory.market_evidence_id,
        )

        self.recorder.step("priority", "running")
        priority, priority_reason, priority_gap = decide_priority(
            stock_pct=snapshot.stock_cum,
            industry_pct=snapshot.industry_cum,
            market_pct=snapshot.market_cum,
            divergence_threshold=self.settings.divergence_threshold_pct,
            industry_move_threshold=self.settings.industry_move_threshold_pct,
            market_move_threshold=self.settings.market_move_threshold_pct,
        )
        if priority_gap:
            memory.gaps.append(priority_gap)
        memory.priority = priority
        memory.priority_reason = priority_reason
        memory.scopes = scope_order(priority)
        self.recorder.step("priority", "done", priority_label(priority))

        memory.what_happened = build_what_happened(
            snapshot.daily_profile,
            snapshot.window_profile,
            memory.comparison,
            subject.industry,
            snapshot.window_bars,
            stock,
            resolution.info.label,
            memory.stock_evidence_id,
            memory.gaps,
        )
        memory.mark("snapshot")

    async def _search(self, memory: AgentMemory, action: AgentAction) -> None:
        subject = memory.subject
        snapshot = memory.snapshot
        assert subject is not None and snapshot is not None
        if memory.evidence_window is None:
            memory.clue_pool = register_clue_pool(
                memory.ledger,
                snapshot.clue_reasons,
                getattr(self.providers.market, "name", "unknown"),
            )
            memory.evidence_window = build_evidence_window(
                subject.resolution.window_days,
                subject.trading_days,
                self.settings.evidence_extended_window_days,
            )
        collector = EvidenceCollector(self.providers.evidence, memory.ledger, self.recorder)
        scopes = action.scopes or memory.scopes
        if not memory.has("retrieved"):
            self.recorder.step("retrieve", "running")
            memory.collection = await collector.collect(
                window=memory.evidence_window,
                scopes=scopes,
                stock_name=subject.stock.name,
                industry_name=subject.industry.index_name,
                clue_keywords=memory.clue_pool,
            )
            memory.gaps.extend(memory.collection.gaps)
            memory.failed_scopes = {
                g.field.split(".", 1)[-1]
                for g in memory.collection.gaps
                if g.field.startswith("evidence.")
            }
            self.recorder.step(
                "retrieve",
                "done" if memory.collection.clusters else "failed",
                f"归并出 {len(memory.collection.clusters)} 个事件聚类",
            )
            memory.ctx = _market_context(memory, self.settings)
            memory.reasoner = build_reasoner(self.providers, self.recorder)
            memory.mark("retrieved")
            return

        self.recorder.step("retrieve", "running")
        before = {g.field for g in memory.collection.gaps} if memory.collection else set()
        for scope in scopes:
            memory.collection = await collector.collect_scope(
                result=memory.collection,
                window=memory.evidence_window,
                scope=scope,
                stock_name=subject.stock.name,
                industry_name=subject.industry.index_name,
                extra_terms=action.extra_terms or memory.clue_pool,
            )
            memory.retried_scopes.add(scope)
        memory.extra_searches += 1
        if memory.collection:
            for gap in memory.collection.gaps:
                if gap.field not in before and gap not in memory.gaps:
                    memory.gaps.append(gap)
            for scope in scopes:
                added_fail = any(
                    g.field == f"evidence.{scope}" and g.field not in before
                    for g in memory.collection.gaps
                )
                if not added_fail:
                    memory.failed_scopes.discard(scope)
        self.recorder.step(
            "retrieve",
            "done",
            f"补充检索 {', '.join(scopes)}",
        )
        memory.ctx = _market_context(memory, self.settings)

    async def _propose(self, memory: AgentMemory) -> None:
        assert memory.collection is not None and memory.ctx is not None
        self.recorder.step("drivers", "running")
        memory.reasoner = memory.reasoner or build_reasoner(self.providers, self.recorder)
        memory.proposals = await memory.reasoner.propose_drivers(
            memory.collection.clusters, memory.ctx, memory.scopes
        )
        cluster_by_id = {c.cluster_id: c for c in memory.collection.clusters}
        memory.pairs = [
            (proposal, cluster_by_id[proposal.cluster_id])
            for proposal in memory.proposals
            if proposal.cluster_id in cluster_by_id
        ]
        memory.mark("proposed")

    async def _assess(self, memory: AgentMemory) -> None:
        assert memory.collection is not None and memory.ctx is not None
        memory.reasoner = memory.reasoner or build_reasoner(self.providers, self.recorder)
        memory.mechanisms = await asyncio.gather(
            *[
                memory.reasoner.assess_mechanism(
                    proposal, cluster, memory.collection.clusters, memory.ctx
                )
                for proposal, cluster in memory.pairs
            ]
        )
        memory.drivers = []
        memory.drafts = {}
        for i, ((proposal, cluster), mechanism) in enumerate(
            zip(memory.pairs, memory.mechanisms), start=1
        ):
            checks = build_checks(
                proposal, cluster, memory.ctx, mechanism.result, mechanism.reasoning
            )
            supporting, contradicting = split_evidence_refs(
                cluster, checks, [], memory.ledger
            )
            status, unresolved = decide_status(checks, supporting, memory.ledger)
            driver = assemble_driver(
                i, proposal, cluster, checks, supporting, contradicting, status, unresolved
            )
            memory.drivers.append(driver)
            memory.drafts[driver.id] = (proposal, cluster, checks, mechanism)
        self.recorder.step("drivers", "done", f"形成 {len(memory.drivers)} 个候选驱动因素")
        memory.mark("assessed")

    async def _counter(self, memory: AgentMemory) -> None:
        self.recorder.step("counter", "running")
        subject = memory.subject
        if (
            subject is not None
            and memory.collection is not None
            and memory.evidence_window is not None
            and memory.extra_searches < self.settings.agent_extra_search_limit
        ):
            collector = EvidenceCollector(
                self.providers.evidence, memory.ledger, self.recorder
            )
            for scope in memory.scopes or ["company"]:
                memory.collection = await collector.collect_scope(
                    result=memory.collection,
                    window=memory.evidence_window,
                    scope=scope,
                    stock_name=subject.stock.name,
                    industry_name=subject.industry.index_name,
                    extra_terms=COUNTER_TERMS,
                )
            memory.extra_searches += 1
        self.recorder.step(
            "counter",
            "done",
            f"{sum(len(d.contradicting_refs) for d in memory.drivers)} 条反向证据",
        )
        memory.mark("counter")

    async def _transmit(self, memory: AgentMemory) -> None:
        assert memory.collection is not None and memory.ctx is not None
        self.recorder.step("exposure", "running")
        self.recorder.step("transmission", "running")
        memory.reasoner = memory.reasoner or build_reasoner(self.providers, self.recorder)
        stage_three = [driver for driver in memory.drivers if enters_stage_three(driver)]
        transmissions = await asyncio.gather(
            *[
                memory.reasoner.build_transmission(
                    memory.drafts[driver.id][0],
                    memory.drafts[driver.id][1],
                    memory.collection.clusters,
                    memory.ctx,
                    memory.ledger,
                )
                for driver in stage_three
            ]
        )
        assessed_ids: List[str] = []
        for driver, draft in zip(stage_three, transmissions):
            proposal, cluster, checks, mechanism = memory.drafts[driver.id]
            extra_supporting, extra_contra = split_evidence_refs(
                cluster, checks, draft.counter_evidence_ids, memory.ledger
            )
            driver.contradicting_refs = merge_refs(
                driver.contradicting_refs, extra_contra
            )
            driver.assessment = build_assessment(
                driver, cluster, checks, draft, memory.ledger
            )
            assessed_ids.append(driver.id)
        memory._assessed_ids = assessed_ids  # type: ignore[attr-defined]
        self.recorder.step("exposure", "done")
        self.recorder.step("transmission", "done", f"{len(assessed_ids)} 个驱动因素完成传导分析")
        memory.mark("transmitted")

    def _assemble(self, memory: AgentMemory) -> None:
        subject = memory.subject
        snapshot = memory.snapshot
        assert subject is not None and snapshot is not None
        self.recorder.step("verdict", "running")
        overall_dir, overall_display, overall_reason, can_summarize = summarize_overall(
            [
                (d.assessment.direction, d.assessment.strength)
                for d in memory.drivers
                if d.assessment is not None
            ]
        )
        assessed_ids = getattr(memory, "_assessed_ids", [])
        why = WhyHappenedSection(
            priority=memory.priority or ResearchPriority.COMPANY_FIRST,
            priority_label=priority_label(memory.priority or ResearchPriority.COMPANY_FIRST),
            priority_reason=memory.priority_reason,
            drivers=memory.drivers,
            clue_pool=memory.clue_pool,
            evidence_window_note=(
                memory.evidence_window.describe() if memory.evidence_window else ""
            ),
            gaps=[g for g in memory.gaps if g.field.startswith("evidence")],
        )
        what_it_means = WhatItMeansSection(
            assessments=assessed_ids,
            overall=OverallVerdict(
                direction=overall_dir,
                display=overall_display,
                reason=overall_reason,
                can_summarize=can_summarize,
            ),
        )
        open_questions = build_open_questions(
            memory.drivers, memory.gaps, subject.industry
        )
        metrics = compute_metrics(
            memory.drivers,
            memory.ledger,
            self.recorder,
            self.providers,
            agent_steps=len(memory.actions),
        )
        reasoner = memory.reasoner
        brief = ResearchBrief(
            run_id=memory.run_id,
            created_at=now_iso(),
            subject=SubjectSection(
                stock=subject.stock,
                user_question=self.request.query,
                window=subject.resolution.info,
                resolved_note=subject.resolution.info.remap_note,
            ),
            what_happened=memory.what_happened,
            why_happened=why,
            what_it_means=what_it_means,
            open_questions=open_questions,
            evidence=memory.ledger.all(),
            metrics=metrics,
            trace=ResearchTrace(
                tool_calls=self.recorder.tool_calls,
                llm_calls=self.recorder.llm_calls,
                providers=self.providers.labels,
                plan=memory.plan,
                agent_steps=memory.actions,
            ),
            disclaimers=guardrails.DISCLAIMERS
            + guardrails.degraded_notice(
                self.providers.labels, getattr(reasoner, "disclaimer", "")
            ),
        )
        apply_guardrails(brief, self.recorder)
        brief.metrics.time_to_verifiable_insight_ms = self.recorder.elapsed_ms()
        self.recorder.step("verdict", "done")
        memory.brief = brief
        memory.done = True
        memory.mark("assembled")


def _market_context(memory: AgentMemory, settings: Settings) -> MarketContext:
    subject = memory.subject
    snapshot = memory.snapshot
    comparison = memory.comparison
    assert subject is not None and snapshot is not None and comparison is not None
    return MarketContext(
        window=memory.window,
        window_days=subject.resolution.window_days,
        evidence_window=memory.evidence_window,
        stock_pct=snapshot.stock_cum,
        industry_pct=snapshot.industry_cum,
        market_pct=snapshot.market_cum,
        stock_vs_industry=comparison.stock_vs_industry.value if comparison.stock_vs_industry else None,
        industry_vs_market=comparison.industry_vs_market.value if comparison.industry_vs_market else None,
        gap_pct=snapshot.daily_profile.gap_pct if snapshot.daily_profile else None,
        post_open_pct=snapshot.daily_profile.post_open_pct if snapshot.daily_profile else None,
        has_minute_data=False,
        divergence_threshold=settings.divergence_threshold_pct,
        industry_move_threshold=settings.industry_move_threshold_pct,
        industry_name=subject.industry.index_name,
        stock_name=subject.stock.name,
    )
