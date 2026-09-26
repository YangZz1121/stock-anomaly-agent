"""第三类：合规边界。

产品边界（规划 28 章 / 赛题安全要求）：
不输出确定性涨跌预测、不做收益承诺、不给买卖建议；
事实、推断与不确定信息必须分开；证据不足时必须说不知道。

护栏测试分两层：一层验证护栏本身拦得住，另一层验证真实输出里
本来就没有违规表述——两层都要过，才能说这条边界是真的成立。
"""

from __future__ import annotations

import pytest

from app.contracts import (
    DriverStatus,
    EvidenceStrength,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
    ResearchWindow,
    SourceTier,
)
from app.engine import guardrails
from tests.conftest import all_free_text, research

SCENARIOS = [
    ("宁德时代", ResearchWindow.D5),
    ("贵州茅台", ResearchWindow.TODAY),
    ("伊利股份", ResearchWindow.TODAY),
    ("宁德时代", ResearchWindow.D3),
]

ADVICE_SAMPLES = [
    "综合来看建议买入并长期持有。",
    "给予买入评级，目标价 260 元。",
    "当前位置值得加仓，逢低吸纳。",
]

PREDICTION_SAMPLES = [
    "股价将在下周反弹至 250 元。",
    "该股必然上涨，预计收益率可达 30%。",
    "目标价：300 元。",
    "这笔投资稳赚不赔。",
]


@pytest.mark.parametrize("text", ADVICE_SAMPLES)
def test_guardrails_catch_investment_advice(text):
    cleaned, hits = guardrails.sanitize(text)
    assert "investment_advice" in hits
    assert cleaned == guardrails.REDACTION


@pytest.mark.parametrize("text", PREDICTION_SAMPLES)
def test_guardrails_catch_price_predictions(text):
    cleaned, hits = guardrails.sanitize(text)
    assert "price_prediction" in hits
    assert cleaned == guardrails.REDACTION


def test_guardrails_replace_the_whole_sentence():
    """半截的买卖建议读起来仍然像建议，所以必须整句替换。"""
    cleaned, _ = guardrails.sanitize("基于以上分析，建议买入该股票。")
    assert "买入" not in cleaned


def test_guardrails_do_not_touch_legitimate_research_language():
    """护栏不能误伤正常的研究表述，否则会逼着产品说不清话。"""
    legit = [
        "若该因素持续，相关业务的经营条件面临压力。",
        "现有证据不足以判断影响期限。",
        "公司年度报告显示境外收入占比 32.6%。",
        "该事件对公司基本面的影响方向倾向负向。",
    ]
    for text in legit:
        cleaned, hits = guardrails.sanitize(text)
        assert hits == []
        assert cleaned == text


@pytest.mark.parametrize("query,window", SCENARIOS)
def test_no_scenario_output_contains_advice_or_prediction(query, window):
    brief = research(query, window)
    offenders = [
        (text, guardrails.scan_text(text))
        for text in all_free_text(brief)
        if guardrails.scan_text(text)
    ]
    assert offenders == [], f"输出中出现违规表述：{offenders}"


@pytest.mark.parametrize("query,window", SCENARIOS)
def test_disclaimers_always_ship_with_the_brief(query, window):
    brief = research(query, window)
    assert brief.disclaimers
    assert any("不构成投资建议" in d for d in brief.disclaimers)


def test_insufficient_evidence_suppresses_direction_and_horizon():
    """证据不足时不允许还摆出一个方向和期限，哪怕模型给了。"""
    brief = research("贵州茅台", ResearchWindow.TODAY)
    weak = [
        d
        for d in brief.why_happened.drivers
        if d.assessment and d.assessment.strength == EvidenceStrength.INSUFFICIENT
    ]
    assert weak, "该场景本应产生证据不足的因素"
    for driver in weak:
        a = driver.assessment
        assert a.display_suppressed is True
        assert a.suppression_reason
        assert "暂无法可靠判断" in a.display_direction
        assert "暂无法可靠判断" in a.display_horizon


def test_unverified_sources_never_reach_the_third_stage():
    """T4 网传消息只能当检索线索，不能进入基本面影响评估。"""
    brief = research("宁德时代", ResearchWindow.D5)
    tiers = {e.id: e.source_tier for e in brief.evidence}
    for driver in brief.why_happened.drivers:
        best = min(
            (
                list(SourceTier).index(tiers[ref.evidence_id])
                for ref in driver.supporting_refs
            ),
            default=None,
        )
        if best is None:
            continue
        if list(SourceTier)[best] == SourceTier.T4_UNVERIFIED:
            assert driver.status == DriverStatus.INSUFFICIENT
            assert driver.assessment is None


def test_market_wide_factors_are_not_dressed_up_as_company_exposure():
    """风险偏好回落对所有公司都成立，不能拿一份公司公告去"确认"它。"""
    brief = research("伊利股份", ResearchWindow.TODAY)
    market = next(
        d for d in brief.why_happened.drivers if d.category.value == "market"
    )
    assert market.assessment is not None
    assert market.assessment.exposure_level == ExposureLevel.UNCONFIRMED
    assert market.assessment.exposure_refs == []


def test_structural_horizon_requires_filed_exposure():
    """结构性影响门槛很高，光靠媒体报道推不出来。"""
    for query, window in SCENARIOS:
        brief = research(query, window)
        for driver in brief.why_happened.drivers:
            a = driver.assessment
            if a is None or a.horizon != ImpactHorizon.STRUCTURAL:
                continue
            assert a.exposure_level in (
                ExposureLevel.P1_FILING,
                ExposureLevel.P2_COMPANY,
            )


def test_no_percentage_attribution_of_price_movement():
    """三层对比是研究优先级，不是因果拆分，产品必须自己说清楚。"""
    brief = research("宁德时代", ResearchWindow.D5)
    assert "不代表各层对本次价格变化的因果贡献" in (
        brief.what_happened.comparison.disclaimer
    )
    for text in all_free_text(brief):
        assert "贡献" not in text or "因果贡献" in text


def test_conclusions_about_fundamentals_not_about_share_price():
    """第三阶段判断的是经营影响，不是股价走势。"""
    brief = research("伊利股份", ResearchWindow.TODAY)
    for driver in brief.why_happened.drivers:
        a = driver.assessment
        if a is None:
            continue
        assert a.direction in set(ImpactDirection)
        for step in a.chain:
            assert "股价" not in step.text


def test_conditional_chain_steps_are_marked_as_inference():
    """推断必须和事实分开标记，否则读者会把假设当结论。"""
    brief = research("宁德时代", ResearchWindow.D5)
    marked = 0
    for driver in brief.why_happened.drivers:
        if driver.assessment is None:
            continue
        for step in driver.assessment.chain:
            if step.text.startswith("若"):
                assert step.is_conditional, f"条件句未标记为推断：{step.text}"
                marked += 1
    assert marked > 0
