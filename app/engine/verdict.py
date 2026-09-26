"""证据强度计算与最终结论的展示约束。

两条产品原则在这里变成代码：

1. **证据强度不是模型置信度**（规划 17 章）。它衡量的是
   "事件事实 → 公司暴露 → 传导机制 → 方向 → 期限"这条链的完整程度，
   因此由规则按客观条件计算，模型无权给自己的结论打分。

2. **证据强度对方向和期限拥有展示层面的否决权**（规划 18 章）。
   证据不足时，不允许把"负向 · 结构性"这样的结论摆出来，
   哪怕模型非常确信。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel

from app.contracts import (
    CheckResult,
    DriverStatus,
    EvidenceStrength,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
    SourceTier,
)

# 结构性结论的门槛：必须有一级原始权威来源确认事件，且公司暴露来自正式披露
_STRUCTURAL_EVENT_TIERS = (SourceTier.T1_AUTHORITATIVE,)
_STRUCTURAL_EXPOSURE_LEVELS = (ExposureLevel.P1_FILING, ExposureLevel.P2_COMPANY)

_CONFIRMED_EXPOSURE = (
    ExposureLevel.P1_FILING,
    ExposureLevel.P2_COMPANY,
    ExposureLevel.P3_MEDIA,
)

DIRECTION_LABELS_STRONG = {
    ImpactDirection.POSITIVE: "正向",
    ImpactDirection.NEGATIVE: "负向",
    ImpactDirection.MIXED: "正负影响并存",
    ImpactDirection.UNCERTAIN: "方向不确定",
}

DIRECTION_LABELS_CAUTIOUS = {
    ImpactDirection.POSITIVE: "倾向正向",
    ImpactDirection.NEGATIVE: "倾向负向",
    ImpactDirection.MIXED: "正负影响并存",
    ImpactDirection.UNCERTAIN: "方向不确定",
}

HORIZON_LABELS = {
    ImpactHorizon.ONE_OFF: "短期一次性",
    ImpactHorizon.PHASED: "阶段性",
    ImpactHorizon.STRUCTURAL: "结构性",
    ImpactHorizon.UNCERTAIN: "期限不确定",
}

STRENGTH_LABELS = {
    EvidenceStrength.SUFFICIENT: "证据充分",
    EvidenceStrength.PARTIAL: "部分充分",
    EvidenceStrength.INSUFFICIENT: "证据不足",
}

SUPPRESSED_DIRECTION = "影响方向：暂无法可靠判断"
SUPPRESSED_HORIZON = "影响期限：暂无法可靠判断"


class StrengthInput(BaseModel):
    event_tier: SourceTier
    timing_result: CheckResult
    mechanism_result: CheckResult
    exposure_level: ExposureLevel
    direction: ImpactDirection
    horizon: ImpactHorizon
    has_unresolved_counter_evidence: bool = False
    key_unknowns: List[str] = []
    driver_status: DriverStatus = DriverStatus.PARTIALLY_SUPPORTED


class StrengthResult(BaseModel):
    strength: EvidenceStrength
    reason: str
    missing_links: List[str] = []
    concerns: List[str] = []


def compute_evidence_strength(data: StrengthInput) -> StrengthResult:
    """按证据链完整程度计算证据强度。"""
    missing: List[str] = []

    event_confirmed = (
        data.event_tier
        in (SourceTier.T1_AUTHORITATIVE, SourceTier.T2_PROFESSIONAL, SourceTier.MARKET_DATA)
        and data.timing_result != CheckResult.FAIL
    )
    if not event_confirmed:
        if data.event_tier == SourceTier.T4_UNVERIFIED:
            missing.append("事件事实缺少可确认的原始出处")
        elif data.timing_result == CheckResult.FAIL:
            missing.append("事件时间与价格变化在时序上不匹配")
        else:
            missing.append("事件事实未能由足够可信的来源证实")

    if data.exposure_level == ExposureLevel.UNCONFIRMED:
        missing.append("公司在该因素上的业务暴露无法确认")

    if data.mechanism_result == CheckResult.FAIL:
        missing.append("事件与公司之间不存在可确认的作用机制")

    if data.driver_status == DriverStatus.INSUFFICIENT:
        missing.append("该驱动因素本身尚未通过候选验证")

    if missing:
        return StrengthResult(
            strength=EvidenceStrength.INSUFFICIENT,
            reason="证据链缺少关键环节：" + "；".join(missing) + "。",
            missing_links=missing,
        )

    concerns: List[str] = []
    if data.exposure_level == ExposureLevel.P4_INFERENCE:
        concerns.append("公司暴露仅依据行业常识推断，属于弱证据")
    elif data.exposure_level == ExposureLevel.P3_MEDIA:
        concerns.append("公司暴露来自媒体报道而非公司正式披露")
    if data.mechanism_result != CheckResult.PASS:
        concerns.append("传导机制只是部分成立")
    if data.timing_result != CheckResult.PASS:
        concerns.append("事件时间与价格变化窗口只是部分吻合")
    if data.has_unresolved_counter_evidence:
        concerns.append("存在尚未被解决的反向证据")
    if data.direction == ImpactDirection.UNCERTAIN:
        concerns.append("影响方向缺少判断依据")
    if data.horizon == ImpactHorizon.UNCERTAIN:
        concerns.append("影响期限缺少判断依据")
    if data.key_unknowns:
        concerns.append(f"仍有 {len(data.key_unknowns)} 项关键未知未被解决")

    if not concerns:
        return StrengthResult(
            strength=EvidenceStrength.SUFFICIENT,
            reason=(
                "事件事实、公司暴露、传导机制、方向与期限判断均有证据支撑，"
                "且没有足以改变当前结论的重大未解决问题。"
            ),
        )

    return StrengthResult(
        strength=EvidenceStrength.PARTIAL,
        reason="核心逻辑有事实支持，但仍存在可能影响结论的重要未知："
        + "；".join(concerns)
        + "。",
        concerns=concerns,
    )


def apply_structural_gate(
    horizon: ImpactHorizon,
    horizon_reason: str,
    event_tier: SourceTier,
    exposure_level: ExposureLevel,
) -> Tuple[ImpactHorizon, str]:
    """结构性结论需要更高门槛，达不到就不输出结构性。"""
    if horizon != ImpactHorizon.STRUCTURAL:
        return horizon, horizon_reason

    if event_tier in _STRUCTURAL_EVENT_TIERS and exposure_level in _STRUCTURAL_EXPOSURE_LEVELS:
        return horizon, horizon_reason

    return (
        ImpactHorizon.UNCERTAIN,
        "初步线索指向长期性影响，但结构性结论需要一级原始权威来源确认事件、"
        "且公司暴露来自正式披露，当前证据未达到该门槛，因此不输出结构性判断。"
        f"（原判断依据：{horizon_reason}）",
    )


class DisplayVerdict(BaseModel):
    direction: str
    horizon: str
    strength: str
    headline: str
    suppressed: bool = False
    suppression_reason: Optional[str] = None


def build_display_verdict(
    direction: ImpactDirection,
    horizon: ImpactHorizon,
    strength: EvidenceStrength,
) -> DisplayVerdict:
    """证据强度对方向和期限的展示否决权在这里生效。"""
    strength_label = STRENGTH_LABELS[strength]

    if strength == EvidenceStrength.INSUFFICIENT:
        return DisplayVerdict(
            direction=SUPPRESSED_DIRECTION,
            horizon=SUPPRESSED_HORIZON,
            strength=strength_label,
            headline=f"{SUPPRESSED_DIRECTION} · {SUPPRESSED_HORIZON} · 证据：不足",
            suppressed=True,
            suppression_reason=(
                "关键证据链不完整，因此不展示方向与期限判断。"
                "下方保留初步可能的传导路径，但它不是结论。"
            ),
        )

    if strength == EvidenceStrength.PARTIAL:
        direction_label = DIRECTION_LABELS_CAUTIOUS[direction]
    else:
        direction_label = DIRECTION_LABELS_STRONG[direction]

    horizon_label = HORIZON_LABELS[horizon]
    return DisplayVerdict(
        direction=direction_label,
        horizon=horizon_label,
        strength=strength_label,
        headline=f"{direction_label} · {horizon_label} · {strength_label}",
    )


# --------------------------------------------------------------------------
# 多驱动因素的总体概括
# --------------------------------------------------------------------------


def summarize_overall(
    verdicts: List[Tuple[ImpactDirection, EvidenceStrength]],
) -> Tuple[ImpactDirection, str, str, bool]:
    """对多个驱动因素做总体概括。

    不使用票数、人工权重或百分比归因：只有当各主要驱动因素方向高度一致时
    才给出总体方向；出现重要冲突时显示"正负影响并存"；关键原因证据不足时
    不强行形成方向性结论。
    """
    usable = [
        (d, s)
        for d, s in verdicts
        if s != EvidenceStrength.INSUFFICIENT and d != ImpactDirection.UNCERTAIN
    ]

    if not verdicts:
        return (
            ImpactDirection.UNCERTAIN,
            "暂无可评估的驱动因素",
            "本次研究没有形成任何通过验证的驱动因素，因此不给出总体基本面判断。",
            False,
        )

    if not usable:
        return (
            ImpactDirection.UNCERTAIN,
            "暂无法形成总体判断",
            "主要驱动因素的证据强度不足或方向无法判断，"
            "因此不强行形成方向性结论。",
            False,
        )

    directions = {d for d, _ in usable}

    if directions == {ImpactDirection.POSITIVE}:
        return (
            ImpactDirection.POSITIVE,
            "主要驱动因素方向一致：正向",
            "各主要驱动因素的基本面传导方向一致指向正面。",
            True,
        )

    if directions == {ImpactDirection.NEGATIVE}:
        return (
            ImpactDirection.NEGATIVE,
            "主要驱动因素方向一致：负向",
            "各主要驱动因素的基本面传导方向一致指向负面。",
            True,
        )

    return (
        ImpactDirection.MIXED,
        "正负影响并存",
        "不同驱动因素的基本面影响方向存在冲突，当前证据不足以判断哪一方占主导，"
        "因此不做加权合并，请分别查看每个驱动因素的独立评估。",
        True,
    )
