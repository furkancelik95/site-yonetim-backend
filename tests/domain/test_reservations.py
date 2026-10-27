"""Rezervasyon saf kuralları — REZ-01..03, K11."""

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from site_yonetim.domain.reservations import (
    DEFAULT_RULES,
    BookingInput,
    DebtRule,
    FeeType,
    NoShowRule,
    ReservationStatus,
    VerifyMethod,
    add_months,
    blocked_until,
    check_booking,
    check_code,
    check_debt_rule,
    check_rules,
    close_status,
    debt_blocked,
    fee_of,
    is_late,
    no_show_rate,
    occupancy,
    slots,
    week_start,
)
from site_yonetim.domain.service_desk import RuleError

BUSINESS_TZ = ZoneInfo("Europe/Istanbul")


def raw(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "enabled": True, "open_from": "09:00", "open_to": "22:00", "slot_minutes": 60,
        "capacity": 1, "max_people": 10, "max_guests": 5, "weekly_limit": 2, "cancel_hours": 24,
        "late_cancel_refund": False, "fee_type": "free", "fee_amount": None, "channel": "both",
        "approval": "auto", "verify": "code", "waitlist": True, "waitlist_minutes": 30,
        "noshow": {"enabled": False, "months": 3, "count": 2, "block_days": 14},
    }  # fmt: skip
    body.update(over)
    return body


def test_varsayilan_kurallar_formdan_aynen_doner() -> None:
    assert check_rules(raw()) == DEFAULT_RULES


def test_noshow_dogrulama_olmadan_kaydedilmez() -> None:
    with pytest.raises(RuleError) as caught:
        check_rules(raw(verify="none", noshow={"enabled": True, "months": 3, "count": 2,
                                               "block_days": 14}))  # fmt: skip
    assert caught.value.fields == {"noshow": "No-show kuralı için kullanım doğrulaması gerekir."}


def test_sure_acik_saatlere_sigmali_ve_ucret_zorunlu() -> None:
    with pytest.raises(RuleError) as caught:
        check_rules(raw(open_to="10:00", slot_minutes=90, fee_type="fixed", fee_amount="0"))
    assert set(caught.value.fields) == {"slot_minutes", "fee_amount"}
    with pytest.raises(RuleError) as bad_choice:
        check_rules(raw(channel="x", slot_minutes=45))
    assert set(bad_choice.value.fields) == {"channel", "slot_minutes"}


def test_saat_izgarasi_ve_ucret_tipleri() -> None:
    rules = check_rules(raw(open_from="09:30", open_to="12:00", slot_minutes=60))
    assert slots(rules) == [(time(9, 30), time(10, 30)), (time(10, 30), time(11, 30))]
    hourly = check_rules(raw(fee_type="hourly", fee_amount="100.00", slot_minutes=90))
    assert fee_of(hourly, 0) == Decimal("150.00")
    guest = check_rules(raw(fee_type="per_guest", fee_amount="40.00"))
    assert fee_of(guest, 3) == Decimal("120.00")
    assert fee_of(DEFAULT_RULES, 3) == Decimal("0")
    assert check_rules(raw(fee_type="fixed", fee_amount="250.00")).fee_type is FeeType.FIXED


def test_rezervasyon_girdisi() -> None:
    now = datetime(2026, 10, 1, 10, 30, tzinfo=BUSINESS_TZ)
    ok = check_booking(DEFAULT_RULES, BookingInput(date(2026, 10, 1), time(11, 0), 2, 1),
                       now_local=now)  # fmt: skip
    assert ok == (date(2026, 10, 1), time(11, 0), time(12, 0), 2, 1)
    with pytest.raises(RuleError) as caught:
        check_booking(DEFAULT_RULES, BookingInput(date(2026, 10, 1), time(10, 0), 0, 9),
                      now_local=now)  # fmt: skip
    assert caught.value.fields == {
        "start": "Geçmiş saat seçilemez.", "people": "Kişi sayısı 1–10.",
        "guests": "Misafir en fazla 5.",
    }  # fmt: skip


def test_hafta_pazartesi_baslar_ve_ay_kirpilir() -> None:
    assert week_start(date(2026, 10, 4)) == date(2026, 9, 28)  # pazar → pazartesi
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 15), -3) == date(2025, 10, 15)


def test_noshow_kisiti() -> None:
    rules = DEFAULT_RULES.__class__(verify=VerifyMethod.CODE, noshow=NoShowRule(True, 3, 2, 14))
    today = date(2026, 10, 10)
    assert blocked_until(rules, [date(2026, 10, 1)], today) is None  # tek no-show yetmez
    assert blocked_until(rules, [date(2026, 6, 1), date(2026, 10, 1)], today) is None  # pencere
    assert blocked_until(rules, [date(2026, 9, 1), date(2026, 10, 1)], today) == date(2026, 10, 15)
    assert blocked_until(rules, [date(2026, 9, 1), date(2026, 9, 2)], today) is None  # süresi bitti
    assert close_status(VerifyMethod.NONE) is ReservationStatus.COMPLETED
    assert close_status(VerifyMethod.QR) is ReservationStatus.NO_SHOW


def test_gec_iptal_doluluk_ve_oran() -> None:
    starts = datetime(2026, 10, 2, 10, 0, tzinfo=BUSINESS_TZ)
    assert is_late(starts, DEFAULT_RULES, datetime(2026, 10, 1, 11, 0, tzinfo=BUSINESS_TZ))
    assert not is_late(starts, DEFAULT_RULES, datetime(2026, 10, 1, 9, 0, tzinfo=BUSINESS_TZ))
    assert occupancy(3, 13, 1, 30) == Decimal("0.8")
    assert occupancy(0, 0, 1, 30) == Decimal("0.0")
    assert no_show_rate(VerifyMethod.CODE, 1, 8) == 13  # 12,5 → 13
    assert no_show_rate(VerifyMethod.NONE, 1, 2) is None
    assert no_show_rate(VerifyMethod.CODE, 1, 3) == 33


def test_kod_bicimi() -> None:
    assert check_code("SY-REZ:123456") == "123456"
    assert check_code(" 12 34 56 ") == "123456"
    with pytest.raises(RuleError):
        check_code("12345")


def test_k11_borc_kisiti() -> None:
    assert check_debt_rule(False, None, None) == DebtRule(False, None, None)
    with pytest.raises(RuleError) as caught:
        check_debt_rule(True, Decimal(0), 0)
    assert set(caught.value.fields) == {"debt_min_amount", "debt_min_days"}
    rule = check_debt_rule(True, Decimal("1000"), 30)
    today = date(2026, 10, 1)
    assert debt_blocked(rule, Decimal("1500.00"), date(2026, 8, 1), today)
    assert not debt_blocked(rule, Decimal("999.99"), date(2026, 8, 1), today)  # tutar altı
    assert not debt_blocked(rule, Decimal("1500.00"), date(2026, 9, 15), today)  # gün altı
    assert not debt_blocked(DebtRule(), Decimal("99999"), date(2020, 1, 1), today)  # kapalı
