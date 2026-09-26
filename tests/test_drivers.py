"""驱动因素状态：四项验证里至少三项吻合即视为有效因素。"""

from __future__ import annotations

from app.agent.drivers import decide_status
from app.contracts import CheckResult, DriverStatus, EvidenceRef, SupportLevel
from app.schemas import DriverCheck


class _Ledger:
    def refs_can_support(self, refs) -> bool:
        return bool(refs)


def _check(key: str, result: CheckResult) -> DriverCheck:
    return DriverCheck(
        key=key,
        label=key,
        result=result,
        result_label=result.value,
        reasoning="",
    )


def _refs() -> list:
    return [EvidenceRef(evidence_id="E1", support=SupportLevel.SUPPORTS, rationale="")]


def _status(*results: CheckResult) -> DriverStatus:
    keys = ("timing", "cross_section", "specificity", "mechanism")
    checks = [_check(k, r) for k, r in zip(keys, results)]
    status, _ = decide_status(checks, _refs(), _Ledger())
    return status


def test_three_passes_count_as_valid_even_if_one_fails():
    assert (
        _status(
            CheckResult.FAIL,
            CheckResult.PASS,
            CheckResult.PASS,
            CheckResult.PASS,
        )
        == DriverStatus.SUPPORTED
    )


def test_four_passes_are_supported():
    assert (
        _status(
            CheckResult.PASS,
            CheckResult.PASS,
            CheckResult.PASS,
            CheckResult.PASS,
        )
        == DriverStatus.SUPPORTED
    )


def test_two_passes_and_a_fail_are_not_valid():
    assert (
        _status(
            CheckResult.FAIL,
            CheckResult.PASS,
            CheckResult.PASS,
            CheckResult.PARTIAL,
        )
        == DriverStatus.INSUFFICIENT
    )


def test_two_passes_without_fail_stay_partial():
    assert (
        _status(
            CheckResult.PASS,
            CheckResult.PASS,
            CheckResult.PARTIAL,
            CheckResult.PARTIAL,
        )
        == DriverStatus.PARTIALLY_SUPPORTED
    )


def test_no_confirmable_evidence_is_insufficient():
    checks = [
        _check("timing", CheckResult.PASS),
        _check("cross_section", CheckResult.PASS),
        _check("specificity", CheckResult.PASS),
        _check("mechanism", CheckResult.PASS),
    ]
    status, unresolved = decide_status(checks, [], _Ledger())
    assert status == DriverStatus.INSUFFICIENT
    assert any("来源可确认" in item for item in unresolved)
