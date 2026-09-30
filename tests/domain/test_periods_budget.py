"""Dönem, kesim takvimi ve işletme projesi durumları (docs/04 §3, §7.2)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.charging.budget import (
    check_finalize,
    check_notify,
    ensure_editable,
)
from site_yonetim.domain.charging.periods import (
    YearMonth,
    already_posted_message,
    charge_description,
    default_dates,
    is_item_due,
    next_period,
    period_amount,
    reversal_description,
)
from site_yonetim.domain.finance import BudgetStatus, FinanceRuleError, Frequency


def test_year_month() -> None:
    assert YearMonth(2026, 12).next() == YearMonth(2027, 1)
    assert YearMonth(2027, 1).previous() == YearMonth(2026, 12)
    assert YearMonth(2026, 5).previous() == YearMonth(2026, 4)
    assert YearMonth(2026, 9).name == "09/2026"
    assert YearMonth(2026, 2).last_day == date(2026, 2, 28)
    assert YearMonth.of(date(2026, 9, 30)) == YearMonth(2026, 9)
    with pytest.raises(ValueError, match="Ay"):
        YearMonth(2026, 13)


def test_3_10_sonraki_donem_ters_kayittan_etkilenmez() -> None:
    """Eylül ters kaydedilince geçerli koşu yok → sıradaki dönem bu ay (eylül), ekim değil."""
    assert next_period(None, date(2026, 9, 30)) == YearMonth(2026, 9)
    assert next_period(YearMonth(2026, 8), date(2026, 9, 30)) == YearMonth(2026, 9)


def test_default_dates() -> None:
    assert default_dates(YearMonth(2026, 9)) == (date(2026, 9, 1), date(2026, 9, 15))


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        (Frequency.MONTHLY, "83.33"),
        (Frequency.QUARTERLY, "250.00"),
        (Frequency.YEARLY, "1000.00"),
        (Frequency.ONE_TIME, "1000.00"),
    ],
)
def test_period_amount(frequency: Frequency, expected: str) -> None:
    assert period_amount(Decimal(1000), frequency) == Decimal(expected)


@pytest.mark.parametrize(
    ("frequency", "month", "charged_before", "due"),
    [
        (Frequency.MONTHLY, 5, False, True),
        (Frequency.QUARTERLY, 1, False, True),
        (Frequency.QUARTERLY, 4, False, True),
        (Frequency.QUARTERLY, 5, False, False),
        (Frequency.QUARTERLY, 10, False, True),
        (Frequency.YEARLY, 1, False, True),
        (Frequency.YEARLY, 2, False, False),
        (Frequency.ONE_TIME, 7, False, True),
        (Frequency.ONE_TIME, 8, True, False),
    ],
)
def test_item_due_calendar(
    frequency: Frequency, month: int, charged_before: bool, due: bool
) -> None:
    period = YearMonth(2026, month)
    assert is_item_due(frequency, period, fiscal_year=2026, charged_before=charged_before) is due


def test_yearly_item_only_in_fiscal_years_january() -> None:
    assert not is_item_due(
        Frequency.YEARLY, YearMonth(2027, 1), fiscal_year=2026, charged_before=False
    )


def test_descriptions() -> None:
    period = YearMonth(2026, 9)
    assert charge_description(period, ["Aidat"]) == "09/2026 tahakkuku — Aidat"
    assert charge_description(period, ["a", "b", "c"]) == "09/2026 tahakkuku — 3 kalem"
    assert reversal_description(period) == "TERS KAYIT — 09/2026 tahakkuku iptali"
    assert already_posted_message(period, date(2026, 9, 15), "14:30").startswith(
        "09/2026 dönemi için tahakkuk zaten kesilmiş (15.09.2026 14:30)."
    )


# --- işletme projesi -------------------------------------------------------------


def test_only_draft_is_editable() -> None:
    ensure_editable(BudgetStatus.DRAFT)
    for status in (BudgetStatus.NOTIFIED, BudgetStatus.FINALIZED, BudgetStatus.SUPERSEDED):
        with pytest.raises(FinanceRuleError) as caught:
            ensure_editable(status)
        assert caught.value.code == "budget_not_editable"


def test_notify_sets_objection_deadline() -> None:
    deadline = check_notify(BudgetStatus.DRAFT, 3, date(2026, 9, 1), date(2026, 9, 2))
    assert deadline == date(2026, 9, 8)


@pytest.mark.parametrize(
    ("status", "items", "notified_on", "code"),
    [
        (BudgetStatus.NOTIFIED, 3, date(2026, 9, 1), "budget_not_draft"),
        (BudgetStatus.DRAFT, 0, date(2026, 9, 1), "budget_empty"),
        (BudgetStatus.DRAFT, 3, date(2026, 9, 3), "notified_in_future"),
    ],
)
def test_notify_rejections(status: BudgetStatus, items: int, notified_on: date, code: str) -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_notify(status, items, notified_on, date(2026, 9, 2))
    assert caught.value.code == code


def test_finalize_after_objection_period() -> None:
    deadline = date(2026, 9, 8)
    with pytest.raises(FinanceRuleError) as caught:
        check_finalize(BudgetStatus.NOTIFIED, deadline, deadline)
    assert caught.value.code == "objection_period_open"
    assert "08.09.2026" in caught.value.message
    check_finalize(BudgetStatus.NOTIFIED, deadline, date(2026, 9, 9))
    with pytest.raises(FinanceRuleError) as caught:
        check_finalize(BudgetStatus.DRAFT, None, date(2026, 9, 9))
    assert caught.value.code == "budget_not_notified"
