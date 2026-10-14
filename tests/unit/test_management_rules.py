"""Yönetim paketi saf kuralları — frontend servis istekleri 14–18 (domain/management.py)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.management import (
    ContractState,
    PollStatus,
    asset_code,
    contract_state,
    format_quantity,
    format_quantity_tr,
    parse_quantity,
    poll_status,
    staff_active,
)

TODAY = date(2026, 6, 20)


@pytest.mark.parametrize(
    ("ends_on", "closed_early", "expected"),
    [
        (date(2026, 6, 20), False, PollStatus.OPEN),  # bitiş günü dahil açık
        (date(2026, 6, 19), False, PollStatus.CLOSED),
        (date(2026, 7, 1), True, PollStatus.CLOSED),  # erken kapatıldı
        (date(2026, 7, 1), False, PollStatus.OPEN),
    ],
)
def test_anket_durumu(ends_on: date, closed_early: bool, expected: PollStatus) -> None:
    assert poll_status(ends_on, closed_early, TODAY) is expected


@pytest.mark.parametrize(
    ("end_date", "notice_days", "archived", "expected"),
    [
        (date(2026, 12, 31), 30, False, (194, ContractState.ACTIVE)),
        (date(2026, 7, 20), 30, False, (30, ContractState.EXPIRING)),  # sınır: = ihbar
        (date(2026, 7, 21), 30, False, (31, ContractState.ACTIVE)),
        (date(2026, 6, 20), 0, False, (0, ContractState.EXPIRING)),  # bugün bitiyor
        (date(2026, 6, 19), 30, False, (-1, ContractState.EXPIRED)),
        (date(2026, 6, 19), 30, True, (-1, ContractState.ARCHIVED)),  # arşiv önce gelir
    ],
)
def test_sozlesme_durumu(
    end_date: date, notice_days: int, archived: bool, expected: tuple[int, ContractState]
) -> None:
    assert (
        contract_state(end_date=end_date, notice_days=notice_days, archived=archived, today=TODAY)
        == expected
    )


def test_demirbas_kodu() -> None:
    assert [asset_code(n) for n in (1, 42, 9999, 10000)] == [
        "DB-0001",
        "DB-0042",
        "DB-9999",
        "DB-10000",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12.5", Decimal("12.500")),
        ("12,5", Decimal("12.500")),
        (" 0.001 ", Decimal("0.001")),
        ("4", Decimal("4.000")),
        (Decimal("1.10"), Decimal("1.100")),
        (3, Decimal("3.000")),
    ],
)
def test_miktar_okuma(text: str | int | Decimal, expected: Decimal) -> None:
    assert parse_quantity(text) == expected


@pytest.mark.parametrize("text", ["0", "0.000", "-1", "1.2345", "abc", "", None, "NaN", "Infinity"])
def test_gecersiz_miktar(text: str | None) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - mesajı çağıran yazar
        parse_quantity(text)


def test_sifir_yalniz_izinle_ve_ust_sinir() -> None:
    assert parse_quantity("0", allow_zero=True) == Decimal("0.000")
    with pytest.raises(ValueError):  # noqa: PT011
        parse_quantity("100000000")


def test_ondalik_toplam_kayan_noktasiz() -> None:
    assert parse_quantity("0.1") + parse_quantity("0.2") == parse_quantity("0.3")


@pytest.mark.parametrize(
    ("value", "tr", "api"),
    [
        (Decimal("4.500"), "4,5", "4.5"),
        (Decimal("10.000"), "10", "10"),
        (Decimal("0.000"), "0", "0"),
        (Decimal("1234.125"), "1234,125", "1234.125"),
        (Decimal("100"), "100", "100"),
    ],
)
def test_miktar_yazimi(value: Decimal, tr: str, api: str) -> None:
    assert (format_quantity_tr(value), format_quantity(value)) == (tr, api)


@pytest.mark.parametrize(
    ("end_date", "expected"),
    [
        (None, True),
        (date(2026, 6, 20), True),
        (date(2026, 6, 21), True),
        (date(2026, 6, 19), False),
    ],
)
def test_personel_calisiyor_mu(end_date: date | None, expected: bool) -> None:
    assert staff_active(end_date, TODAY) is expected
