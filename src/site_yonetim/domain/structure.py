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


# --- Yapı tanımları (servis isteği 19, Yazılım Dokümanı YAP-02) ---------------------------


class CommonAreaKind(StrEnum):
    POOL = "pool"  # havuz
    HALL = "hall"  # salon
    GYM = "gym"  # spor salonu
    GARDEN = "garden"  # bahçe
    PLAYGROUND = "playground"  # oyun alanı
    PARKING = "parking"  # otopark
    TECHNICAL = "technical"  # teknik oda
    ROOF = "roof"  # çatı / teras
    OTHER = "other"


# Yürürlük tarihli (versiyonlu) bölüm öznitelikleri: m², arsa payı, tip (YAP-02 S3).
VERSIONED_FIELDS: tuple[str, ...] = (
    "gross_area",
    "net_area",
    "land_share_numerator",
    "land_share_denominator",
    "unit_type_id",
)
SHARE_TOLERANCE = Decimal("0.01")


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def earliest_effective_date(today: date, closed_months: Iterable[tuple[int, int]]) -> date:
    """Yürürlük/devir tarihinin en erkeni: içinde bulunulan ayın başı; o ay (ve sonrakiler)
    kapatılmışsa ilk açık ayın başı. Geçmiş tahakkuklar değişmesin (YAP-02 S2: PERIOD_LOCKED)."""
    closed = set(closed_months)
    earliest = month_start(today)
    while (earliest.year, earliest.month) in closed:
        earliest = next_month(earliest)
    return earliest


def check_effective_date(effective: date, earliest: date, field: str) -> None:
    if effective < earliest:
        raise StructureRuleError(
            "period_locked",
            field,
            f"Yürürlük tarihi geçmiş bir döneme düşemez; en erken {earliest:%d.%m.%Y}.",
        )


def check_full_owner_shares(shares: Iterable[Decimal]) -> None:
    """Malik değişiminde yeni maliklerin hisse toplamı tam %100 (±0,01)."""
    total = sum(shares, Decimal(0))
    if abs(total - FULL_SHARE) > SHARE_TOLERANCE:
        shown = f"{total:.2f}".replace(".", ",")
        raise StructureRuleError(
            "shares_not_full",
            "new_owners",
            f"Yeni maliklerin hisse toplamı tam %100 olmalı (şu an %{shown}).",
        )


def most_common_count(counts: dict[int, int]) -> int | None:
    """`{değer: adet}` içinde en sık değer; eşitlikte küçük olan (yapı özeti: etkin
    bölümlerde en sık payda)."""
    if not counts:
        return None
    return min(counts, key=lambda v: (-counts[v], v))
