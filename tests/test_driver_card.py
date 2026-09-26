"""第三部分卡片：极性、短观点、解释度 / 影响度星级。"""

from __future__ import annotations

from app.contracts import (
    CheckResult,
    DriverCategory,
    DriverStatus,
    EvidenceStrength,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
)
from app.engine.driver_card import decorate_driver_card
from app.schemas import Driver, DriverCheck, FundamentalAssessment, TransmissionStep


def _check(key: str, result: CheckResult) -> DriverCheck:
    return DriverCheck(key=key, label=key, result=result, result_label=result.value, reasoning="")


def _driver(**kwargs) -> Driver:
    base = dict(
        id="D1",
        name="某出口目的地市场发布动力电池准入新规征求意见稿",
        category=DriverCategory.INDUSTRY,
        category_label="行业",
        status=DriverStatus.PARTIALLY_SUPPORTED,
        status_label="部分支持",
        summary="征求意见稿拟提高进口动力电池的本地化含量与碳足迹披露要求，设置 18 个月过渡期。",
        relevance="发生在核心证据窗口内；2 个独立信源确认。",
        checks=[
            _check("timing", CheckResult.PASS),
            _check("cross_section", CheckResult.PARTIAL),
            _check("specificity", CheckResult.PARTIAL),
            _check("mechanism", CheckResult.PASS),
        ],
    )
    base.update(kwargs)
    return Driver(**base)


def test_partial_support_maps_to_three_explain_stars():
    driver = _driver()
    decorate_driver_card(driver)
    assert driver.explain_stars == 3
    assert "三星" in driver.explain_reason


def test_full_support_maps_to_five_explain_stars():
    driver = _driver(
        status=DriverStatus.SUPPORTED,
        checks=[
            _check("timing", CheckResult.PASS),
            _check("cross_section", CheckResult.PASS),
            _check("specificity", CheckResult.PASS),
            _check("mechanism", CheckResult.PASS),
        ],
    )
    decorate_driver_card(driver)
    assert driver.explain_stars == 5


def test_three_passes_as_supported_maps_to_four_explain_stars():
    driver = _driver(
        status=DriverStatus.SUPPORTED,
        checks=[
            _check("timing", CheckResult.FAIL),
            _check("cross_section", CheckResult.PASS),
            _check("specificity", CheckResult.PASS),
            _check("mechanism", CheckResult.PASS),
        ],
    )
    decorate_driver_card(driver)
    assert driver.explain_stars == 4
    assert "有效因素" in driver.explain_reason


def test_suppressed_assessment_is_not_shown_as_good_or_bad():
    driver = _driver(
        assessment=FundamentalAssessment(
            exposure_level=ExposureLevel.UNCONFIRMED,
            exposure_label="无法确认",
            exposure_basis="",
            direction=ImpactDirection.NEGATIVE,
            horizon=ImpactHorizon.PHASED,
            strength=EvidenceStrength.INSUFFICIENT,
            display_direction="影响方向：暂无法可靠判断",
            display_horizon="影响期限：暂无法可靠判断",
            display_strength="证据不足",
            display_headline="",
            display_suppressed=True,
        )
    )
    decorate_driver_card(driver)
    assert driver.polarity == "uncertain"
    assert driver.polarity_label == "待确认"
    assert driver.direction_confirmed is False
    assert "不作为利好或利空" in driver.viewpoint


def test_card_uses_short_thesis_not_full_news():
    driver = _driver(
        assessment=FundamentalAssessment(
            exposure_level=ExposureLevel.P1_FILING,
            exposure_label="正式披露确认",
            exposure_basis="年报确认出口业务。",
            direction=ImpactDirection.NEGATIVE,
            horizon=ImpactHorizon.PHASED,
            strength=EvidenceStrength.PARTIAL,
            display_direction="倾向负向",
            display_horizon="阶段性",
            display_strength="部分充分",
            display_headline="倾向负向 · 阶段性 · 部分充分",
            direction_reason="准入门槛提高，出口业务经营条件承压。",
            chain=[TransmissionStep(text="若该因素持续，相关业务的经营条件面临压力")],
        )
    )
    decorate_driver_card(driver)
    assert driver.polarity == "negative"
    assert driver.direction_confirmed is True
    assert driver.thesis.startswith("倾向利空")
    assert "征求意见稿" not in driver.thesis
    assert len(driver.viewpoint) < 80
    assert "18 个月过渡期" not in driver.viewpoint
    assert 1 <= driver.impact_stars <= 5
    assert "模拟划分" in driver.impact_reason
