"""Site yapısı saf kuralları — docs/03 §4–§5."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.structure import (
    AccountKind,
    PartyRole,
    StructureRuleError,
    accounts_to_open,
    check_owner_share,
    check_party_end,
    is_active_on,
    reference_base,
    reference_code,
)
from site_yonetim.domain.validation import normalize_name_part, normalize_tr_mobile


@pytest.mark.parametrize(
    ("block", "number", "expected"),
    [
        ("A", "12", "A12"),
        ("Kule 1", "3", "KULE13"),
        ("Işık", "5", "ISIK5"),
        ("", "Z03", "Z03"),
        ("", "--", "BB"),
    ],
)
def test_referans_tabani(block: str, number: str, expected: str) -> None:
    assert reference_base(block, number) == expected


def test_referans_kodu_cakisinca_sayi_alir() -> None:
    assert reference_code("A12", "M", []) == "A12-M"
    assert reference_code("A12", "M", ["A12-M"]) == "A12-M2"
    assert reference_code("A12", "M", ["A12-M", "A12-M2"]) == "A12-M3"


def test_acilacak_hesaplar() -> None:
    assert accounts_to_open(PartyRole.OWNER, unit_has_active_tenant=False) == [
        (AccountKind.OWNER, "M"),
        (AccountKind.OCCUPANT, "O"),
    ]
    assert accounts_to_open(PartyRole.OWNER, unit_has_active_tenant=True) == [
        (AccountKind.OWNER, "M")
    ]
    assert accounts_to_open(PartyRole.TENANT, unit_has_active_tenant=False) == [
        (AccountKind.OCCUPANT, "K")
    ]
    assert accounts_to_open(PartyRole.RESIDENT, unit_has_active_tenant=False) == []
    assert accounts_to_open(PartyRole.PROXY, unit_has_active_tenant=True) == []


def test_tarih_araligi_etkinligi() -> None:
    assert is_active_on(date(2026, 1, 1), None, date(2026, 5, 1))
    assert is_active_on(date(2026, 1, 1), date(2026, 5, 1), date(2026, 5, 1))
    assert not is_active_on(date(2026, 1, 1), date(2026, 4, 30), date(2026, 5, 1))
    assert not is_active_on(date(2026, 6, 1), None, date(2026, 5, 1))


def test_hisse_kurallari() -> None:
    check_owner_share([Decimal(60)], Decimal(40))
    with pytest.raises(StructureRuleError) as exc:
        check_owner_share([Decimal(60), Decimal(40)], Decimal("0.01"))
    assert exc.value.code == "owner_shares_exceed"
    for bad in (Decimal(0), Decimal(101)):
        with pytest.raises(StructureRuleError, match="Hisse"):
            check_owner_share([], bad)


def test_bitis_kurallari() -> None:
    check_party_end(date(2026, 1, 1), None, date(2026, 1, 1))
    with pytest.raises(StructureRuleError, match="zaten"):
        check_party_end(date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1))
    with pytest.raises(StructureRuleError, match="önce"):
        check_party_end(date(2026, 1, 1), None, date(2025, 12, 31))


@pytest.mark.parametrize(
    "raw",
    [
        "5321234567",
        "05321234567",
        "905321234567",
        "+90 532 123 45 67",
        "0532 123 45 67",
        "532-123-45-67",
    ],
)
def test_5_1_telefon_e164(raw: str) -> None:
    assert normalize_tr_mobile(raw) == "+905321234567"


@pytest.mark.parametrize("raw", ["2121234567", "53212345", "abc"])
def test_5_2_gecersiz_telefon(raw: str) -> None:
    with pytest.raises(ValueError, match="cep"):
        normalize_tr_mobile(raw)


def test_ad_kurallari() -> None:
    assert normalize_name_part("  Ayşe   Nur ", "Ad") == "Ayşe Nur"
    with pytest.raises(ValueError, match="rakam"):
        normalize_name_part("Ayşe2", "Ad")
    with pytest.raises(ValueError, match="2–40"):
        normalize_name_part("A", "Soyad")
