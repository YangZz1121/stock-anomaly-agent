"""把驱动因素压成用户能扫读的卡片。

对照常见个股 App 的内容块（极性标签 + 短标题 + 影响度星级 + 一两句观点），
只用规则层已有字段，不再调用模型、也不做百分比归因。

解释度：可以直接映射。
影响度：只能模拟——产品刻意不做「这个因素贡献了多少涨跌」。
"""

from __future__ import annotations

from typing import Optional, Tuple

from app.contracts import (
    CheckResult,
    DriverCategory,
    DriverStatus,
    EvidenceStrength,
    ImpactDirection,
    ImpactHorizon,
)

POLARITY_LABELS = {
    "positive": "利好",
    "negative": "利空",
    "mixed": "利弊并存",
    "uncertain": "待确认",
}


def decorate_driver_card(driver) -> None:
    """就地写入卡片字段，供第三部分展示。"""
    polarity, polarity_label = _polarity(driver)
    explain_stars, explain_reason = _explain_stars(driver)
    impact_stars, impact_reason = _impact_stars(driver, polarity)
    driver.polarity = polarity
    driver.polarity_label = polarity_label
    driver.thesis = _thesis(driver, polarity_label)
    driver.viewpoint = _viewpoint(driver)
    driver.explain_stars = explain_stars
    driver.explain_reason = explain_reason
    driver.impact_stars = impact_stars
    driver.impact_reason = impact_reason
    # 只有方向已确认的因子才进入第三部分；待确认不是影响因子。
    driver.direction_confirmed = polarity in ("positive", "negative", "mixed")


def _polarity(driver) -> Tuple[str, str]:
    assessment = getattr(driver, "assessment", None)
    if assessment is None or assessment.display_suppressed:
        return "uncertain", POLARITY_LABELS["uncertain"]
    mapping = {
        ImpactDirection.POSITIVE: "positive",
        ImpactDirection.NEGATIVE: "negative",
        ImpactDirection.MIXED: "mixed",
        ImpactDirection.UNCERTAIN: "uncertain",
    }
    key = mapping.get(assessment.direction, "uncertain")
    if key in ("positive", "negative") and assessment.strength == EvidenceStrength.PARTIAL:
        return key, "倾向" + POLARITY_LABELS[key]
    return key, POLARITY_LABELS[key]


def _explain_stars(driver) -> Tuple[int, str]:
    """解释度：部分支持 = 3 星。这是对已有状态的直接翻译，不是新评分。"""
    by_key = {c.key: c.result for c in (driver.checks or [])}
    passed = sum(1 for result in by_key.values() if result == CheckResult.PASS)
    total = max(1, len(by_key))

    if driver.status == DriverStatus.SUPPORTED:
        if passed == total:
            return 5, "四项验证均通过，该因素可以较完整地解释本次价格变化。"
        return 4, f"四项中已有 {passed} 项吻合，已识别为有效因素，但仍有单项未完全通过。"
    if driver.status == DriverStatus.PARTIALLY_SUPPORTED:
        return 3, "部分支持，对应解释度三星：能解释一部分，但不能独立说清全部变化。"
    if any(result != CheckResult.FAIL for result in by_key.values()):
        return 2, "证据不足，只有弱对应，解释度两星。"
    return 1, "关键验证未通过或缺少可确认证据，解释度一星。"


def _impact_stars(driver, polarity: str) -> Tuple[int, str]:
    """影响度只能模拟。缺少的是「该因素解释了多少涨跌」的定量拆分。"""
    score = {
        DriverCategory.COMPANY: 3,
        DriverCategory.INDUSTRY: 3,
        DriverCategory.MARKET: 2,
        DriverCategory.TRADING: 2,
    }.get(driver.category, 2)

    reasons = [f"起点按{driver.category_label}层赋值 {score} 星"]
    assessment = getattr(driver, "assessment", None)

    if driver.status == DriverStatus.SUPPORTED:
        score += 1
        reasons.append("验证通过 +1")
    elif driver.status == DriverStatus.INSUFFICIENT:
        score -= 1
        reasons.append("证据不足 −1")

    if assessment is not None and not assessment.display_suppressed:
        if assessment.horizon == ImpactHorizon.STRUCTURAL:
            score += 1
            reasons.append("结构性影响 +1")
        elif assessment.horizon == ImpactHorizon.ONE_OFF:
            score -= 1
            reasons.append("一次性冲击 −1")
        if assessment.amplifying_factors:
            score += 1
            reasons.append("存在放大因素 +1")

    by_key = {c.key: c.result for c in (driver.checks or [])}
    specificity = by_key.get("specificity")
    if driver.category == DriverCategory.COMPANY and specificity == CheckResult.PASS:
        score += 1
        reasons.append("公司特异性成立 +1")
    if driver.category in (DriverCategory.MARKET, DriverCategory.INDUSTRY) and specificity == CheckResult.PARTIAL:
        score -= 1
        reasons.append("无法覆盖个股额外变化 −1")

    if polarity == "uncertain":
        score = min(score, 3)
        reasons.append("方向未确认，影响度上限 3 星")

    stars = max(1, min(5, score))
    return stars, "模拟划分，不是涨跌贡献百分比。" + "；".join(reasons) + "。"


def _thesis(driver, polarity_label: str) -> str:
    topic = _short_topic(driver.name)
    return f"{polarity_label}：{topic}"


def _short_topic(name: str, limit: int = 18) -> str:
    text = (name or "").strip()
    for junk in ("征求意见稿", "发布解读", "最新消息", "报道称"):
        text = text.replace(junk, "")
    text = text.strip("：:，, ")
    if len(text) <= limit:
        return text or "相关因素"
    return text[: limit - 1] + "…"


def _viewpoint(driver) -> str:
    assessment = getattr(driver, "assessment", None)
    if assessment is not None:
        if assessment.display_suppressed:
            return "方向尚未被证据链确认，这里只保留事件要点，不作为利好或利空结论。"
        for candidate in (assessment.direction_reason, _first_chain(assessment)):
            sentence = _one_sentence(candidate)
            if sentence:
                return sentence
    return _one_sentence(driver.summary) or "该因素与本次异动相关，但还缺少可写成观点的一句话。"


def _first_chain(assessment) -> str:
    for step in assessment.chain or []:
        text = (step.text or "").strip()
        if text.startswith("事件事实") or text.startswith("公司暴露"):
            continue
        if "中断" in text:
            continue
        return text
    return ""


def _one_sentence(text: Optional[str], limit: int = 72) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    sentence = raw.split("。")[0].strip()
    if not sentence:
        return ""
    if len(sentence) > limit:
        sentence = sentence[: limit - 1] + "…"
    return sentence + "。"
