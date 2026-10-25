"""Devriye — saf kurallar (servis isteği 46; Yazılım Dokümanı GUV-09, §10.3).

- Kontrol noktası kodu 6 hane, tesis içinde tekil; QR içeriği `SY-DEVRIYE:{kod}` (frontend üretir).
  Okutmada yalnız o anki zaman kaydedilir; sürekli konum takibi yok.
- Tur planı: noktalar ve sırası (sıralı / serbest), başlangıç saatleri, izin verilen süre.
- Takvim: başlangıçtan 15 dk sonra başlamayan tur `not_started` (yöneticiye kritik bildirim),
  süresi aşan açık tur `overdue`.
- Sapma raporu: kaçırılan nokta, sıra dışı okutma, süre aşımı, geç başlama.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.text import tr_lower


class PatrolTag(StrEnum):
    QR = "qr"
    NFC = "nfc"
    BOTH = "both"


class SlotStatus(StrEnum):
    UPCOMING = "upcoming"  # sırada
    DUE = "due"  # başlamalı (15 dk tolerans içinde)
    NOT_STARTED = "not_started"  # başlamadı — kritik
    RUNNING = "running"
    OVERDUE = "overdue"  # süre aşıldı, tur hâlâ açık
    DONE = "done"
    DONE_WITH_DEVIATION = "done_with_deviation"


NOT_STARTED_AFTER = timedelta(minutes=15)
POINT_CODE_DIGITS = 6
ALLOWED_MINUTES_RANGE = (5, 240)
MAX_POINTS, MAX_TIMES = 100, 24
NAME_MAX = 60
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _name(value: str | None, label: str, taken: set[str] | None = None) -> tuple[str, str | None]:
    text = " ".join((value or "").split())
    if not text:
        return text, f"{label} adını yazın."
    if len(text) > NAME_MAX:
        return text, f"Ad en fazla {NAME_MAX} karakter olabilir."
    if taken is not None and tr_lower(text) in taken:
        return text, "Bu adla nokta var."
    return text, None


@dataclass(frozen=True, slots=True)
class PointForm:
    name: str
    location: str | None
    area: str | None
    tag: PatrolTag


def _optional(value: str | None, limit: int) -> str | None:
    text = " ".join((value or "").split())
    return text[:limit] or None


def check_point(
    *, name: str | None, location: str | None, area: str | None, tag: str | None, taken: set[str]
) -> PointForm:
    errors: dict[str, str] = {}
    text, problem = _name(name, "Nokta", taken)
    if problem:
        errors["name"] = problem
    if tag not in set(PatrolTag):
        errors["tag"] = "Etiket tipini seçin."
    if errors:
        raise FormFieldError(errors)
    return PointForm(text, _optional(location, 80), _optional(area, 40), PatrolTag(str(tag)))


@dataclass(frozen=True, slots=True)
class PlanForm:
    name: str
    point_ids: tuple[str, ...]
    ordered: bool
    start_times: tuple[str, ...]
    allowed_minutes: int
    assignee: str | None


def check_plan(
    *,
    name: str | None,
    point_ids: list[str] | None,
    ordered: bool,
    start_times: list[str] | None,
    allowed_minutes: object,
    assignee: str | None,
    active_points: set[str],
) -> PlanForm:
    errors: dict[str, str] = {}
    text, problem = _name(name, "Tur")
    if problem:
        errors["name"] = problem
    points = tuple(point_ids or ())
    if (
        not points
        or len(points) > MAX_POINTS
        or len(set(points)) != len(points)
        or any(p not in active_points for p in points)
    ):
        errors["point_ids"] = "En az bir etkin nokta seçin (her nokta bir kez)."
    times = tuple(start_times or ())
    if not times or len(times) > MAX_TIMES or any(not _TIME_RE.match(t) for t in times):
        errors["start_times"] = "Başlangıç saatlerini SS:DD biçiminde girin (ör. 22:00, 01:00)."
    low, high = ALLOWED_MINUTES_RANGE
    if not (
        isinstance(allowed_minutes, int)
        and not isinstance(allowed_minutes, bool)
        and low <= allowed_minutes <= high
    ):
        errors["allowed_minutes"] = "İzin verilen süre 5–240 dakika olmalı."
    if errors:
        raise FormFieldError(errors)
    return PlanForm(
        name=text,
        point_ids=points,
        ordered=ordered,
        start_times=tuple(sorted(set(times))),
        allowed_minutes=int(str(allowed_minutes)),
        assignee=_optional(assignee, 40),
    )


def is_out_of_order(
    ordered: bool, point_ids: tuple[str, ...], scanned: set[str], point: str
) -> bool:
    """Sıralı turda beklenen (ilk okutulmamış) nokta dışındaki okutma sıra dışıdır."""
    if not ordered:
        return False
    expected = next((p for p in point_ids if p not in scanned), None)
    return expected is not None and expected != point


@dataclass(frozen=True, slots=True)
class Scan:
    point_id: str
    out_of_order: bool


@dataclass(frozen=True, slots=True)
class PatrolReport:
    missed: list[str]
    out_of_order: list[str]
    duration_minutes: int
    overtime: bool
    late_start_minutes: int
    total_points: int
    scanned_points: int

    @property
    def ok(self) -> bool:
        return not self.missed and not self.out_of_order and not self.overtime


def build_report(
    *,
    point_ids: tuple[str, ...],
    names: dict[str, str],
    scans: list[Scan],
    started_at: datetime,
    finished_at: datetime | None,
    now: datetime,
    allowed_minutes: int,
    slot_start: datetime | None,
) -> PatrolReport:
    scanned = {s.point_id for s in scans}
    end = finished_at or now
    minutes = max(0, int((end - started_at).total_seconds() // 60))
    late = 0
    if slot_start is not None:
        late = max(0, int((started_at - slot_start).total_seconds() // 60))
    return PatrolReport(
        missed=[names.get(p, "?") for p in point_ids if p not in scanned],
        out_of_order=[names.get(s.point_id, "?") for s in scans if s.out_of_order],
        duration_minutes=minutes,
        overtime=(end - started_at) > timedelta(minutes=allowed_minutes),
        late_start_minutes=late,
        total_points=len(point_ids),
        scanned_points=len(scanned & set(point_ids)),
    )


def slot_status(
    *,
    slot_start: datetime,
    now: datetime,
    started_at: datetime | None,
    finished: bool,
    report_ok: bool,
    allowed_minutes: int,
) -> SlotStatus:
    if started_at is not None:
        if finished:
            return SlotStatus.DONE if report_ok else SlotStatus.DONE_WITH_DEVIATION
        if now - started_at > timedelta(minutes=allowed_minutes):
            return SlotStatus.OVERDUE
        return SlotStatus.RUNNING
    if now < slot_start:
        return SlotStatus.UPCOMING
    if now - slot_start > NOT_STARTED_AFTER:
        return SlotStatus.NOT_STARTED
    return SlotStatus.DUE


def current_slot(start_times: tuple[str, ...], hhmm: str) -> str | None:
    """Başlatılan turun saati: şu andan önceki son başlangıç saati (yoksa plan dışı yürütme)."""
    earlier = [t for t in start_times if t <= hhmm]
    return earlier[-1] if earlier else None
