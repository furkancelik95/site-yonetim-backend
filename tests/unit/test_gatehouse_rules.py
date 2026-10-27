"""Güvenlik, devriye ve otopark saf kuralları (servis istekleri 40–46, K30)."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from site_yonetim.domain.gatehouse import (
    InviteStatus,
    InviteType,
    InviteWindow,
    UnannouncedMethod,
    VisitorFields,
    VisitRequestStatus,
    check_check_in,
    check_guard_decision,
    check_invite,
    check_resident_decision,
    check_settings,
    display_plate,
    months_before,
    normalize_plate,
    seconds_left,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.parking import check_allocation, check_end, check_space, overlaps
from site_yonetim.domain.patrol import (
    PatrolTag,
    Scan,
    SlotStatus,
    build_report,
    check_plan,
    check_point,
    current_slot,
    is_out_of_order,
    slot_status,
)

NOW = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)


def _settings(**over: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unannounced_method": "approval",
        "approval_timeout_seconds": 90,
        "gate_methods": {"g1": "info"},
        "parcel_method": "both",
        "visitor_fields": VisitorFields(),
        "retention_months": 6,
        "auto_anonymize": False,
        "parcel_long_wait_days": 5,
        "guest_parking_capacity": 10,
        "gate_ids": {"g1"},
    }
    return base | over


def test_ayarlar_kapi_istisnasi_ve_saklama_siniri() -> None:
    data = check_settings(**_settings())
    assert data.method_for("g1") is UnannouncedMethod.INFO
    assert data.method_for("g2") is UnannouncedMethod.APPROVAL
    assert data.method_for(None) is UnannouncedMethod.APPROVAL
    with pytest.raises(OperationRuleError) as limit:
        check_settings(**_settings(retention_months=25))
    assert (limit.value.code, limit.value.field) == ("retention_limit", "retention_months")
    with pytest.raises(FormFieldError) as bad:
        check_settings(
            **_settings(approval_timeout_seconds=601, parcel_method="x", retention_months=True)
        )
    assert set(bad.value.fields) == {
        "approval_timeout_seconds",
        "parcel_method",
        "retention_months",
    }


@pytest.mark.parametrize(
    ("raw", "plate", "shown"),
    [
        ("34 abc 123", "34ABC123", "34 ABC 123"),
        ("06-ab-1234", "06AB1234", "06 AB 1234"),
        ("34 ıi 12", "34II12", "34 II 12"),
        ("DE XY 99", "DEXY99", "DEXY99"),  # yabancı plaka: olduğu gibi
    ],
)
def test_plaka_normalize_ve_gorunum(raw: str, plate: str, shown: str) -> None:
    assert normalize_plate(raw) == plate
    assert display_plate(plate) == shown


def test_saklama_siniri_ay_sonu() -> None:
    assert months_before(date(2026, 8, 31), 6) == date(2026, 2, 28)
    assert months_before(date(2026, 1, 15), 1) == date(2025, 12, 15)
    assert months_before(date(2026, 6, 20), 24) == date(2024, 6, 20)


def test_sureli_kalici_davet_gun_ve_saat() -> None:
    window = InviteWindow(
        InviteType.RECURRING,
        date(2026, 6, 1),
        date(2026, 6, 30),
        (0, 1, 2, 3, 4),
        time(8),
        time(18),
    )
    monday, saturday = date(2026, 6, 22), date(2026, 6, 20)
    assert window.valid_at(monday, time(9))
    assert not window.valid_at(monday, time(18, 1))
    assert not window.valid_on(saturday)
    assert not window.valid_on(date(2026, 7, 1))
    check_check_in(InviteStatus.ACTIVE, window, monday, time(9))
    with pytest.raises(OperationRuleError, match="geçerli değil"):
        check_check_in(InviteStatus.ACTIVE, window, saturday, time(9))
    with pytest.raises(OperationRuleError, match="kullanılmış"):
        check_check_in(InviteStatus.USED, window, monday, time(9))


def test_davet_formu() -> None:
    form = check_invite(
        visitor_name="  Test   Deneme ",
        type_="once",
        date_from=date(2026, 6, 20),
        date_to=date(2026, 6, 25),
        weekdays=[1],
        time_from=None,
        time_to=None,
        plate="34 msf 77",
        note=None,
        today=date(2026, 6, 20),
    )
    assert (form.visitor_name, form.window.date_to, form.window.weekdays, form.plate) == (
        "Test Deneme",
        date(2026, 6, 20),  # tek kullanımlıkta bitiş = başlangıç
        (),
        "34MSF77",
    )
    with pytest.raises(FormFieldError) as bad:
        check_invite(
            visitor_name="A1",
            type_="range",
            date_from=date(2026, 6, 20),
            date_to=date(2027, 7, 1),
            weekdays=[],
            time_from=time(9),
            time_to=None,
            plate=None,
            note="x" * 201,
            today=date(2026, 6, 20),
        )
    assert set(bad.value.fields) == {"visitor_name", "date_to", "time_to", "note"}


def test_onay_karari_suresi() -> None:
    expires = NOW + timedelta(seconds=90)
    assert seconds_left(expires, NOW + timedelta(seconds=0.5)) == 90
    assert seconds_left(expires, NOW + timedelta(seconds=120)) == 0
    with pytest.raises(OperationRuleError) as early:
        check_guard_decision(VisitRequestStatus.AWAITING, expires, NOW, "allow", "Diafonla")
    assert early.value.code == "awaiting_resident"
    with pytest.raises(FormFieldError):
        check_guard_decision(VisitRequestStatus.AWAITING, expires, expires, "allow", "kısa")
    assert (
        check_guard_decision(
            VisitRequestStatus.AWAITING, expires, expires, "deny", " Ulaşılamadı "
        )[1]
        == "Ulaşılamadı"
    )
    with pytest.raises(OperationRuleError) as late:
        check_resident_decision(VisitRequestStatus.AWAITING, expires, expires, "allow")
    assert late.value.code == "timed_out"
    with pytest.raises(OperationRuleError) as decided:
        check_resident_decision(VisitRequestStatus.ALLOWED, expires, NOW, "allow")
    assert decided.value.code == "already_decided"


def test_devriye_plani_ve_noktasi() -> None:
    point = check_point(name=" Havuz ", location=None, area="A", tag="both", taken=set())
    assert (point.name, point.tag) == ("Havuz", PatrolTag.BOTH)
    with pytest.raises(FormFieldError) as dup:
        check_point(name="HAVUZ", location=None, area=None, tag="qr", taken={"havuz"})
    assert dup.value.fields == {"name": "Bu adla nokta var."}
    plan = check_plan(
        name="Gece",
        point_ids=["a", "b"],
        ordered=True,
        start_times=["22:00", "01:00", "22:00"],
        allowed_minutes=30,
        assignee=None,
        active_points={"a", "b"},
    )
    assert plan.start_times == ("01:00", "22:00")
    with pytest.raises(FormFieldError) as bad:
        check_plan(
            name="Gece",
            point_ids=["a", "a"],
            ordered=True,
            start_times=["7:00"],
            allowed_minutes=True,
            assignee=None,
            active_points={"a"},
        )
    assert set(bad.value.fields) == {"point_ids", "start_times", "allowed_minutes"}
    assert current_slot(("01:00", "10:00", "22:00"), "10:30") == "10:00"
    assert current_slot(("10:00",), "09:00") is None


def test_sira_disi_ve_sapma_raporu() -> None:
    points = ("p1", "p2", "p3")
    assert not is_out_of_order(True, points, set(), "p1")
    assert is_out_of_order(True, points, set(), "p2")
    assert not is_out_of_order(False, points, set(), "p3")
    report = build_report(
        point_ids=points,
        names={"p1": "Bir", "p2": "İki", "p3": "Üç"},
        scans=[Scan("p2", True), Scan("p1", False)],
        started_at=NOW,
        finished_at=NOW + timedelta(minutes=31),
        now=NOW,
        allowed_minutes=30,
        slot_start=NOW - timedelta(minutes=5),
    )
    assert (report.missed, report.out_of_order, report.overtime, report.late_start_minutes) == (
        ["Üç"],
        ["İki"],
        True,
        5,
    )
    assert (report.scanned_points, report.total_points, report.ok) == (2, 3, False)


@pytest.mark.parametrize(
    ("minutes", "started", "finished", "ok", "expected"),
    [
        (-1, None, False, False, SlotStatus.UPCOMING),
        (10, None, False, False, SlotStatus.DUE),
        (16, None, False, False, SlotStatus.NOT_STARTED),
        (20, 5, False, False, SlotStatus.RUNNING),
        (40, 5, False, False, SlotStatus.OVERDUE),
        (40, 5, True, True, SlotStatus.DONE),
        (40, 5, True, False, SlotStatus.DONE_WITH_DEVIATION),
    ],
)
def test_takvim_durumu(
    minutes: int, started: int | None, finished: bool, ok: bool, expected: SlotStatus
) -> None:
    start = NOW
    assert (
        slot_status(
            slot_start=start,
            now=start + timedelta(minutes=minutes),
            started_at=start + timedelta(minutes=started) if started is not None else None,
            finished=finished,
            report_ok=ok,
            allowed_minutes=30,
        )
        is expected
    )


def test_park_yeri_ve_tahsis_kurallari() -> None:
    form = check_space(
        code=" b-14 ", kind="disabled", location=None, is_paid=False, note=None, taken=set()
    )
    assert form.code == "B-14"
    with pytest.raises(FormFieldError):
        check_space(
            code="B-14", kind="standard", location=None, is_paid=False, note=None, taken={"B-14"}
        )
    d = date
    assert overlaps(d(2026, 6, 1), None, d(2026, 7, 1), None)
    assert not overlaps(d(2026, 6, 1), d(2026, 6, 30), d(2026, 7, 1), None)
    assert overlaps(d(2026, 6, 1), d(2026, 6, 30), d(2026, 6, 30), d(2026, 7, 5))
    assert not overlaps(d(2026, 7, 1), None, d(2026, 6, 1), d(2026, 6, 30))
    with pytest.raises(FormFieldError):
        check_allocation(d(2026, 6, 2), d(2026, 6, 1))
    with pytest.raises(OperationRuleError):
        check_end(d(2026, 6, 1), d(2026, 6, 30), d(2026, 7, 1))
    assert check_end(d(2026, 6, 1), None, d(2026, 6, 1)) == d(2026, 6, 1)
