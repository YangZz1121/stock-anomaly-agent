"""研究编排器：把三个阶段串成一条完整链路。

编排器只负责流程与装配，所有判断逻辑都在 ``engine``（确定性）和
``agent``（不确定性）两侧。任何一步拿不到数据，都会变成 Brief 里一条
显式的缺口，而不是被跳过或被默认值掩盖。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.agent.drivers import (
    assemble_driver,
    build_checks,
    decide_status,
    enters_stage_three,
    split_evidence_refs,
)
from app.agent.evidence_collect import (
    ClusterInfo,
    CollectionResult,
    EvidenceCollector,
    build_evidence_window,
    register_clue_pool,
)
from app.agent.llm_reasoner import LLMReasoner
from app.agent.reasoner import HeuristicReasoner
from app.agent.validation import MarketContext
from app.config import Settings
from app.contracts import (
    CATEGORY_LABELS,
    EXPOSURE_LABELS,
    CheckResult,
    DataGap,
    DriverStatus,
    EvidenceRef,
    EvidenceStrength,
    FetchStatus,
    ImpactDirection,
    PATTERN_LABELS,
    PricePattern,
    ResearchPriority,
    ResearchWindow,
    SourceTier,
    SupportLevel,
)
from app.engine import guardrails, presenter
from app.engine.price_profile import describe_close_position, describe_turnover
from app.engine.priority import build_comparison, decide_priority, priority_label, scope_order
from app.errors import NeedsWindowChoice, ResearchError
from app.engine.verdict import (
    StrengthInput,
    apply_structural_gate,
    build_display_verdict,
    compute_evidence_strength,
    summarize_overall,
)
from app.ledger import EvidenceLedger
from app.engine.resolver import parse_query, resolve_window
from app.pipeline import fetch_snapshot, resolve_subject
from app.pipeline.snapshot import _build_price_profiles, _fetch_bars
from app.pipeline.subject import fetch_trading_days, resolve_stock
from app.providers.base import Bar
from app.providers.registry import ProviderBundle
from app.schemas import (
    Driver,
    FundamentalAssessment,
    IndustryInfo,
    OpenQuestionsSection,
    OverallVerdict,
    PricePoint,
    ResearchBrief,
    ResearchTrace,
    RunMetrics,
    StockRef,
    SubjectSection,
    TransmissionStep,
    WhatHappenedSection,
    WhatItMeansSection,
    WhyHappenedSection,
)
from app.timeutil import now_iso
from app.trace import RunRecorder


class ResearchRequest(BaseModel):
    query: str
    window: Optional[ResearchWindow] = None
    search_key: Optional[str] = None


async def run_research(
    request: ResearchRequest,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ResearchBrief:
    ledger = EvidenceLedger()
    run_id = recorder.run_id

    subject = await resolve_subject(
        request.query,
        request.window,
        providers,
        settings,
        recorder,
        search_key=request.search_key,
    )
    stock = subject.stock
    industry = subject.industry
    resolution = subject.resolution
    window = (
        request.window or subject.parsed.window or subject.resolution.info.window
    )

    snapshot = await fetch_snapshot(subject, providers, settings, recorder, window)
    gaps: List[DataGap] = list(snapshot.gaps)
    window_bars = snapshot.window_bars
    stock_cum = snapshot.stock_cum
    daily_profile = snapshot.daily_profile
    window_profile = snapshot.window_profile
    market_cum = snapshot.market_cum
    industry_cum = snapshot.industry_cum
    clue_reasons = snapshot.clue_reasons

    stock_evidence = ledger.register_market_fact(
        claim=(
            f"{stock.name}（{stock.thscode}）在 {resolution.info.actual_start} ~ "
            f"{resolution.info.actual_end} 的区间累计涨跌为 {presenter.pct(stock_cum)}"
        ),
        source=f"{providers.labels['market']} · 日 K",
        provider=getattr(providers.market, "name", "unknown"),
        as_of=resolution.info.actual_end,
        unit="百分比",
        caliber="区间末收盘价 / 区间首日前收盘价 - 1",
        raw_ref={
            "thscode": stock.thscode,
            "bars": [b.model_dump() for b in window_bars],
        },
    )

    market_evidence_id: Optional[str] = None
    if market_cum is not None:
        market_evidence_id = _register_index_fact(
            ledger,
            providers,
            settings.market_index_code,
            settings.market_index_name,
            resolution.window_days,
            market_cum,
            snapshot.market_selected,
        )
    industry_evidence_id: Optional[str] = None
    if industry_cum is not None:
        industry_evidence_id = _register_index_fact(
            ledger,
            providers,
            industry.index_code or "",
            industry.index_name or industry.index_code or "",
            resolution.window_days,
            industry_cum,
            snapshot.industry_selected,
        )

    comparison = build_comparison(
        stock_pct=stock_cum,
        industry_pct=industry_cum,
        market_pct=market_cum,
        window_label=resolution.info.label,
        stock_evidence_id=stock_evidence.id,
        industry_evidence_id=industry_evidence_id,
        market_evidence_id=market_evidence_id,
    )

    recorder.step("priority", "running")
    priority, priority_reason, priority_gap = decide_priority(
        stock_pct=stock_cum,
        industry_pct=industry_cum,
        market_pct=market_cum,
        divergence_threshold=settings.divergence_threshold_pct,
        industry_move_threshold=settings.industry_move_threshold_pct,
        market_move_threshold=settings.market_move_threshold_pct,
    )
    if priority_gap:
        gaps.append(priority_gap)
    scopes = scope_order(priority)
    recorder.step("priority", "done", priority_label(priority))

    what_happened = _build_what_happened(
        daily_profile,
        window_profile,
        comparison,
        industry,
        window_bars,
        stock,
        resolution.info.label,
        stock_evidence.id,
        gaps,
    )

    # ---------------- 第二阶段：为什么发生 ----------------
    recorder.step("retrieve", "running")
    clue_pool = register_clue_pool(
        ledger, clue_reasons, getattr(providers.market, "name", "unknown")
    )
    evidence_window = build_evidence_window(
        resolution.window_days, subject.trading_days, settings.evidence_extended_window_days
    )
    collector = EvidenceCollector(providers.evidence, ledger, recorder)
    collection = await collector.collect(
        window=evidence_window,
        scopes=scopes,
        stock_name=stock.name,
        industry_name=industry.index_name,
        clue_keywords=clue_pool,
    )
    gaps.extend(collection.gaps)
    recorder.step(
        "retrieve",
        "done" if collection.clusters else "failed",
        f"归并出 {len(collection.clusters)} 个事件聚类",
    )

    ctx = MarketContext(
        window=window,
        window_days=resolution.window_days,
        evidence_window=evidence_window,
        stock_pct=stock_cum,
        industry_pct=industry_cum,
        market_pct=market_cum,
        stock_vs_industry=comparison.stock_vs_industry.value if comparison.stock_vs_industry else None,
        industry_vs_market=comparison.industry_vs_market.value if comparison.industry_vs_market else None,
        gap_pct=daily_profile.gap_pct if daily_profile else None,
        post_open_pct=daily_profile.post_open_pct if daily_profile else None,
        has_minute_data=False,  # 当前数据源只提供日线
        divergence_threshold=settings.divergence_threshold_pct,
        industry_move_threshold=settings.industry_move_threshold_pct,
        industry_name=industry.index_name,
        stock_name=stock.name,
    )

    reasoner = _build_reasoner(providers, recorder)

    recorder.step("drivers", "running")
    proposals = await reasoner.propose_drivers(collection.clusters, ctx, scopes)
    cluster_by_id = {c.cluster_id: c for c in collection.clusters}
    pairs = [
        (proposal, cluster_by_id[proposal.cluster_id])
        for proposal in proposals
        if proposal.cluster_id in cluster_by_id
    ]
    mechanisms = await asyncio.gather(
        *[
            reasoner.assess_mechanism(proposal, cluster, collection.clusters, ctx)
            for proposal, cluster in pairs
        ]
    )

    drivers: List[Driver] = []
    drafts: Dict[str, Any] = {}
    for i, ((proposal, cluster), mechanism) in enumerate(zip(pairs, mechanisms), start=1):
        checks = build_checks(
            proposal, cluster, ctx, mechanism.result, mechanism.reasoning
        )
        supporting, contradicting = split_evidence_refs(
            cluster, checks, [], ledger
        )
        status, unresolved = decide_status(checks, supporting, ledger)
        driver = assemble_driver(
            i, proposal, cluster, checks, supporting, contradicting, status, unresolved
        )
        drivers.append(driver)
        drafts[driver.id] = (proposal, cluster, checks, mechanism)
    recorder.step("drivers", "done", f"形成 {len(drivers)} 个候选驱动因素")

    recorder.step("counter", "running")
    recorder.step(
        "counter",
        "done",
        f"{sum(len(d.contradicting_refs) for d in drivers)} 条反向证据",
    )

    # ---------------- 第三阶段：意味着什么 ----------------
    recorder.step("exposure", "running")
    recorder.step("transmission", "running")
    stage_three = [driver for driver in drivers if enters_stage_three(driver)]
    transmissions = await asyncio.gather(
        *[
            reasoner.build_transmission(
                drafts[driver.id][0],
                drafts[driver.id][1],
                collection.clusters,
                ctx,
                ledger,
            )
            for driver in stage_three
        ]
    )
    assessed_ids: List[str] = []
    for driver, draft in zip(stage_three, transmissions):
        proposal, cluster, checks, mechanism = drafts[driver.id]
        extra_supporting, extra_contra = split_evidence_refs(
            cluster, checks, draft.counter_evidence_ids, ledger
        )
        driver.contradicting_refs = _merge_refs(
            driver.contradicting_refs, extra_contra
        )
        driver.assessment = _build_assessment(
            driver, cluster, checks, draft, ledger
        )
        assessed_ids.append(driver.id)
    recorder.step("exposure", "done")
    recorder.step("transmission", "done", f"{len(assessed_ids)} 个驱动因素完成传导分析")

    # ---------------- 结论装配 ----------------
    recorder.step("verdict", "running")
    overall_dir, overall_display, overall_reason, can_summarize = summarize_overall(
        [
            (d.assessment.direction, d.assessment.strength)
            for d in drivers
            if d.assessment is not None
        ]
    )

    why = WhyHappenedSection(
        priority=priority,
        priority_label=priority_label(priority),
        priority_reason=priority_reason,
        drivers=drivers,
        clue_pool=clue_pool,
        evidence_window_note=evidence_window.describe(),
        gaps=[g for g in gaps if g.field.startswith("evidence")],
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
    open_questions = _build_open_questions(drivers, gaps, industry, providers)

    metrics = _compute_metrics(drivers, ledger, recorder, providers)
    brief = ResearchBrief(
        run_id=run_id,
        created_at=now_iso(),
        subject=SubjectSection(
            stock=stock,
            user_question=request.query,
            window=resolution.info,
            resolved_note=resolution.info.remap_note,
        ),
        what_happened=what_happened,
        why_happened=why,
        what_it_means=what_it_means,
        open_questions=open_questions,
        evidence=ledger.all(),
        metrics=metrics,
        trace=ResearchTrace(
            tool_calls=recorder.tool_calls,
            llm_calls=recorder.llm_calls,
            providers=providers.labels,
        ),
        disclaimers=guardrails.DISCLAIMERS
        + guardrails.degraded_notice(
            providers.labels, getattr(reasoner, "disclaimer", "")
        ),
    )
    _apply_guardrails(brief, recorder)
    brief.metrics.time_to_verifiable_insight_ms = recorder.elapsed_ms()
    recorder.step("verdict", "done")
    return brief


# --------------------------------------------------------------------------
# 装配
# --------------------------------------------------------------------------


def _register_index_fact(
    ledger: EvidenceLedger,
    providers: ProviderBundle,
    code: str,
    name: str,
    window_days: List[str],
    cum: float,
    selected: List[Bar],
) -> str:
    evidence = ledger.register_market_fact(
        claim=f"{name}（{code}）在 {window_days[0]} ~ {window_days[-1]} 的区间累计涨跌为 {presenter.pct(cum)}",
        source=f"{providers.labels['market']} · 指数日 K",
        provider=getattr(providers.market, "name", "unknown"),
        as_of=window_days[-1],
        unit="百分比",
        caliber="区间末收盘价 / 区间首日前收盘价 - 1",
        raw_ref={"thscode": code, "bars": [b.model_dump() for b in selected]},
    )
    return evidence.id


def _build_reasoner(providers: ProviderBundle, recorder: RunRecorder):
    if getattr(providers.llm, "name", "mock") == "mock":
        return HeuristicReasoner()
    return LLMReasoner(providers.llm, recorder)


def _build_what_happened(
    daily_profile,
    window_profile,
    comparison,
    industry: IndustryInfo,
    window_bars: List[Bar],
    stock: StockRef,
    window_label: str,
    stock_evidence_id: str,
    gaps: List[DataGap],
) -> WhatHappenedSection:
    measures = []
    if daily_profile is not None:
        p = daily_profile
        measures = [
            presenter.measure("pct_change", "全天涨跌", p.pct_change,
                              presenter.pct(p.pct_change), unit=presenter.UNIT_PCT,
                              caliber=p.caliber("pct_change"),
                              evidence_id=stock_evidence_id),
            presenter.measure("gap_pct", "开盘缺口", p.gap_pct,
                              presenter.pct(p.gap_pct), unit=presenter.UNIT_PCT,
                              caliber=p.caliber("gap_pct")),
            presenter.measure("post_open_pct", "开盘后变化", p.post_open_pct,
                              presenter.pct(p.post_open_pct), unit=presenter.UNIT_PCT,
                              caliber=p.caliber("post_open_pct")),
            presenter.measure("amplitude", "日内振幅", p.amplitude,
                              presenter.pct(p.amplitude, signed=False), unit=presenter.UNIT_PCT_ABS,
                              caliber=p.caliber("amplitude")),
            presenter.measure("close_position", "收盘位置", p.close_position,
                              presenter.share(p.close_position), unit=presenter.UNIT_SHARE,
                              caliber=p.caliber("close_position"),
                              note=describe_close_position(p.close_position)),
            presenter.measure("turnover_ratio", "成交活跃度", p.turnover_ratio,
                              presenter.ratio(p.turnover_ratio), unit=presenter.UNIT_RATIO,
                              caliber=p.caliber("turnover_ratio"),
                              note=describe_turnover(p.turnover_ratio)),
        ]
    else:
        wp = window_profile
        measures = [
            presenter.measure("cumulative_pct", "区间累计涨跌", wp.cumulative_pct,
                              presenter.pct(wp.cumulative_pct), unit=presenter.UNIT_PCT,
                              caliber="区间末收盘价 / 区间首日前收盘价 - 1",
                              evidence_id=stock_evidence_id),
            presenter.measure("max_single_day", "最大单日变化",
                              wp.max_single_day.pct if wp.max_single_day else None,
                              presenter.pct(wp.max_single_day.pct) if wp.max_single_day else presenter.UNAVAILABLE,
                              unit=presenter.UNIT_PCT,
                              caliber="窗口内绝对值最大的单日涨跌",
                              note=f"发生于 {wp.max_single_day.date}" if wp.max_single_day else None),
            presenter.measure("concentration", "单日集中度", wp.concentration,
                              presenter.share(wp.concentration), unit=presenter.UNIT_SHARE,
                              caliber="最大单日绝对收益 ÷ 窗口内所有交易日绝对收益之和"),
            presenter.measure("direction_consistency", "方向一致性", wp.direction_consistency,
                              presenter.share(wp.direction_consistency), unit=presenter.UNIT_SHARE,
                              caliber="与区间总体方向同向的交易日数 ÷ 有效交易日数",
                              note=f"{wp.same_direction_days}/{len(wp.daily_returns)} 个交易日同向"),
            presenter.measure("path_efficiency", "路径效率", wp.path_efficiency,
                              presenter.share(wp.path_efficiency), unit=presenter.UNIT_SHARE,
                              caliber="|区间净价格变化| ÷ 期间逐日价格变化绝对值之和"),
        ]
        if wp.max_drawdown is not None:
            measures.append(
                presenter.measure("max_drawdown", "区间最大回撤", wp.max_drawdown,
                                  presenter.pct(wp.max_drawdown), unit=presenter.UNIT_PCT,
                                  caliber="区间内收盘价从阶段高点回落的最大幅度")
            )

    series = [
        PricePoint(
            date=b.date, open=b.open, high=b.high, low=b.low, close=b.close,
            prev_close=b.prev_close, volume=b.volume, amount=b.amount,
            pct_change=(b.close / b.prev_close - 1)
            if b.close is not None and b.prev_close not in (None, 0) else None,
        )
        for b in window_bars
    ]

    pattern = (
        PricePattern.SINGLE_DAY if daily_profile is not None else window_profile.pattern
    )
    summary = _summarize_what_happened(
        stock, window_label, daily_profile, window_profile, comparison
    )
    return WhatHappenedSection(
        measures=measures,
        pattern=pattern,
        pattern_label=PATTERN_LABELS[pattern.value],
        pattern_reason=window_profile.pattern_reason,
        comparison=comparison,
        industry=industry,
        series=series,
        summary=summary,
        gaps=[g for g in gaps if not g.field.startswith("evidence")],
    )


def _summarize_what_happened(
    stock, window_label, daily_profile, window_profile, comparison
) -> str:
    parts = [f"{stock.name}在{window_label}"]
    if daily_profile is not None:
        parts.append(f"收盘 {presenter.pct(daily_profile.pct_change)}")
        if daily_profile.gap_pct is not None:
            parts.append(f"开盘缺口 {presenter.pct(daily_profile.gap_pct)}")
        parts.append(describe_close_position(daily_profile.close_position))
        parts.append(describe_turnover(daily_profile.turnover_ratio))
    else:
        parts.append(f"区间累计 {presenter.pct(window_profile.cumulative_pct)}")
        parts.append(f"价格形态为{PATTERN_LABELS[window_profile.pattern.value]}")
    if comparison.industry and comparison.industry.value is not None:
        parts.append(f"同期行业 {comparison.industry.display}")
    if comparison.market and comparison.market.value is not None:
        parts.append(f"市场 {comparison.market.display}")
    return "，".join(parts) + "。"


def _build_assessment(
    driver: Driver,
    cluster: ClusterInfo,
    checks,
    draft,
    ledger: EvidenceLedger,
) -> FundamentalAssessment:
    by_key = {c.key: c for c in checks}
    timing = by_key["timing"].result
    mechanism = by_key["mechanism"].result

    horizon, horizon_reason = apply_structural_gate(
        draft.horizon, draft.horizon_reason, cluster.best_tier, draft.exposure_level
    )

    has_counter = any(
        r.support in (SupportLevel.WEAKENS, SupportLevel.REFUTES)
        for r in driver.contradicting_refs
    )
    strength_result = compute_evidence_strength(
        StrengthInput(
            event_tier=cluster.best_tier,
            timing_result=timing,
            mechanism_result=mechanism,
            exposure_level=draft.exposure_level,
            direction=draft.direction,
            horizon=horizon,
            has_unresolved_counter_evidence=has_counter,
            key_unknowns=draft.key_unknowns,
            driver_status=driver.status,
        )
    )
    display = build_display_verdict(
        draft.direction, horizon, strength_result.strength
    )

    exposure_refs = [
        EvidenceRef(
            evidence_id=eid,
            support=SupportLevel.SUPPORTS,
            rationale="用于确认公司在该因素上的业务暴露。",
        )
        for eid in draft.exposure_evidence_ids
        if eid in ledger
    ]

    return FundamentalAssessment(
        exposure_level=draft.exposure_level,
        exposure_label=EXPOSURE_LABELS[draft.exposure_level.value],
        exposure_basis=draft.exposure_basis,
        exposure_refs=exposure_refs,
        chain=[
            TransmissionStep(text=s.text, is_conditional=s.is_conditional)
            for s in draft.chain
        ],
        offsetting_factors=draft.offsetting_factors,
        amplifying_factors=draft.amplifying_factors,
        key_unknowns=draft.key_unknowns,
        direction=draft.direction,
        horizon=horizon,
        strength=strength_result.strength,
        display_direction=display.direction,
        display_horizon=display.horizon,
        display_strength=display.strength,
        display_headline=display.headline,
        display_suppressed=display.suppressed,
        suppression_reason=display.suppression_reason,
        direction_reason=draft.direction_reason,
        horizon_reason=horizon_reason,
        strength_reason=strength_result.reason,
    )


def _merge_refs(base: List[EvidenceRef], extra: List[EvidenceRef]) -> List[EvidenceRef]:
    seen = {r.evidence_id for r in base}
    return base + [r for r in extra if r.evidence_id not in seen]


def _build_open_questions(
    drivers: List[Driver],
    gaps: List[DataGap],
    industry: IndustryInfo,
    providers: ProviderBundle,
) -> OpenQuestionsSection:
    questions: List[str] = []
    for driver in drivers:
        for item in driver.unresolved:
            questions.append(f"【{driver.name}】{item}")
        if driver.assessment:
            for unknown in driver.assessment.key_unknowns:
                questions.append(f"【{driver.name}】{unknown}")
    if industry.is_weak_evidence:
        questions.append(
            f"所属行业（{industry.index_name}）由模型推断而非结构化数据确认，"
            "行业层面的比较结论需要额外谨慎对待。"
        )
    if not drivers:
        questions.append("本次研究没有形成任何候选驱动因素，价格变化的原因尚未被解释。")

    # 去重并保序
    seen = set()
    unique = []
    for q in questions:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return OpenQuestionsSection(questions=unique, gaps=gaps)


def _compute_metrics(
    drivers: List[Driver],
    ledger: EvidenceLedger,
    recorder: RunRecorder,
    providers: ProviderBundle,
) -> RunMetrics:
    """核心结论 = 每个驱动因素的状态判定 + 每个未被抑制的基本面判断。"""
    total = 0
    with_evidence = 0
    unsupported = 0

    for driver in drivers:
        total += 1
        if ledger.refs_can_support(driver.supporting_refs):
            with_evidence += 1
        elif driver.status != DriverStatus.INSUFFICIENT:
            # 没有可支撑证据却没被判为证据不足 —— 这正是要监控的无证据推断
            unsupported += 1

        assessment = driver.assessment
        if assessment is None or assessment.display_suppressed:
            continue
        total += 1
        if assessment.exposure_refs or ledger.refs_can_support(driver.supporting_refs):
            with_evidence += 1
        else:
            unsupported += 1

    return RunMetrics(
        total_conclusions=total,
        conclusions_with_evidence=with_evidence,
        evidence_coverage=(with_evidence / total) if total else 0.0,
        unsupported_inference_rate=(unsupported / total) if total else 0.0,
        evidence_count=len(ledger),
        independent_source_count=ledger.total_independent_sources(),
        tool_calls=len(recorder.tool_calls),
        failed_tool_calls=recorder.failed_tool_calls,
        degraded=providers.degraded,
    )


def _apply_guardrails(brief: ResearchBrief, recorder: RunRecorder) -> None:
    """对所有自由文本做最后一道合规扫描。"""
    hits: List[str] = []

    brief.what_happened.summary, h = guardrails.sanitize(brief.what_happened.summary)
    hits += h
    brief.why_happened.priority_reason, h = guardrails.sanitize(
        brief.why_happened.priority_reason
    )
    hits += h

    for driver in brief.why_happened.drivers:
        driver.summary, h = guardrails.sanitize(driver.summary)
        hits += h
        driver.relevance, h = guardrails.sanitize(driver.relevance)
        hits += h
        driver.unresolved, h = guardrails.sanitize_list(driver.unresolved)
        hits += h
        for check in driver.checks:
            check.reasoning, h = guardrails.sanitize(check.reasoning)
            hits += h
        a = driver.assessment
        if a is None:
            continue
        a.exposure_basis, h = guardrails.sanitize(a.exposure_basis)
        hits += h
        a.direction_reason, h = guardrails.sanitize(a.direction_reason)
        hits += h
        a.horizon_reason, h = guardrails.sanitize(a.horizon_reason)
        hits += h
        a.offsetting_factors, h = guardrails.sanitize_list(a.offsetting_factors)
        hits += h
        a.amplifying_factors, h = guardrails.sanitize_list(a.amplifying_factors)
        hits += h
        a.key_unknowns, h = guardrails.sanitize_list(a.key_unknowns)
        hits += h
        for step in a.chain:
            step.text, h = guardrails.sanitize(step.text)
            hits += h

    brief.open_questions.questions, h = guardrails.sanitize_list(
        brief.open_questions.questions
    )
    hits += h

    if hits:
        recorder.log(f"合规护栏拦截了 {len(hits)} 处表述：{sorted(set(hits))}")
        brief.disclaimers.append(
            f"本次输出中有 {len(hits)} 处表述被合规护栏拦截并替换。"
        )


SNAPSHOT_FOLLOWUP = "若需要完整异动分析报告，请直接说明。"


async def run_snapshot(
    request: ResearchRequest,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ResearchBrief:
    """简单 query：只识别标的、取日历和日 K，不检索资讯、不调用模型。"""
    recorder.step("resolve", "running")
    parsed = parse_query(request.query)
    key = request.search_key or parsed.search_key
    window = request.window or parsed.window
    if not key:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_stock",
            "没有识别出股票名称或代码。",
            "请输入 A 股公司名称或 6 位代码。",
        )
    if window is None:
        recorder.step("resolve", "failed")
        raise NeedsWindowChoice(
            "need_window",
            "识别到股票，但没有指定研究窗口。",
            "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后先为您做行情快照。",
        )

    stock, trading_days = await asyncio.gather(
        resolve_stock(key, providers, recorder),
        fetch_trading_days(providers, recorder),
    )
    resolution = resolve_window(
        window, trading_days, baseline_days=settings.turnover_baseline_days
    )
    if resolution is None:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_calendar",
            "交易日历数据不足，无法确定研究窗口。",
            "该窗口需要的交易日数量超出了当前可用的日历范围。",
        )
    recorder.step(
        "resolve",
        "done",
        f"{stock.name} {stock.thscode}，"
        f"窗口 {resolution.info.actual_start} ~ {resolution.info.actual_end}",
    )

    recorder.step("quote", "running")
    stock_bars, stock_gap = await _fetch_bars(
        providers.market.daily_bars,
        stock.thscode,
        resolution.lookback_start,
        resolution.info.actual_end,
        recorder,
        "daily_bars",
        f"{stock.name} 日 K",
    )
    if not stock_bars:
        recorder.step("quote", "failed")
        raise ResearchError(
            "no_price_data",
            f"无法获取 {stock.name} 的行情数据，研究无法继续。",
            stock_gap.reason if stock_gap else "",
        )
    window_days = set(resolution.window_days)
    window_bars = [bar for bar in stock_bars if bar.date in window_days]
    history_bars = [bar for bar in stock_bars if bar.date < resolution.info.actual_start]
    if resolution.info.is_intraday and window_bars:
        window_bars[-1].is_intraday = True
    recorder.step("quote", "done", f"取得 {len(stock_bars)} 根日 K")

    recorder.step("profile", "running")
    stock_cum, daily_profile, window_profile = _build_price_profiles(
        window_bars, history_bars, window, settings
    )
    gaps: List[DataGap] = []
    if stock_gap:
        gaps.append(stock_gap)
    if window_profile:
        gaps.extend(window_profile.gaps)
    if daily_profile:
        gaps.extend(daily_profile.gaps)

    industry = IndustryInfo(
        method="unavailable",
        method_label="快照未查询行业",
        note="行情快照不检索行业与资讯。",
    )
    comparison = build_comparison(
        stock_pct=stock_cum,
        industry_pct=None,
        market_pct=None,
        window_label=resolution.info.label,
    )
    what_happened = _build_what_happened(
        daily_profile,
        window_profile,
        comparison,
        industry,
        window_bars,
        stock,
        resolution.info.label,
        "",
        gaps,
    )
    recorder.step("profile", "done")

    brief = ResearchBrief(
        run_id=recorder.run_id,
        created_at=now_iso(),
        kind="snapshot",
        subject=SubjectSection(
            stock=stock,
            user_question=request.query,
            window=resolution.info,
            resolved_note=resolution.info.remap_note,
        ),
        what_happened=what_happened,
        why_happened=WhyHappenedSection(
            priority=ResearchPriority.COMPANY_FIRST,
            priority_label="行情快照",
            priority_reason="本次为快速查询，未展开异动归因。",
        ),
        what_it_means=WhatItMeansSection(
            overall=OverallVerdict(
                direction=ImpactDirection.UNCERTAIN,
                display="本次为行情快照，未形成基本面判断。",
                reason=SNAPSHOT_FOLLOWUP,
                can_summarize=False,
            ),
            note=SNAPSHOT_FOLLOWUP,
        ),
        open_questions=OpenQuestionsSection(questions=[], gaps=gaps),
        evidence=[],
        metrics=RunMetrics(
            time_to_verifiable_insight_ms=recorder.elapsed_ms(),
            tool_calls=len(recorder.tool_calls),
            failed_tool_calls=recorder.failed_tool_calls,
            degraded=providers.degraded,
        ),
        trace=ResearchTrace(
            tool_calls=recorder.tool_calls,
            llm_calls=recorder.llm_calls,
            providers=providers.labels,
        ),
        disclaimers=guardrails.DISCLAIMERS
        + guardrails.degraded_notice(providers.labels, "")
        + [SNAPSHOT_FOLLOWUP],
    )
    brief.what_happened.summary, _ = guardrails.sanitize(brief.what_happened.summary)
    return brief
