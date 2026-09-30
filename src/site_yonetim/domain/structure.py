"""Site yapısı — saf kurallar (docs/03-veri-modeli.md §3–§5)."""

import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.text import slugify


class UnitUsage(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    STORAGE = "storage"
    PARKING = "parking"


class PartyRole(StrEnum):
    OWNER = "owner"  # malik
    TENANT = "tenant"  # kiracı
    RESIDENT = "resident"  # oturan
    PROXY = "proxy"  # vekil


class AccountKind(StrEnum):
    OWNER = "owner"  # malik hesabı — demirbaş/yatırım (KMK m.22)
    OCCUPANT = "occupant"  # oturan hesabı — aidat/işletme gideri


class StructureRuleError(Exception):
    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


FULL_SHARE = Decimal(100)
_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def is_active_on(start: date, end: date | None, day: date) -> bool:
    return start <= day and (end is None or end >= day)


def reference_base(block_name: str, number: str) -> str:
    """`{BLOK}{NO}`: yalnız harf/rakam, büyük harf, ASCII (havale açıklamasında kullanılır)."""
    ascii_text = slugify(f"{block_name}{number}").upper()
    return _NON_ALNUM.sub("", ascii_text) or "BB"


def reference_code(base: str, letter: str, taken: Iterable[str]) -> str:
    """`A12-M`; çakışırsa `A12-M2`, `A12-M3` … (docs/03 §5)."""
    used = set(taken)
    code = f"{base}-{letter}"
    counter = 2
    while code in used:
        code = f"{base}-{letter}{counter}"
        counter += 1
    return code


def accounts_to_open(
    role: PartyRole, *, unit_has_active_tenant: bool
) -> list[tuple[AccountKind, str]]:
    """Taraf eklenince açılan cari hesaplar ve referans kodu harfi (docs/03 §5).

    - Malik → malik hesabı (`-M`); kiracı yoksa malike oturan hesabı da (`-O`): aidatın
      yazılacağı bir hesap her zaman bulunsun.
    - Kiracı → kiracıya oturan hesabı (`-K`).
    - Oturan / vekil → hesap açılmaz.
    """
    if role is PartyRole.OWNER:
        accounts = [(AccountKind.OWNER, "M")]
        if not unit_has_active_tenant:
            accounts.append((AccountKind.OCCUPANT, "O"))
        return accounts
    if role is PartyRole.TENANT:
        return [(AccountKind.OCCUPANT, "K")]
    return []


def check_owner_share(active_owner_shares: Iterable[Decimal], new_share: Decimal) -> None:
    """Hisseli mülkiyet: aynı gün etkin maliklerin payları toplamı %100'ü aşamaz."""
    if not Decimal(0) < new_share <= FULL_SHARE:
        raise StructureRuleError(
            "invalid_share", "share_percent", "Hisse oranı 0'dan büyük ve en fazla 100 olmalı."
        )
    if sum(active_owner_shares, Decimal(0)) + new_share > FULL_SHARE:
        raise StructureRuleError(
            "owner_shares_exceed",
            "share_percent",
            "Maliklerin hisseleri toplamı %100'ü aşamaz. Önce mevcut malikin bitiş tarihini girin "
            "ya da hisse oranını düşürün.",
        )


def check_party_end(start: date, current_end: date | None, end_date: date) -> None:
    if current_end is not None:
        raise StructureRuleError(
            "party_already_ended", None, "Bu kişinin bölümle ilişkisi zaten sona erdirilmiş."
        )
    if end_date < start:
        raise StructureRuleError(
            "end_before_start", "end_date", "Bitiş tarihi başlangıç tarihinden önce olamaz."
        )
