"""Otopark — saf kurallar (PM kararı K30: park yeri ayrı kaynak, bölüme tarihçeli tahsis;
Yazılım Dokümanı OTP-01, §11.2).

- Park yeri: kod (site içinde tekil), tip (standart / engelli / elektrikli araç / misafir), konum,
  ücretli mi. Engelli yer tahsisinde sağlık verisi tutulmaz.
- Tahsis: bir yer aynı tarihte tek bölüme tahsislidir (tarih aralıkları çakışamaz). Bitiş tarihi
  boşsa süresiz. Tahsis silinmez; bitirilir (geçmiş kalır).
- Ücretli yerin ücreti dönem dışı ücret kuralıyla cariye yansır — o modül gelince bağlanır.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import tr_upper


class ParkingKind(StrEnum):
    STANDARD = "standard"
    DISABLED = "disabled"
    EV = "ev"
    GUEST = "guest"


CODE_MAX, LOCATION_MAX, NOTE_MAX = 20, 80, 200


@dataclass(frozen=True, slots=True)
class SpaceForm:
    code: str
    kind: ParkingKind
    location: str | None
    is_paid: bool
    note: str | None


def _optional(value: str | None, limit: int) -> str | None:
    return " ".join((value or "").split())[:limit] or None


def check_space(
    *,
    code: str | None,
    kind: str | None,
    location: str | None,
    is_paid: bool,
    note: str | None,
    taken: set[str],
) -> SpaceForm:
    """`taken`: diğer park yerlerinin büyük harf kodları."""
    errors: dict[str, str] = {}
    clean = tr_upper(" ".join((code or "").split()))
    if not clean:
        errors["code"] = "Park yeri kodunu yazın (ör. B-14)."
    elif len(clean) > CODE_MAX:
        errors["code"] = f"Kod en fazla {CODE_MAX} karakter olabilir."
    elif clean in taken:
        errors["code"] = "Bu kodla park yeri var."
    if kind not in set(ParkingKind):
        errors["kind"] = "Park yeri tipini seçin."
    if errors:
        raise FormFieldError(errors)
    return SpaceForm(
        clean,
        ParkingKind(str(kind)),
        _optional(location, LOCATION_MAX),
        is_paid,
        _optional(note, NOTE_MAX),
    )


def overlaps(start: date, end: date | None, other_start: date, other_end: date | None) -> bool:
    """İki tarih aralığı (bitiş dahil, boş = süresiz) çakışıyor mu?"""
    return (end is None or other_start <= end) and (other_end is None or start <= other_end)


def check_allocation(start: date | None, end: date | None) -> date:
    if start is None:
        raise FormFieldError({"start_date": "Başlangıç tarihini seçin."})
    if end is not None and end < start:
        raise FormFieldError({"end_date": "Bitiş tarihi başlangıçtan önce olamaz."})
    return start


def taken_error(space_code: str, unit_name: str) -> OperationRuleError:
    return OperationRuleError(
        "space_taken",
        f"{space_code} bu tarihte başka bölüme tahsisli ({unit_name}). "
        "Önce o tahsisi bitirin ya da başka tarih seçin.",
        conflict=True,
    )


def check_end(start: date, current_end: date | None, end: date | None) -> date:
    if current_end is not None:
        raise OperationRuleError("allocation_ended", "Bu tahsis zaten bitmiş.", conflict=True)
    if end is None:
        raise FormFieldError({"end_date": "Bitiş tarihini seçin."})
    if end < start:
        raise FormFieldError({"end_date": "Bitiş tarihi başlangıçtan önce olamaz."})
    return end
