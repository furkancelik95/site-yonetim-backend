"""Hizmetler — SLA ve çalışma takvimi, kapanış onayı, ücretli hizmet, tadilat (saf; HIZ-01..05)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from site_yonetim.domain.operations import RequestPriority
from site_yonetim.domain.service_desk import (
    DEFAULT_CALENDAR,
    DEFAULT_CATEGORIES,
    HourType,
    PermitType,
    RuleError,
    SlaRule,
    SlaState,
    add_business_minutes,
    billed_amount,
    business_minutes_between,
    calendar_json,
    check_feedback,
    check_hours,
    check_hours_range,
    check_permit,
    due_at,
    parse_calendar,
    quote_lines,
    quote_total,
    sla_status,
)

TZ = ZoneInfo("Europe/Istanbul")
FRIDAY_17 = datetime(2026, 10, 23, 17, 0, tzinfo=TZ)  # cuma
WEEKDAYS_9_18 = parse_calendar(
    [{"closed": False, "open": "09:00", "close": "18:00"}] * 5
    + [{"closed": True, "open": "09:00", "close": "18:00"}] * 2
)


def test_hiz02_s1_friday_17_plus_4_hours_is_monday_12() -> None:
    """HIZ-02 S1: çalışma saati tipi, 4 saat SLA, cuma 17:00, mesai 09–18 → pazartesi 12:00."""
    rule = SlaRule(HourType.BUSINESS, 4, WEEKDAYS_9_18)
    assert due_at(rule, FRIDAY_17, TZ) == datetime(2026, 10, 26, 12, 0, tzinfo=TZ)


def test_24x7_counts_every_hour() -> None:
    rule = SlaRule(HourType.ALWAYS, 4, None)
    assert due_at(rule, FRIDAY_17, TZ) == FRIDAY_17 + timedelta(hours=4)
    assert due_at(rule, FRIDAY_17, TZ, ratio=Decimal("0.8")) == FRIDAY_17 + timedelta(minutes=192)


def test_counter_stops_outside_business_hours() -> None:
    saturday_noon = datetime(2026, 10, 24, 12, 0, tzinfo=TZ)
    assert business_minutes_between(FRIDAY_17, saturday_noon, WEEKDAYS_9_18, TZ) == 60
    monday_10 = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)
    assert business_minutes_between(FRIDAY_17, monday_10, WEEKDAYS_9_18, TZ) == 120
    before_open = datetime(2026, 10, 26, 7, 0, tzinfo=TZ)
    assert add_business_minutes(before_open, 30, WEEKDAYS_9_18, TZ) == datetime(
        2026, 10, 26, 9, 30, tzinfo=TZ
    )


def test_sla_status_paused_and_states() -> None:
    rule = SlaRule(HourType.BUSINESS, 4, WEEKDAYS_9_18)
    due = due_at(rule, FRIDAY_17, TZ)
    weekend = sla_status(
        rule, created_at=FRIDAY_17, due=due, finished_at=None,
        now=datetime(2026, 10, 24, 12, 0, tzinfo=TZ), tz=TZ,
    )  # fmt: skip
    assert (weekend.used_minutes, weekend.remaining_minutes, weekend.paused) == (60, 180, True)
    assert weekend.state is SlaState.OK
    warning = sla_status(
        rule, created_at=FRIDAY_17, due=due, finished_at=None,
        now=datetime(2026, 10, 26, 11, 15, tzinfo=TZ), tz=TZ,
    )  # fmt: skip
    assert (warning.state, warning.paused) == (SlaState.WARNING, False)
    breached = sla_status(
        rule, created_at=FRIDAY_17, due=due, finished_at=None,
        now=datetime(2026, 10, 26, 13, 0, tzinfo=TZ), tz=TZ,
    )  # fmt: skip
    assert (breached.state, breached.remaining_minutes) == (SlaState.BREACHED, 0)
    met = sla_status(
        rule, created_at=FRIDAY_17, due=due, finished_at=due, now=due + timedelta(days=3), tz=TZ
    )
    assert met.state is SlaState.MET
    missed = sla_status(
        rule, created_at=FRIDAY_17, due=due, finished_at=due + timedelta(minutes=1),
        now=due + timedelta(days=3), tz=TZ,
    )  # fmt: skip
    assert missed.state is SlaState.MISSED


def test_calendar_validation_and_round_trip() -> None:
    assert parse_calendar(calendar_json(DEFAULT_CALENDAR)) == DEFAULT_CALENDAR
    assert DEFAULT_CALENDAR[5].open == time(10)
    assert DEFAULT_CALENDAR[6].closed
    with pytest.raises(RuleError) as caught:
        parse_calendar([{"closed": True}] * 7)
    assert caught.value.fields == {"calendar": "En az bir çalışma günü olmalı."}
    with pytest.raises(RuleError):
        parse_calendar([{"closed": False, "open": "18:00", "close": "09:00"}] * 7)
    with pytest.raises(RuleError):
        parse_calendar([{"closed": False, "open": "09:00", "close": "18:00"}] * 6)


def test_check_hours() -> None:
    good = {"low": 168, "normal": 72, "high": 24, "urgent": 4}
    assert check_hours(good, "x")[RequestPriority.URGENT] == 4
    with pytest.raises(RuleError) as caught:
        check_hours({**good, "urgent": 0, "high": 2161}, "categories.elevator")
    assert caught.value.fields == {
        "categories.elevator.high": "1–2160 saat.",
        "categories.elevator.urgent": "1–2160 saat.",
    }


def test_default_categories_keep_old_keys_and_emergencies_are_24x7() -> None:
    keys = {c.key: c for c in DEFAULT_CATEGORIES}
    old = {"other", "plumbing", "electrical", "elevator", "heating", "cleaning", "security",
           "garden", "common_area"}  # fmt: skip
    assert old <= set(keys)
    for key in ("flood", "elevator_trapped", "gas_smell", "elevator", "plumbing", "security"):
        assert keys[key].hour_type is HourType.ALWAYS


def test_feedback_rules() -> None:
    assert check_feedback(True, 4, "  iyi  ") == "iyi"
    with pytest.raises(RuleError) as caught:
        check_feedback(True, None, None)
    assert caught.value.fields == {"score": "1–5 arası puan verin."}
    with pytest.raises(RuleError):
        check_feedback(True, 6, None)
    with pytest.raises(RuleError) as caught:
        check_feedback(False, None, "yok")
    assert caught.value.fields == {"comment": "Sorunun neden devam ettiğini yazın."}


def test_quote_lines_and_billing() -> None:
    lines = quote_lines([("Batarya", Decimal(1), Decimal("650.00")), ("İşçilik", "1.5", "133.33"),
                         ("  ", 1, 1)])  # fmt: skip
    assert [line.total for line in lines] == [Decimal("650.00"), Decimal("200.00")]
    assert quote_total(lines) == Decimal("850.00")
    with pytest.raises(RuleError) as caught:
        quote_lines([])
    assert caught.value.fields == {"lines": "En az bir kalem girin."}
    with pytest.raises(RuleError):
        quote_lines([("x", 0, 10)])
    # Servis isteği 51 kabul: 850 teklif, 1.500 gerçekleşen, sakin ek tutarı reddeder → 850.
    offer, actual = Decimal(850), Decimal(1500)
    assert billed_amount(offer, actual, extra_accepted=False) == offer
    assert billed_amount(offer, actual, extra_accepted=True) == actual
    assert billed_amount(offer, Decimal(700), extra_accepted=None) == Decimal(700)


def test_permit_rules() -> None:
    today = date(2026, 10, 25)
    end, text = check_permit(PermitType.MOVE_IN, today, None, " Nakliye  10:00 ", today)
    assert (end, text) == (today, "Nakliye 10:00")
    with pytest.raises(RuleError) as caught:
        check_permit(PermitType.RENOVATION, today - timedelta(days=1), today - timedelta(days=2),
                     "kısa", today)  # fmt: skip
    assert set(caught.value.fields) == {"date_from", "date_to", "description"}
    assert check_hours_range("09:00-18:00", "w", optional=False) == "09:00-18:00"
    assert check_hours_range("", "s", optional=True) is None
    with pytest.raises(RuleError):
        check_hours_range("18:00-09:00", "w", optional=False)
