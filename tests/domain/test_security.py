"""Kapı işleri — saf kurallar (docs/06 §2.12)."""

import pytest

from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import (
    PackageStatus,
    VisitorStatus,
    check_delivery,
    check_enter,
    check_exit,
)
from site_yonetim.services.security import new_pickup_code


def test_pickup_code_is_four_digits() -> None:
    codes = {new_pickup_code() for _ in range(500)}
    assert all(len(c) == 4 and c.isdigit() and c[0] != "0" for c in codes)
    assert len(codes) > 100  # rastgele


def test_delivery_rules() -> None:
    check_delivery(PackageStatus.WAITING, "4821", " 4821 ")
    with pytest.raises(OperationRuleError) as caught:
        check_delivery(PackageStatus.WAITING, "4821", "4822")
    assert (caught.value.code, caught.value.field) == ("wrong_pickup_code", "pickup_code")
    for status in (PackageStatus.DELIVERED, PackageStatus.RETURNED):
        with pytest.raises(OperationRuleError) as caught:
            check_delivery(status, "4821", "4821")
        assert caught.value.conflict is True


def test_visitor_transitions() -> None:
    check_enter(VisitorStatus.EXPECTED)
    check_exit(VisitorStatus.ENTERED)
    for status in (VisitorStatus.ENTERED, VisitorStatus.EXITED, VisitorStatus.DENIED):
        with pytest.raises(OperationRuleError):
            check_enter(status)
    for status in (VisitorStatus.EXPECTED, VisitorStatus.EXITED):
        with pytest.raises(OperationRuleError):
            check_exit(status)
