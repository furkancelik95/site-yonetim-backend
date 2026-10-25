"""İnsan kaynakları saf kuralları — servis istekleri 36–39 kabul kriterleri (IK-02..05)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.staffing import (
    AttendanceFlag,
    ConflictKind,
    LeaveDays,
    LeaveType,
    PlannedShift,
    TimesheetStatus,
    check_fix_minutes,
    check_payroll_no,
    days_between,
    detect,
    distance_m,
    export_csv,
    find_conflicts,
    format_time,
    hours,
    month_bounds,
    night_minutes,
    parse_time,
    planned_minutes,
    span,
    week_start,
    worked_minutes,
)

DAY = date(2026, 10, 12)


def _span(start: str, end: str):  # type: ignore[no-untyped-def]
    s, e = parse_time(start), parse_time(end)
    assert s is not None
    assert e is not None
    return span(s, e)


def test_kabul_08_16_mola_60_gec_20_6_saat_40_dakika() -> None:
    planned = planned_minutes(_span("08:00", "16:00"), 60)
    assert planned == 420
    assert worked_minutes(planned, 20, 0, 0) == 400  # 6 sa 40 dk
    assert worked_minutes(planned, 500, 0, 0) == 0  # alt sınır 0
    assert worked_minutes(planned, 0, 10, 30) == 440


def test_kabul_16_00_vardiyasinda_gece_2_saat() -> None:
    assert night_minutes(_span("16:00", "00:00")) == 120
    assert night_minutes(_span("22:00", "06:00")) == 480
    assert night_minutes(_span("00:00", "08:00")) == 360
    assert night_minutes(_span("08:00", "16:00")) == 0
    assert night_minutes(_span("20:00", "08:00")) == 480


def test_gece_vardiyasi_ertesi_gune_tasar() -> None:
    s = _span("22:00", "06:00")
    assert (s.start, s.end, s.length) == (1320, 1800, 480)
    assert _span("00:00", "00:00").length == 1440
    assert parse_time("24:00") is None
    assert parse_time("8:00") is None
    assert format_time(1800) == "06:00"


def test_kabul_gece_00_08_ardindan_16_24_dinlenme_8_saat() -> None:
    shifts = [
        PlannedShift("a", "ali", DAY, _span("00:00", "08:00")),
        PlannedShift("b", "ali", DAY, _span("16:00", "00:00")),
    ]
    (conflict,) = find_conflicts(shifts, [])
    assert (conflict.assignment_id, conflict.kind) == ("b", ConflictKind.REST)
    assert "8 saat" in conflict.message


def test_cakisan_vardiya_ve_izin_gunu() -> None:
    shifts = [
        PlannedShift("a", "ali", DAY, _span("08:00", "16:00")),
        PlannedShift("b", "ali", DAY, _span("12:00", "20:00")),
        PlannedShift("c", "veli", DAY, _span("08:00", "16:00")),
        PlannedShift("d", "ali", date(2026, 10, 13), _span("08:00", "16:00")),
    ]
    leaves = [LeaveDays("veli", DAY, DAY, LeaveType.SICK)]
    found = {(c.assignment_id, c.kind) for c in find_conflicts(shifts, leaves)}
    assert found == {("b", ConflictKind.OVERLAP), ("c", ConflictKind.LEAVE)}
    # 20:00'de biten vardiyadan sonra ertesi 08:00 → 12 saat: uyarı yok
    leave_msg = next(c.message for c in find_conflicts(shifts, leaves) if c.assignment_id == "c")
    assert "raporlu" in leave_msg


def test_11_saat_dinlenme_tam_sinirda_uyari_yok() -> None:
    shifts = [
        PlannedShift("a", "ali", DAY, _span("08:00", "16:00")),
        PlannedShift("b", "ali", date(2026, 10, 13), _span("03:00", "08:00")),
    ]
    assert find_conflicts(shifts, []) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", None), (None, None), (" ab-12 ", "AB-12"), ("A" * 20, "A" * 20)],
)
def test_sicil_no_gecerli(value: str | None, expected: str | None) -> None:
    assert check_payroll_no(value) == expected


@pytest.mark.parametrize("value", ["A" * 21, "12 34", "12/34", "çğ1"])
def test_sicil_no_gecersiz(value: str) -> None:
    with pytest.raises(ValueError, match="Sicil no harf, rakam ve tire"):
        check_payroll_no(value)


def test_dakika_araligi() -> None:
    assert check_fix_minutes(None) == 0
    assert check_fix_minutes(960) == 960
    with pytest.raises(ValueError, match="0–960 dakika"):
        check_fix_minutes(961)
    with pytest.raises(ValueError, match="0–960 dakika"):
        check_fix_minutes(-1)


def test_kabul_qr_20_dk_gec_tolerans_10_gec_gelme_istisnasi() -> None:
    s = _span("08:00", "16:00")
    found = detect(
        s, check_in=500, check_out=960, unverified=False, late_tolerance=10, early_tolerance=10
    )
    assert (found.status, found.late, found.early, found.overtime) == (
        TimesheetStatus.WORKED,
        20,
        0,
        0,
    )
    assert found.flags == (AttendanceFlag.LATE,)
    within = detect(
        s, check_in=485, check_out=955, unverified=False, late_tolerance=10, early_tolerance=10
    )
    assert (within.late, within.early, within.flags) == (0, 0, ())


def test_giris_yoksa_eksik_vardiya_cikis_yoksa_isaret() -> None:
    s = _span("16:00", "00:00")
    absent = detect(
        s, check_in=None, check_out=None, unverified=True, late_tolerance=10, early_tolerance=10
    )
    assert absent.status is TimesheetStatus.ABSENT
    assert absent.flags == (AttendanceFlag.UNVERIFIED, AttendanceFlag.NO_CHECK_IN)
    late_out = detect(
        s, check_in=960, check_out=1500, unverified=False, late_tolerance=10, early_tolerance=10
    )
    assert (late_out.overtime, late_out.flags) == (60, (AttendanceFlag.OVERTIME,))
    early = detect(
        s, check_in=960, check_out=1380, unverified=False, late_tolerance=10, early_tolerance=10
    )
    assert (early.early, early.flags) == (60, (AttendanceFlag.EARLY_LEAVE,))
    no_out = detect(
        s, check_in=960, check_out=None, unverified=False, late_tolerance=10, early_tolerance=10
    )
    assert no_out.flags == (AttendanceFlag.NO_CHECK_OUT,)


def test_tarih_yardimcilari() -> None:
    assert week_start(date(2026, 10, 18)) == date(2026, 10, 12)
    assert days_between(date(2026, 10, 12), date(2026, 10, 14)) == 3
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert month_bounds("2026-13") is None
    assert hours(400) == Decimal("6.67")
    assert hours(90) == Decimal("1.50")


def test_konum_uzakligi() -> None:
    lat, lon = Decimal("41.015137"), Decimal("28.979530")
    assert distance_m(lat, lon, lat, lon) == 0
    assert 1100 <= distance_m(lat, lon, Decimal("41.025137"), lon) <= 1115


def test_aktarim_dosyasi_bom_noktali_virgul_ve_formul_korumasi() -> None:
    rows: list[dict[str, object]] = [
        {
            "payroll_no": "P-1",
            "name": "=HYPERLINK(1)",
            "worked_days": 20,
            "worked_hours": "160.50",
            "overtime_hours": Decimal("2.00"),
            "night_hours": "0.00",
            "absent_days": 1,
            "leave_annual": 2,
            "leave_excuse": 0,
            "leave_sick": 0,
            "leave_unpaid": 0,
            "holiday_hours": "0.00",
        }
    ]
    data = export_csv("2026-10", rows)
    assert data.startswith(b"\xef\xbb\xbf")
    lines = data.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Dönem;Bordro sicil no;Ad soyad;Çalışılan gün")
    assert lines[1] == "2026-10;P-1;'=HYPERLINK(1);20;160,50;2,00;0,00;1;2;0;0;0;0,00"
