"""Yönetim paketi — saf kurallar (frontend servis istekleri 14–18): toplantı, anket, sözleşme,
demirbaş ve stok, personel.

Tarih parametre olarak gelir (domain saati okumaz). Stok miktarı tam ondalık (`Decimal`), en çok
3 hane; kayan nokta yok.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

# --- Toplantı (14) ------------------------------------------------------------------


class MeetingKind(StrEnum):
    GENERAL_ORDINARY = "general_ordinary"  # olağan genel kurul
    GENERAL_EXTRAORDINARY = "general_extraordinary"  # olağanüstü genel kurul
    BOARD = "board"  # yönetim kurulu


class MeetingStatus(StrEnum):
    PLANNED = "planned"
    HELD = "held"  # kararlar girildi — kayıt kilitli
    CANCELLED = "cancelled"


class AgendaResult(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    POSTPONED = "postponed"
    INFO = "info"  # bilgi verildi — karar metni gerekmez


AGENDA_MIN, AGENDA_MAX = 1, 30


# --- Anket (15) ---------------------------------------------------------------------


class PollAudience(StrEnum):
    ALL = "all"  # bölümdeki malik ya da oturan — ilk veren
    OWNERS = "owners"
    TENANTS = "tenants"  # kiracı/oturan


class PollStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


OPTIONS_MIN, OPTIONS_MAX = 2, 8


def poll_status(ends_on: date, closed_early: bool, today: date) -> PollStatus:
    """`ends_on` günü dahil açık; ertesi gün kendiliğinden kapanır."""
    return PollStatus.CLOSED if closed_early or today > ends_on else PollStatus.OPEN


# --- Sözleşme (16) ------------------------------------------------------------------


class ContractCategory(StrEnum):
    ELEVATOR = "elevator"
    CLEANING = "cleaning"
    SECURITY = "security"
    GARDEN = "garden"
    MAINTENANCE = "maintenance"
    INSURANCE = "insurance"
    POOL = "pool"
    OTHER = "other"


class ContractPeriod(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    ONCE = "once"


class ContractState(StrEnum):
    ACTIVE = "active"
    EXPIRING = "expiring"  # bitişe ihbar süresi kadar ya da daha az kaldı
    EXPIRED = "expired"
    ARCHIVED = "archived"


def contract_state(
    *, end_date: date, notice_days: int, archived: bool, today: date
) -> tuple[int, ContractState]:
    """(bitişe kalan gün, durum). Gün hesabı sitenin yerel tarihiyle (Europe/Istanbul)."""
    days_left = (end_date - today).days
    if archived:
        return days_left, ContractState.ARCHIVED
    if days_left < 0:
        return days_left, ContractState.EXPIRED
    if days_left <= notice_days:
        return days_left, ContractState.EXPIRING
    return days_left, ContractState.ACTIVE


# --- Demirbaş ve stok (17) ----------------------------------------------------------


class AssetStatus(StrEnum):
    IN_USE = "in_use"
    BROKEN = "broken"
    RETIRED = "retired"  # kullanım dışı — silme yerine


class StockDirection(StrEnum):
    IN = "in"
    OUT = "out"


QUANTITY_PLACES = 3
QUANTITY_MAX = Decimal("99999999.999")


def asset_code(sequence: int) -> str:
    """asset_code(1) → "DB-0001" """
    return f"DB-{sequence:04d}"


def parse_quantity(value: str | int | Decimal | None, *, allow_zero: bool = False) -> Decimal:
    """Miktar metni → `Decimal`; en çok 3 ondalık, sıfırdan büyük (ya da `allow_zero`).
    Geçersizse `ValueError`. `"12,5"` de kabul edilir."""
    text = str(value if value is not None else "").strip().replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(text) from exc
    exponent = number.as_tuple().exponent
    if (
        not number.is_finite()
        or (isinstance(exponent, int) and exponent < -QUANTITY_PLACES)
        or number < 0
        or (number == 0 and not allow_zero)
        or number > QUANTITY_MAX
    ):
        raise ValueError(text)
    return number.quantize(Decimal(1).scaleb(-QUANTITY_PLACES))


def format_quantity_tr(value: Decimal) -> str:
    """format_quantity_tr(Decimal("4.500")) → "4,5"; tam sayıda ondalık yok."""
    text = f"{value.normalize():f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def format_quantity(value: Decimal) -> str:
    """API metni: format_quantity(Decimal("4.500")) → "4.5" """
    return format_quantity_tr(value).replace(",", ".")


# --- Personel (18) ------------------------------------------------------------------


class Employer(StrEnum):
    SITE = "site"  # site kadrosu
    CONTRACTOR = "contractor"  # taşeron firma


def staff_active(end_date: date | None, today: date) -> bool:
    """Ayrılışı olmayan ya da ayrılışı bugün/ileride olan çalışır sayılır."""
    return end_date is None or end_date >= today
