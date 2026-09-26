"""把工作记忆装配成 Brief。

装配是确定性的：不再调用模型，也不再取数。
"""

from __future__ import annotations

from typing import List

from app.agent.evidence_collect import ClusterInfo
from app.agent.llm_reasoner import LLMReasoner
from app.agent.reasoner import HeuristicReasoner
from app.contracts import (
    EXPOSURE_LABELS,
    PATTERN_LABELS,
    DataGap,
    DriverStatus,
    EvidenceRef,
    ImpactDirection,
    PricePattern,
    SupportLevel,
)
from app.engine import guardrails, presenter
from app.engine.price_profile import describe_close_position, describe_turnover
from app.engine.verdict import (
    StrengthInput,
    apply_structural_gate,
    build_display_verdict,
    compute_evidence_strength,
    summarize_overall,
)
from app.ledger import EvidenceLedger
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
    RunMetrics,
    StockRef,
    TransmissionStep,
    WhatHappenedSection,
    WhatItMeansSection,
    WhyHappenedSection,
)
from app.trace import RunRecorder


def register_index_fact(
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


def build_reasoner(providers: ProviderBundle, recorder: RunRecorder):
    if getattr(providers.llm, "name", "mock") == "mock":
        return HeuristicReasoner()
    return LLMReasoner(providers.llm, recorder)


def build_what_happened(
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
    """封面导语：先写价格事实，再写相对表现。一句一事，便于扫描。"""
    sentences: List[str] = []
    if daily_profile is not None:
        sentences.append(
            f"{stock.name}在{window_label}收盘 {presenter.pct(daily_profile.pct_change)}。"
        )
        extras = []
        if daily_profile.gap_pct is not None:
            extras.append(f"开盘缺口 {presenter.pct(daily_profile.gap_pct)}")
        extras.append(describe_close_position(daily_profile.close_position))
        extras.append(describe_turnover(daily_profile.turnover_ratio))
        sentences.append("，".join(extras) + "。")
    else:
        sentences.append(
            f"{stock.name}在{window_label}累计 {presenter.pct(window_profile.cumulative_pct)}，"
            f"价格形态为{PATTERN_LABELS[window_profile.pattern.value]}。"
        )

    relatives = []
    if comparison.industry and comparison.industry.value is not None:
        relatives.append(f"同期行业 {comparison.industry.display}")
    if comparison.market and comparison.market.value is not None:
        relatives.append(f"市场宽基 {comparison.market.display}")
    if relatives:
        relative = "，".join(relatives) + "。"
        vs = comparison.stock_vs_industry
        if vs and vs.value is not None:
            relative += f"个股相对行业 {vs.display}。"
        sentences.append(relative)
    return "".join(sentences)


def build_assessment(
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


def merge_refs(base: List[EvidenceRef], extra: List[EvidenceRef]) -> List[EvidenceRef]:
    seen = {r.evidence_id for r in base}
    return base + [r for r in extra if r.evidence_id not in seen]


_SKIP_QUESTION_FRAGMENTS = (
    "不足以解释个股相对行业的全部额外变化",
    "仍有未被解释的部分",
    "特异性仅部分成立",
    "机制合理性仅部分成立",
    "横截面吻合仅部分成立",
    "时间吻合仅部分成立",
    "无法判定",
    "当前没有任何来源可确认的证据",
    "来源无法确认的信息只能作为检索线索",
)


def _is_watchable_question(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 8:
        return False
    return not any(frag in t for frag in _SKIP_QUESTION_FRAGMENTS)


def build_open_questions(
    drivers: List[Driver],
    gaps: List[DataGap],
    industry: IndustryInfo,
) -> OpenQuestionsSection:
    questions: List[str] = []
    residual = False
    material = [
        d for d in drivers if d.status in (DriverStatus.SUPPORTED, DriverStatus.PARTIALLY_SUPPORTED)
    ]
    for driver in material:
        for item in driver.unresolved:
            if "不足以解释个股相对行业" in item:
                residual = True
                continue
            if _is_watchable_question(item):
                questions.append(f"【{driver.name}】{item}")
        if driver.assessment:
            for unknown in driver.assessment.key_unknowns:
                if "不足以解释个股相对行业" in unknown:
                    residual = True
                    continue
                if _is_watchable_question(unknown):
                    questions.append(f"【{driver.name}】{unknown}")
    if residual:
        questions.append("个股相对行业仍有未被解释的额外变化，需继续观察公司特有催化。")
    if industry.is_weak_evidence and industry.index_name:
        questions.append(
            f"所属行业（{industry.index_name}）尚未被结构化数据确认，行业对照需谨慎使用。"
        )
    if not material:
        questions.append("本次未找到可确认的异动解释，价格变化原因仍待观察。")

    seen = set()
    unique = []
    for q in questions:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return OpenQuestionsSection(questions=unique, gaps=gaps)


def compute_metrics(
    drivers: List[Driver],
    ledger: EvidenceLedger,
    recorder: RunRecorder,
    providers: ProviderBundle,
    agent_steps: int = 0,
) -> RunMetrics:
    total = 0
    with_evidence = 0
    unsupported = 0

    for driver in drivers:
        total += 1
        if ledger.refs_can_support(driver.supporting_refs):
            with_evidence += 1
        elif driver.status != DriverStatus.INSUFFICIENT:
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
        agent_steps=agent_steps,
        degraded=providers.degraded,
    )


def apply_guardrails(brief: ResearchBrief, recorder: RunRecorder) -> None:
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
