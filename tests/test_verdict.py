"""证据强度计算、展示约束与合规护栏的校验。

这些是产品最后一道闸门，任何一条失效都会让"证据不足"的结论以
"看起来很确定"的样子出现在用户面前。
"""

from __future__ import annotations

from app.contracts import (
    CheckResult,
    DriverStatus,
    EvidenceStrength,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
    SourceTier,
)
from app.engine import guardrails
from app.engine.verdict import (
    SUPPRESSED_DIRECTION,
    SUPPRESSED_HORIZON,
    StrengthInput,
    apply_structural_gate,
    build_display_verdict,
    compute_evidence_strength,
    summarize_overall,
)


def strength_input(**kwargs) -> StrengthInput:
    base = dict(
        event_tier=SourceTier.T1_AUTHORITATIVE,
        timing_result=CheckResult.PASS,
        mechanism_result=CheckResult.PASS,
        exposure_level=ExposureLevel.P1_FILING,
        direction=ImpactDirection.NEGATIVE,
        horizon=ImpactHorizon.PHASED,
        has_unresolved_counter_evidence=False,
        key_unknowns=[],
        driver_status=DriverStatus.SUPPORTED,
    )
    base.update(kwargs)
    return StrengthInput(**base)


# --------------------------------------------------------------------------
# 证据强度
# --------------------------------------------------------------------------


def test_complete_chain_is_sufficient():
    r = compute_evidence_strength(strength_input())
    assert r.strength == EvidenceStrength.SUFFICIENT
    assert r.missing_links == []


def test_unconfirmed_exposure_breaks_the_chain():
    """公司暴露无法确认时，不允许形成强基本面结论。"""
    r = compute_evidence_strength(
        strength_input(exposure_level=ExposureLevel.UNCONFIRMED)
    )
    assert r.strength == EvidenceStrength.INSUFFICIENT
    assert any("业务暴露无法确认" in m for m in r.missing_links)


def test_unverified_source_breaks_the_chain():
    r = compute_evidence_strength(strength_input(event_tier=SourceTier.T4_UNVERIFIED))
    assert r.strength == EvidenceStrength.INSUFFICIENT
    assert any("原始出处" in m for m in r.missing_links)


def test_timing_mismatch_breaks_the_chain():
    r = compute_evidence_strength(strength_input(timing_result=CheckResult.FAIL))
    assert r.strength == EvidenceStrength.INSUFFICIENT


def test_key_unknowns_downgrade_to_partial_not_insufficient():
    r = compute_evidence_strength(strength_input(key_unknowns=["无法确认长协比例"]))
    assert r.strength == EvidenceStrength.PARTIAL
    assert r.concerns


def test_unresolved_counter_evidence_downgrades_to_partial():
    r = compute_evidence_strength(
        strength_input(has_unresolved_counter_evidence=True)
    )
    assert r.strength == EvidenceStrength.PARTIAL


def test_strength_is_not_model_confidence():
    """同样的方向和期限，只要证据链环节缺失，强度就必须下降。"""
    strong = compute_evidence_strength(strength_input())
    weak = compute_evidence_strength(
        strength_input(exposure_level=ExposureLevel.P4_INFERENCE)
    )
    assert strong.strength == EvidenceStrength.SUFFICIENT
    assert weak.strength == EvidenceStrength.PARTIAL


# --------------------------------------------------------------------------
# 结构性门槛
# --------------------------------------------------------------------------


def test_structural_requires_authoritative_source_and_filed_exposure():
    horizon, _ = apply_structural_gate(
        ImpactHorizon.STRUCTURAL, "准入长期改变",
        SourceTier.T1_AUTHORITATIVE, ExposureLevel.P1_FILING,
    )
    assert horizon == ImpactHorizon.STRUCTURAL


def test_structural_is_withheld_without_authoritative_source():
    horizon, reason = apply_structural_gate(
        ImpactHorizon.STRUCTURAL, "准入长期改变",
        SourceTier.T2_PROFESSIONAL, ExposureLevel.P1_FILING,
    )
    assert horizon == ImpactHorizon.UNCERTAIN
    assert "门槛" in reason


def test_structural_is_withheld_when_exposure_is_only_media_reported():
    horizon, _ = apply_structural_gate(
        ImpactHorizon.STRUCTURAL, "准入变化",
        SourceTier.T1_AUTHORITATIVE, ExposureLevel.P3_MEDIA,
    )
    assert horizon == ImpactHorizon.UNCERTAIN


def test_non_structural_horizons_pass_through_untouched():
    for h in (ImpactHorizon.ONE_OFF, ImpactHorizon.PHASED, ImpactHorizon.UNCERTAIN):
        out, reason = apply_structural_gate(
            h, "理由", SourceTier.T4_UNVERIFIED, ExposureLevel.UNCONFIRMED
        )
        assert out == h and reason == "理由"


