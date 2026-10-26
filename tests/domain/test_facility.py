"""Operasyon ekleri ve teknik varlık — saf kurallar (OPS-01..06, HIZ-06/07)."""

from datetime import date, time
from decimal import Decimal

import pytest

from site_yonetim.domain.facility import (
    DEFAULT_CHECKLISTS,
    ChecklistItem,
    ChecklistItemType,
    CompletionStatus,
    DepositStatus,
    DueWarning,
    PeriodUnit,
    PolicyWarning,
    TaskFrequency,
    TaskStatus,
    TechnicalAssetCategory,
    add_months,
    answer_ok,
    consumption,
    deposit_status,
    deviation_percent,
    due_warning,
    inspection_score,
    is_anomaly,
    next_maintenance,
    policy_warning,
    request_category_for,
    same_name,
    suggested_inspections,
    task_applies,
    task_rate,
    task_status,
    technical_asset_code,
    valid_period,
)


def test_emanet_suresi_gecince_expired() -> None:
    assert deposit_status("held", date(2026, 6, 19), date(2026, 6, 20)) is DepositStatus.EXPIRED
    assert (
        deposit_status("requested", date(2026, 6, 19), date(2026, 6, 20)) is DepositStatus.EXPIRED
    )
    assert deposit_status("held", date(2026, 6, 20), date(2026, 6, 20)) is DepositStatus.HELD
    assert deposit_status("handed", date(2026, 1, 1), date(2026, 6, 20)) is DepositStatus.HANDED


def test_ad_eslesmesi_turkce_harf_duyarsiz() -> None:
    assert same_name("İSMAİL  Işık", "ismail ışık")
    assert not same_name("Ali", "Veli")


def test_denetim_uygunlugu_ve_skor() -> None:
    yesno = ChecklistItem("Temiz", ChecklistItemType.YESNO)
    chlorine = ChecklistItem("Klor", ChecklistItemType.NUMBER, False, Decimal(1), Decimal(3))
    score = ChecklistItem("Puan", ChecklistItemType.SCORE)
    assert answer_ok(yesno, "yes") is True
    assert answer_ok(yesno, "no") is False
    assert answer_ok(yesno, "belki") is None
    assert answer_ok(chlorine, "2,5") is True
    assert answer_ok(chlorine, "3.1") is False
    assert answer_ok(chlorine, "abc") is None
    assert answer_ok(score, "3") is True
    assert answer_ok(score, "2") is False
    assert answer_ok(score, "6") is None
    assert answer_ok(score, "2.5") is None
    assert inspection_score([True, True, False]) == 67
    assert inspection_score([]) == 0
    assert [name for name, _ in DEFAULT_CHECKLISTS][:2] == ["Temizlik kalitesi", "Havuz hijyeni"]


@pytest.mark.parametrize(
    ("left", "warning"),
    [(61, None), (60, PolicyWarning.DAYS_60), (31, PolicyWarning.DAYS_60),
     (30, PolicyWarning.DAYS_30), (0, PolicyWarning.DAYS_30), (-1, PolicyWarning.EXPIRED)],
)  # fmt: skip
def test_police_uyarisi(left: int, warning: PolicyWarning | None) -> None:
    assert policy_warning(left) is warning


def test_sayac_tuketim_ve_sapma() -> None:
    assert consumption(Decimal("1250"), Decimal("1200.5"), Decimal("1.5")) == Decimal("74.250")
    assert deviation_percent(Decimal(150), Decimal(100)) == 50
    assert deviation_percent(Decimal(151), Decimal(100)) == 51
    assert deviation_percent(Decimal(10), None) is None
    assert deviation_percent(Decimal(10), Decimal(0)) is None
    assert not is_anomaly(50, 50)
    assert is_anomaly(-51, 50)
    assert not is_anomaly(None, 50)
    assert valid_period("2026-06")
    assert not valid_period("2026-13")
    assert not valid_period(None)


def test_gorev_gunu_ve_durumu() -> None:
    monday, sunday = date(2026, 6, 15), date(2026, 6, 21)
    assert task_applies(TaskFrequency.DAILY, [], None, sunday)
    assert task_applies(TaskFrequency.WEEKDAYS, [0, 2], None, monday)
    assert not task_applies(TaskFrequency.WEEKDAYS, [0, 2], None, sunday)
    assert task_applies(TaskFrequency.MONTHLY, [], 15, monday)

    def status(day: date, now: time) -> TaskStatus:
        return task_status(
            None, day=day, today=monday, now=now, time_from=time(8), time_to=time(10)
        )

    assert status(monday, time(7, 59)) is TaskStatus.UPCOMING
    assert status(monday, time(8)) is TaskStatus.DUE
    assert status(monday, time(10, 1)) is TaskStatus.LATE
    assert status(date(2026, 6, 14), time(7)) is TaskStatus.LATE
    assert status(date(2026, 6, 16), time(12)) is TaskStatus.UPCOMING
    done = task_status(
        CompletionStatus.DONE,
        day=monday,
        today=monday,
        now=time(11),
        time_from=time(8),
        time_to=time(10),
    )
    assert done is TaskStatus.DONE
    assert task_rate(0, 0) is None
    assert task_rate(3, 2) == 67


def test_muayene_takvimi_kabul_s1() -> None:
    """HIZ-07 S1: 1 yıl periyot, son muayene 15.11.2025 → sonraki 15.11.2026; ilk uyarı
    16.09.2026 (60 gün kala)."""
    next_due = add_months(date(2025, 11, 15), 12)
    assert next_due == date(2026, 11, 15)
    assert due_warning((next_due - date(2026, 9, 15)).days) is None
    assert due_warning((next_due - date(2026, 9, 16)).days) is DueWarning.DAYS_60
    assert due_warning(30) is DueWarning.DAYS_30
    assert due_warning(7) is DueWarning.DAYS_7
    assert due_warning(-1) is DueWarning.OVERDUE
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_bakim_sonraki_tarih_ve_kod() -> None:
    assert next_maintenance(date(2026, 6, 1), date(2026, 6, 3), PeriodUnit.MONTH, 1) == date(
        2026, 7, 1
    )
    # gecikmeli bakım bitişten sonraya kayar
    assert next_maintenance(date(2026, 6, 1), date(2026, 7, 2), PeriodUnit.MONTH, 1) == date(
        2026, 8, 1
    )
    assert next_maintenance(date(2026, 6, 1), date(2026, 6, 1), PeriodUnit.WEEK, 2) == date(
        2026, 6, 15
    )
    assert technical_asset_code(7) == "TV-0007"
    assert [t.key for t in suggested_inspections(TechnicalAssetCategory.ELEVATOR)] == [
        "elevator_periodic"
    ]
    assert request_category_for(TechnicalAssetCategory.ELEVATOR) == "elevator"
    assert request_category_for(TechnicalAssetCategory.OTHER) == "other"
