"""候选驱动因素的组装与状态判定。

状态（支持 / 部分支持 / 证据不足）完全由规则决定，输入是四类验证的结果
和证据的来源等级。四项里至少三项为「吻合」即视为有效因素（支持）；
单项失败不再一票否决。模型不参与这一步。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from app.agent.evidence_collect import ClusterInfo
from app.agent.reasoner import DriverProposal, TransmissionDraft
from app.agent.validation import (
    MarketContext,
    check_cross_section,
    check_specificity,
    check_timing,
    make_mechanism_check,
)
from app.contracts import (
    CATEGORY_LABELS,
    DRIVER_STATUS_LABELS,
    CheckResult,
    DriverCategory,
    DriverStatus,
    EvidenceRef,
    SourceTier,
    SupportLevel,
)
from app.ledger import EvidenceLedger
from app.schemas import Driver, DriverCheck

# 四项验证里至少三项吻合，才识别为有效因素；单项失败不再一票否决。
_VALID_PASS_THRESHOLD = 3
_CRITICAL_CHECKS = ("timing", "cross_section", "mechanism")


def build_checks(
    proposal: DriverProposal,
    cluster: ClusterInfo,
    ctx: MarketContext,
    mechanism_result: CheckResult,
    mechanism_reasoning: str,
) -> List[DriverCheck]:
    return [
        check_timing(cluster, ctx),
        check_cross_section(cluster, proposal.category, ctx),
        check_specificity(cluster, proposal.category, ctx),
        make_mechanism_check(mechanism_result, mechanism_reasoning),
    ]


def split_evidence_refs(
    cluster: ClusterInfo,
    checks: List[DriverCheck],
    counter_ids: List[str],
    ledger: EvidenceLedger,
) -> Tuple[List[EvidenceRef], List[EvidenceRef]]:
    """把证据分成支持与反向两组，并给出每条证据对本论点的支持度。

    这里刻意把「来源可信度」和「对论点的支持度」分开：一条国务院文件的
    来源可信度是最高的，但它对"今天这只股票为什么跌"这个论点的支持度，
    仍然取决于时间吻合等验证结果。
    """
    by_key: Dict[str, DriverCheck] = {c.key: c for c in checks}
    timing = by_key.get("timing")
    timing_result = timing.result if timing else CheckResult.UNKNOWN

    supporting: List[EvidenceRef] = []
    contradicting: List[EvidenceRef] = []

    for eid in cluster.evidence_ids:
        evidence = ledger.get(eid)
        if evidence is None:
            continue

        if evidence.source_tier == SourceTier.T4_UNVERIFIED:
            support = SupportLevel.NEUTRAL
            rationale = "来源无法确认，仅作检索线索，不计入对结论的支撑。"
        elif timing_result == CheckResult.FAIL:
            support = SupportLevel.WEAKLY_SUPPORTS
            rationale = (
                "来源可确认，但发布时间与本次价格变化不完全吻合，只能提供弱支持。"
            )
        elif timing_result == CheckResult.PASS and evidence.source_tier in (
            SourceTier.T1_AUTHORITATIVE,
            SourceTier.T2_PROFESSIONAL,
        ):
            support = SupportLevel.SUPPORTS
            rationale = "来源可确认且时间落在核心证据窗口内。"
        else:
            support = SupportLevel.WEAKLY_SUPPORTS
            rationale = "来源或时间吻合度有限，只能提供弱支持。"

        supporting.append(
            EvidenceRef(evidence_id=eid, support=support, rationale=rationale)
        )

    for eid in counter_ids:
        if eid in {r.evidence_id for r in contradicting}:
            continue
        if ledger.get(eid) is None:
            continue
        contradicting.append(
            EvidenceRef(
                evidence_id=eid,
                support=SupportLevel.WEAKENS,
                rationale="该证据与当前解释存在冲突或削弱其成立条件。",
            )
        )

    return supporting, contradicting


def decide_status(
    checks: List[DriverCheck],
    supporting: List[EvidenceRef],
    ledger: EvidenceLedger,
) -> Tuple[DriverStatus, List[str]]:
    """判定驱动因素状态，并列出仍未解决的问题。"""
    by_key = {c.key: c for c in checks}
    unresolved: List[str] = []

    if not ledger.refs_can_support(supporting):
        unresolved.append(
            "当前没有任何来源可确认的证据能够支撑这一解释"
            "（来源无法确认的信息只能作为检索线索）。"
        )
        return DriverStatus.INSUFFICIENT, unresolved

    passed = sum(1 for c in checks if c.result == CheckResult.PASS)
    failed = [c for c in checks if c.result == CheckResult.FAIL]
    for check in failed:
        unresolved.append(f"{check.label}验证未通过：{check.reasoning}")

    for check in checks:
        if check.result == CheckResult.PARTIAL:
            unresolved.append(f"{check.label}仅部分成立：{check.reasoning}")
        elif check.result == CheckResult.UNKNOWN:
            unresolved.append(f"{check.label}无法判定：{check.reasoning}")

    if passed >= _VALID_PASS_THRESHOLD:
        return DriverStatus.SUPPORTED, unresolved

    if failed:
        return DriverStatus.INSUFFICIENT, unresolved

    unknown_critical = [
        k for k in _CRITICAL_CHECKS if k in by_key and by_key[k].result == CheckResult.UNKNOWN
    ]
    if len(unknown_critical) >= 2:
        return DriverStatus.INSUFFICIENT, unresolved

    return DriverStatus.PARTIALLY_SUPPORTED, unresolved


def assemble_driver(
    index: int,
    proposal: DriverProposal,
    cluster: ClusterInfo,
    checks: List[DriverCheck],
    supporting: List[EvidenceRef],
    contradicting: List[EvidenceRef],
    status: DriverStatus,
    unresolved: List[str],
) -> Driver:
    return Driver(
        id=f"D{index}",
        name=proposal.name,
        category=proposal.category,
        category_label=CATEGORY_LABELS[proposal.category.value],
        status=status,
        status_label=DRIVER_STATUS_LABELS[status.value],
        summary=proposal.summary,
        relevance=proposal.relevance,
        supporting_refs=supporting,
        contradicting_refs=contradicting,
        checks=checks,
        unresolved=unresolved,
    )


def enters_stage_three(driver: Driver) -> bool:
    """只有支持和部分支持的驱动因素才进入第三阶段。"""
    return driver.status in (DriverStatus.SUPPORTED, DriverStatus.PARTIALLY_SUPPORTED)
