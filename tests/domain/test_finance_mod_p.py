"""Finans Mod P saf kuralları: dönem kilidi, finans parametreleri, ödeme planı, borç takibi,
muhasebe fiş dosyası."""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from site_yonetim.domain.accounting import (
    GenericCsvAdapter,
    GenericXlsxAdapter,
    Voucher,
    check_code,
)
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.debt_tracking import (
    AgingBucket,
    InstallmentStatus,
    PlanStatus,
    ReminderStage,
    add_months,
    bucket_of,
    check_action,
    check_letter,
    check_plan,
    due_stages,
    installment_status,
    plan_status,
    schedule,
)
from site_yonetim.domain.finance import FinanceRuleError, PeriodStatus
from site_yonetim.domain.finance_params import DEFAULTS, changed_keys, check_effective, merge
from site_yonetim.domain.fiscal import (
    CheckResult,
    CloseItem,
    check_can_start_close,
    check_unlock_request,
    clean_exceptions,
    earliest_open,
    effective_status,
    locked_error,
    missing_items,
    parse_period_key,
    period_label,
)
from site_yonetim.domain.members import FormFieldError

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


# --- Dönem kilidi -------------------------------------------------------------------------


def test_period_key_and_label() -> None:
    assert parse_period_key("2026-09") == YearMonth(2026, 9)
    for bad in ("2026-13", "26-09", "2026/09", "abcd-ef", "1999-01"):
        assert parse_period_key(bad) is None
    assert period_label(YearMonth(2026, 9)) == "Eylül 2026"


def test_temp_open_expires_and_earliest_open_skips_blocked() -> None:
    later, earlier = NOW + timedelta(hours=1), NOW - timedelta(seconds=1)
    assert effective_status("temp_open", later, NOW) is PeriodStatus.TEMP_OPEN
    assert effective_status("temp_open", earlier, NOW) is PeriodStatus.LOCKED
    assert effective_status("closing", None, NOW) is PeriodStatus.CLOSING
    blocked = {YearMonth(2026, 10), YearMonth(2026, 11)}
    assert earliest_open(YearMonth(2026, 9), blocked) == YearMonth(2026, 12)


def test_locked_error_messages() -> None:
    error = locked_error(date(2026, 9, 5), PeriodStatus.LOCKED, YearMonth(2026, 10))
    assert (error.code, error.conflict) == ("period_locked", True)
    assert error.message == (
        "Tarih kilitli döneme düşüyor (Eylül 2026); kayıt yazılamaz — en erken Ekim 2026 "
        "(01.10.2026)."
    )
    closing = locked_error(date(2026, 9, 5), PeriodStatus.CLOSING, YearMonth(2026, 10))
    assert "kapanışta" in closing.message
    original = locked_error(
        date(2026, 9, 5), PeriodStatus.LOCKED, YearMonth(2026, 10), original=True
    )
    assert "ters kaydedilemez" in original.message


def test_checklist_exceptions_and_transitions() -> None:
    results = [
        CheckResult(CloseItem.CHARGE, False, "yok"),
        CheckResult(CloseItem.CASH_DAYS, True, "tamam"),
        CheckResult(CloseItem.EXPENSES, False, "uyarı"),  # zorunlu değil
    ]
    cleaned = clean_exceptions({"charge": "  kısa  ", "unknown": "yoksayılır", "bank": " "})
    assert cleaned == {"charge": "kısa"}
    assert [r.item for r in missing_items(results, cleaned)] == [CloseItem.CHARGE]
    assert missing_items(results, {"charge": "Eski sistemde kesildi"}) == []
    with pytest.raises(FinanceRuleError, match="İçinde bulunulan"):
        check_can_start_close(PeriodStatus.OPEN, YearMonth(2026, 10), date(2026, 10, 26))
    with pytest.raises(FinanceRuleError, match="Yalnız açık"):
        check_can_start_close(PeriodStatus.LOCKED, YearMonth(2026, 9), date(2026, 10, 26))
    check_can_start_close(PeriodStatus.OPEN, YearMonth(2026, 9), date(2026, 10, 26))
    assert check_unlock_request(" Banka masrafı sonradan geldi, düzeltilecek ", 24)[1] == 24
    with pytest.raises(FinanceRuleError):
        check_unlock_request("Banka masrafı sonradan geldi, düzeltilecek", 5)
    with pytest.raises(FinanceRuleError):
        check_unlock_request("kısa", 4)


# --- Finans parametreleri ---------------------------------------------------------------


def test_defaults_reflect_pm_decisions() -> None:
    """K9 gün bazlı kıst, K25 ayın 10'u, K23 işletme projesi, K2 gecikme kapalı, K24 önce
    gecikme, K11 kısıt kapalı."""
    assert DEFAULTS["proration"] == "day"
    assert DEFAULTS["due_day"] == 10
    assert DEFAULTS["charge_source"] == "budget_plan"
    assert DEFAULTS["late_fee_enabled"] is False
    assert DEFAULTS["allocation_order"] == "late_fee_first"
    assert DEFAULTS["debtor_restriction_enabled"] is False