# --------------------------------------------------------------------------
# 展示约束
# --------------------------------------------------------------------------


def test_sufficient_evidence_displays_direct_conclusion():
    v = build_display_verdict(
        ImpactDirection.NEGATIVE, ImpactHorizon.STRUCTURAL, EvidenceStrength.SUFFICIENT
    )
    assert v.headline == "负向 · 结构性 · 证据充分"
    assert v.suppressed is False


def test_partial_evidence_uses_cautious_wording():
    v = build_display_verdict(
        ImpactDirection.NEGATIVE, ImpactHorizon.PHASED, EvidenceStrength.PARTIAL
    )
    assert v.headline == "倾向负向 · 阶段性 · 部分充分"
    assert v.suppressed is False


def test_insufficient_evidence_suppresses_direction_and_horizon():
    """这是最关键的一条：证据不足时绝不能展示「负向 · 结构性」。"""
    v = build_display_verdict(
        ImpactDirection.NEGATIVE,
        ImpactHorizon.STRUCTURAL,
        EvidenceStrength.INSUFFICIENT,
    )
    assert v.suppressed is True
    assert v.direction == SUPPRESSED_DIRECTION
    assert v.horizon == SUPPRESSED_HORIZON
    assert "负向" not in v.headline
    assert "结构性" not in v.headline
    assert v.suppression_reason


# --------------------------------------------------------------------------
# 总体概括
# --------------------------------------------------------------------------


def test_consistent_directions_produce_an_overall_summary():
    direction, display, _, can = summarize_overall(
        [
            (ImpactDirection.NEGATIVE, EvidenceStrength.SUFFICIENT),
            (ImpactDirection.NEGATIVE, EvidenceStrength.PARTIAL),
        ]
    )
    assert direction == ImpactDirection.NEGATIVE and can is True


def test_conflicting_directions_produce_mixed_not_a_vote_count():
    """2 负 1 正不等于「总体负向」。"""
    direction, display, reason, _ = summarize_overall(
        [
            (ImpactDirection.NEGATIVE, EvidenceStrength.SUFFICIENT),
            (ImpactDirection.NEGATIVE, EvidenceStrength.PARTIAL),
            (ImpactDirection.POSITIVE, EvidenceStrength.PARTIAL),
        ]
    )
    assert direction == ImpactDirection.MIXED
    assert display == "正负影响并存"
    assert "不做加权合并" in reason


def test_insufficient_drivers_yield_no_directional_conclusion():
    direction, _, _, can = summarize_overall(
        [
            (ImpactDirection.NEGATIVE, EvidenceStrength.INSUFFICIENT),
            (ImpactDirection.UNCERTAIN, EvidenceStrength.PARTIAL),
        ]
    )
    assert direction == ImpactDirection.UNCERTAIN and can is False


def test_no_drivers_yields_no_conclusion():
    direction, _, _, can = summarize_overall([])
    assert direction == ImpactDirection.UNCERTAIN and can is False


# --------------------------------------------------------------------------
# 合规护栏
# --------------------------------------------------------------------------


def test_buy_sell_advice_is_removed():
    for text in (
        "建议买入该股票",
        "可以逢低吸纳",
        "给予买入评级",
        "应该卖出并止损",
    ):
        cleaned, hits = guardrails.sanitize(text)
        assert "investment_advice" in hits
        assert cleaned == guardrails.REDACTION


def test_price_predictions_and_return_promises_are_removed():
    for text in (
        "股价将上涨",
        "该股必将反弹",
        "目标价：300 元",
        "预计收益率可达 20%",
        "稳赚不赔",
    ):
        cleaned, hits = guardrails.sanitize(text)
        assert "price_prediction" in hits
        assert cleaned == guardrails.REDACTION


def test_legitimate_fundamental_statements_are_not_touched():
    """基本面判断必须能正常通过，否则护栏会把产品本身的价值删掉。"""
    for text in (
        "若无法完全转嫁成本，毛利空间承压。",
        "公司境外营业收入占比 32.6%，存在相关业务暴露。",
        "该政策设置 18 个月过渡期，影响更可能是阶段性的。",
        "影响方向：暂无法可靠判断。",
    ):
        cleaned, hits = guardrails.sanitize(text)
        assert hits == []
        assert cleaned == text


def test_sanitize_list_reports_all_hits():
    items = ["正常表述", "建议买入", "另一条正常表述"]
    cleaned, hits = guardrails.sanitize_list(items)
    assert cleaned[0] == "正常表述"
    assert cleaned[1] == guardrails.REDACTION
    assert len(hits) == 1
