"""证据链断环诊断与定向补证。

五环：事件事实 → 公司暴露 → 作用机制 → 影响方向 → 影响期限。
缺哪一环就按那一环去补检，补完再重验。宏观因素禁止用年报伪造暴露。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional, Sequence

from pydantic import BaseModel, Field

from app.contracts import (
    CheckResult,
    DriverCategory,
    DriverStatus,
    ExposureLevel,
    SourceTier,
)

LINK_EVENT = "event"
LINK_EXPOSURE = "exposure"
LINK_MECHANISM = "mechanism"
LINK_DIRECTION = "direction"
LINK_HORIZON = "horizon"

LINK_ORDER = (
    LINK_EVENT,
    LINK_EXPOSURE,
    LINK_MECHANISM,
    LINK_DIRECTION,
    LINK_HORIZON,
)

LINK_LABELS = {
    LINK_EVENT: "事件事实",
    LINK_EXPOSURE: "公司暴露",
    LINK_MECHANISM: "作用机制",
    LINK_DIRECTION: "影响方向",
    LINK_HORIZON: "影响期限",
}

# 定向补证用词：只覆盖检索意图，不发明具体公司或政策名称
PATCH_TERMS = {
    LINK_EVENT: ["公告", "官方", "发布", "澄清"],
    LINK_EXPOSURE: ["年报", "年度报告", "半年报", "主营", "营收占比", "业务构成"],
    LINK_MECHANISM: ["业务", "经营", "产品", "影响"],
}

_EXPOSURE_TERMS = set(PATCH_TERMS[LINK_EXPOSURE])

_STOP_TERMS = {
    "征求意见",
    "征求意见稿",
    "最新消息",
    "报道称",
    "发布解读",
    "公司公告",
    "年度报告",
    "驱动因素",
    "相关因素",
}


class PatchPlan(BaseModel):
    scopes: List[str]
    extra_terms: List[str] = Field(default_factory=list)
    reason: str = ""
    missing_links: List[str] = Field(default_factory=list)
    needs_exposure_lookback: bool = False


def diagnose_patch(memory: Any) -> Optional[PatchPlan]:
    """评估后、传导前：看哪些已立项的驱动因素还能定向补证。"""
    drivers = getattr(memory, "drivers", None) or []
    drafts = getattr(memory, "drafts", None) or {}
    if not drivers or not drafts:
        return None

    missing: List[str] = []
    scopes: List[str] = []
    terms: List[str] = []
    notes: List[str] = []

    for driver in drivers:
        packed = drafts.get(driver.id)
        if packed is None:
            continue
        proposal, cluster, _checks, mechanism = packed
        links = diagnose_driver_gaps(driver, proposal, cluster, mechanism)
        if not links:
            continue
        missing.extend(links)
        notes.append(
            f"{driver.name}缺" + "、".join(LINK_LABELS[link] for link in links)
        )
        for link in links:
            scopes.extend(_scopes_for(link, cluster.scope))
            terms.extend(PATCH_TERMS.get(link, []))
        terms.extend(_terms_from_text(proposal.name, cluster.title, cluster.summary))

    scopes = _dedupe(scopes)
    terms = _dedupe(terms)[:8]
    missing = _dedupe(missing)
    if not scopes or not missing:
        return None

    return PatchPlan(
        scopes=scopes,
        extra_terms=terms,
        reason="定向补证：" + "；".join(notes[:3]),
        missing_links=missing,
        needs_exposure_lookback=LINK_EXPOSURE in missing,
    )


def diagnose_driver_gaps(
    driver: Any,
    proposal: Any,
    cluster: Any,
    mechanism: Any,
) -> List[str]:
    """返回该驱动因素上*还能靠检索补*的断环。方向/期限不靠再搜新闻解决。"""
    gaps: List[str] = []
    by_key = {c.key: c.result for c in (driver.checks or [])}
    category = getattr(proposal, "category", None)
    mechanism_result = getattr(mechanism, "result", None)

    if getattr(cluster, "best_tier", None) == SourceTier.T4_UNVERIFIED:
        gaps.append(LINK_EVENT)
    elif not getattr(cluster, "can_support_conclusion", True):
        gaps.append(LINK_EVENT)

    # 时序对不上再搜也补不回来，机制已否决说明证据指向无关
    if by_key.get("mechanism") == CheckResult.FAIL:
        return gaps
    if by_key.get("timing") == CheckResult.FAIL:
        return gaps

    if category == DriverCategory.MARKET:
        return gaps

    company_event_is_exposure = category == DriverCategory.COMPANY and getattr(
        cluster, "best_tier", None
    ) in (SourceTier.T1_AUTHORITATIVE, SourceTier.T2_PROFESSIONAL)

    if not company_event_is_exposure and mechanism_result in (
        CheckResult.PARTIAL,
        CheckResult.UNKNOWN,
    ):
        gaps.append(LINK_EXPOSURE)

    if mechanism_result == CheckResult.UNKNOWN and LINK_EXPOSURE not in gaps:
        gaps.append(LINK_MECHANISM)

    if driver.status == DriverStatus.INSUFFICIENT and not gaps:
        return []
    return _dedupe(gaps)


def describe_gaps(memory: Any) -> List[str]:
    plan = diagnose_patch(memory)
    if plan is None:
        return []
    return [f"{LINK_LABELS.get(link, link)}" for link in plan.missing_links]


def ground_chain(
    steps: Sequence[Any],
    exposure_level: ExposureLevel,
    valid_ids: Optional[Iterable[str]] = None,
) -> List[Any]:
    """把传导步收敛成可验证的五环：缺环即停，不补后面的「若持续则」。"""
    allowed = set(valid_ids) if valid_ids is not None else None
    factory = type(steps[0]) if steps else None
    out: List[Any] = []
    seen: set[str] = set()

    def _make(**kwargs: Any) -> Any:
        if factory is None:
            return kwargs
        payload = {"text": "", "is_conditional": False, "link": "", "evidence_ids": [], "status": "present"}
        payload.update(kwargs)
        return factory(**payload)

    for raw in steps:
        text = str(getattr(raw, "text", "") or "").strip()
        if not text:
            continue
        link = getattr(raw, "link", None) or _infer_link(text)
        ids = [
            str(x)
            for x in (getattr(raw, "evidence_ids", None) or [])
            if str(x).strip()
        ]
        if allowed is not None:
            ids = [x for x in ids if x in allowed]
        status = getattr(raw, "status", None) or "present"
        if exposure_level == ExposureLevel.UNCONFIRMED and link == LINK_EXPOSURE:
            status = "missing"
        if status != "missing" and link == LINK_EXPOSURE and not ids:
            if exposure_level == ExposureLevel.UNCONFIRMED:
                status = "missing"

        if link in seen and status != "missing":
            continue
        if link in seen and status == "missing":
            break

        out.append(
            _make(
                text=text,
                is_conditional=bool(getattr(raw, "is_conditional", False)),
                link=link,
                evidence_ids=ids,
                status=status,
            )
        )
        seen.add(link)

        if status == "missing":
            if (
                exposure_level == ExposureLevel.UNCONFIRMED
                and link == LINK_EXPOSURE
                and not any("中断" in getattr(s, "text", "") for s in out)
            ):
                out.append(
                    _make(
                        text="由于公司暴露未确认，传导链在此中断，不继续推导基本面结果",
                        is_conditional=True,
                        link=LINK_MECHANISM,
                        status="missing",
                    )
                )
            break
        if exposure_level == ExposureLevel.UNCONFIRMED and link == LINK_EXPOSURE:
            break

    return out


def remaining_missing_links(assessment: Any) -> List[str]:
    if assessment is None:
        return []
    missing = list(getattr(assessment, "missing_links", None) or [])
    for step in getattr(assessment, "chain", None) or []:
        if getattr(step, "link_status", None) == "missing" or getattr(
            step, "status", None
        ) == "missing":
            label = LINK_LABELS.get(getattr(step, "link", ""), "")
            if label and label not in missing:
                missing.append(label)
    return missing


def _scopes_for(link: str, cluster_scope: str) -> List[str]:
    if link == LINK_EXPOSURE:
        return ["company"]
    if link == LINK_MECHANISM:
        return ["company"]
    if link == LINK_EVENT:
        scope = cluster_scope if cluster_scope in ("market", "industry", "company") else "company"
        return [scope]
    return []


def _terms_from_text(*texts: str) -> List[str]:
    blob = " ".join(t or "" for t in texts)
    parts = re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,12}", blob)
    return [p for p in parts if p not in _STOP_TERMS]


def _infer_link(text: str) -> str:
    head = text[:10]
    if head.startswith("事件"):
        return LINK_EVENT
    if "暴露" in head:
        return LINK_EXPOSURE
    if "方向" in head:
        return LINK_DIRECTION
    if "期限" in head:
        return LINK_HORIZON
    return LINK_MECHANISM


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def is_exposure_term(term: str) -> bool:
    return term in _EXPOSURE_TERMS