def test_merge_validates_and_normalizes() -> None:
    values, errors = merge({}, {"late_fee_monthly_rate_percent": "4.5", "due_day": 15})
    assert errors == {}
    assert values["late_fee_monthly_rate_percent"] == "4.50"
    assert changed_keys({}, values) == ["due_day", "late_fee_monthly_rate_percent"]
    _, errors = merge(
        {},
        {
            "reminder_days": [3, 3, 30],
            "demand_letter_day": 400,
            "proration": "weekly",
            "late_fee_monthly_rate_percent": "5.01",
            "plan_suspends_late_fee": "evet",
            "debtor_restriction_min_amount": "-1",
            "nope": 1,
        },
    )
    assert set(errors) == {
        "reminder_days",
        "demand_letter_day",
        "proration",
        "late_fee_monthly_rate_percent",
        "plan_suspends_late_fee",
        "debtor_restriction_min_amount",
        "nope",
    }


def test_financial_change_only_from_next_period() -> None:
    june = YearMonth(2026, 6)
    assert check_effective(june, june, ["proration"]) == {"effective_period": "financial"}
    assert check_effective(june.next(), june, ["proration"]) == {}
    assert check_effective(june, june, ["reminder_days"]) == {}
    assert check_effective(june.previous(), june, ["reminder_days"]) == {"effective_period": "past"}


# --- Ödeme planı ve borç takibi ---------------------------------------------------------


def test_installments_keep_total_first_get_extra_cent() -> None:
    plan = schedule(Decimal("1000.00"), 3, date(2026, 1, 31))
    assert [p.amount for p in plan] == [Decimal("333.34"), Decimal("333.33"), Decimal("333.33")]
    assert [p.due_date for p in plan] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert sum(p.amount for p in schedule(Decimal("0.05"), 36, date(2026, 1, 1))) == Decimal("0.05")
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_plan_validation_and_status() -> None:
    with pytest.raises(FormFieldError) as exc:
        check_plan(Decimal("1500.00"), 37, date(2026, 6, 1), Decimal("1000.00"), date(2026, 6, 20))
    assert set(exc.value.fields) == {"amount", "installment_count", "first_due_date"}
    check_plan(Decimal("1000.00"), 2, date(2026, 6, 20), Decimal("1000.00"), date(2026, 6, 20))
    due, today = date(2026, 7, 10), date(2026, 7, 20)
    assert installment_status(Decimal(100), Decimal(100), due, 10, today) is InstallmentStatus.PAID
    assert (
        installment_status(Decimal(100), Decimal(40), due, 10, today) is InstallmentStatus.PARTIAL
    )
    assert installment_status(Decimal(100), Decimal(0), due, 9, today) is InstallmentStatus.OVERDUE
    assert plan_status([InstallmentStatus.PAID] * 2) is PlanStatus.COMPLETED
    assert plan_status([InstallmentStatus.PAID, InstallmentStatus.OVERDUE]) is PlanStatus.BROKEN
    assert plan_status([InstallmentStatus.PAID, InstallmentStatus.OPEN]) is PlanStatus.ACTIVE


def test_aging_buckets_and_reminder_stages() -> None:
    assert [bucket_of(d) for d in (0, 30, 31, 60, 61, 90, 91)] == [
        AgingBucket.D0_30,
        AgingBucket.D0_30,
        AgingBucket.D31_60,
        AgingBucket.D31_60,
        AgingBucket.D61_90,
        AgingBucket.D61_90,
        AgingBucket.D90_PLUS,
    ]
    assert due_stages(2, [3, 10, 30], 60) == []
    assert due_stages(10, [3, 10, 30], 60) == [ReminderStage.APP, ReminderStage.SMS]
    assert due_stages(60, [3, 10, 30], 60)[-1] is ReminderStage.DEMAND_LETTER


def test_action_and_letter_validation() -> None:
    with pytest.raises(FormFieldError) as exc:
        check_action("reminder", "fax", None)
    assert set(exc.value.fields) == {"channel"}
    with pytest.raises(FormFieldError):
        check_action("call", None, "a")
    assert check_action("note", None, "  Aradı, ay sonu ödeyecek ")[2] == "Aradı, ay sonu ödeyecek"
    with pytest.raises(FormFieldError) as letter:
        check_letter("registered_mail", date(2026, 7, 1), None, date(2026, 6, 20))
    assert set(letter.value.fields) == {"sent_on", "tracking_no"}
    assert check_letter("hand", date(2026, 6, 1), None, date(2026, 6, 20))[2] is None


# --- Muhasebe fiş dosyası ---------------------------------------------------------------


def test_csv_and_xlsx_vouchers_balance() -> None:
    vouchers = [
        Voucher(
            "charge:1",
            "charge",
            "1",
            date(2026, 9, 1),
            "09/2026 aidat",
            "120",
            "600",
            Decimal("1234.56"),
        ),
        Voucher(
            "expense:2",
            "expense",
            "2",
            date(2026, 9, 3),
            "=HYPERLINK()",
            "770",
            "102",
            Decimal("10.00"),
        ),
    ]
    data = GenericCsvAdapter().render(vouchers)
    assert data.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(data.decode("utf-8").lstrip("﻿")), delimiter=";"))
    assert rows[1] == [
        "01.09.2026",
        "Aidat tahakkuku",
        "1",
        "09/2026 aidat",
        "120",
        "600",
        "1234,56",
    ]
    assert rows[2][3] == "'=HYPERLINK()"  # formül enjeksiyonu engellenir
    assert GenericXlsxAdapter().render(vouchers)[:2] == b"PK"
    assert check_code("120.01")
    assert check_code("")
    assert not check_code("A120")
