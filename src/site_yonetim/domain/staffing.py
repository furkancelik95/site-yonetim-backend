"""İnsan kaynakları — saf kurallar (Yazılım Dokümanı Bölüm 9, IK-01..05; servis istekleri 35–39).

- Vardiya şablonu: başlangıç–bitiş "SS:DD"; bitiş ≤ başlangıç → vardiya ertesi güne taşar.
  Beklenen çalışma = vardiya süresi − mola. Gece çalışması 22:00–06:00 arasına düşen dakikalar.
- Çakışma (IK-02): aynı kişiye zamanı çakışan vardiya, onaylı izin günü, iki vardiya arası
  11 saatten az dinlenme [belge: ÖNERİ — iş mevzuatı teyidi]. Çakışma atamayı engellemez.
- Puantaj (IK-04): çalışılan = beklenen − geç − erken + fazla mesai (alt sınır 0). Giriş-çıkış
  doğrulaması açıksa kayıtlarla karşılaştırılır; tolerans dakikası tesis ayarı.
- İzin günü takvim günüdür (bitiş − başlangıç + 1). Raporlu izinde tanı bilgisi tutulmaz.
- Platform T.C. kimlik no tutmaz; yalnız bordro sicil no (harf, rakam, tire; ≤ 20).
"""

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

DAY = 1440
REST_MIN_MINUTES = 11 * 60  # [ÖNERİ] iş mevzuatı teyidi bekliyor
BREAK_MAX = 180
FIX_MINUTES_MAX = 960
PAYROLL_NO_MAX = 20
TEMPLATE_NAME_MAX = 40
TEMPLATE_TEXT_MAX = 40
LEAVE_NOTE_MAX = 200
REASON_MAX = 200
REASON_MIN = 3
LEAVE_DAYS_MAX = 366
HOURS = Decimal("0.01")

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PAYROLL = re.compile(r"^[A-Za-z0-9-]{1,20}$")
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

# Gece pencereleri (vardiya gününün başından dakika): 00–06, 22–30 (ertesi 06), 46–54.
_NIGHT_WINDOWS = ((0, 360), (1320, 1800), (2760, 3240))


class LeaveType(StrEnum):
    """İzin türleri — tesis ayarı olacak (IK-03); şimdilik belgenin varsayılan dört türü."""

    ANNUAL = "annual"  # yıllık
    EXCUSE = "excuse"  # mazeret
    SICK = "sick"  # raporlu — tanı bilgisi tutulmaz
    UNPAID = "unpaid"  # ücretsiz


LEAVE_LABELS: dict[LeaveType, str] = {
    LeaveType.ANNUAL: "yıllık",
    LeaveType.EXCUSE: "mazeret",
    LeaveType.SICK: "raporlu",
    LeaveType.UNPAID: "ücretsiz",
}


class LeaveStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


ACTIVE_LEAVE = frozenset({LeaveStatus.PENDING, LeaveStatus.APPROVED})


class ConflictKind(StrEnum):
    OVERLAP = "overlap"
    LEAVE = "leave"
    REST = "rest"


class TimesheetStatus(StrEnum):
    WORKED = "worked"
    ABSENT = "absent"
    LEAVE = "leave"


class AttendanceMethod(StrEnum):
    NONE = "none"  # doğrulama yok: puantaj plana göre
    QR = "qr"  # kulübedeki sabit kod (yönetici yeniler)
    LOCATION = "location"  # o anki konum tesis sınırında mı (sürekli takip yok)


class AttendanceKind(StrEnum):
    IN = "in"
    OUT = "out"


class AttendanceFlag(StrEnum):
    """Giriş-çıkış karşılaştırmasının işaretlediği istisnalar (IK-04 adım 4)."""

    NO_CHECK_IN = "no_check_in"  # eksik vardiya
    NO_CHECK_OUT = "no_check_out"
    LATE = "late"
    EARLY_LEAVE = "early_leave"
    OVERTIME = "overtime"
    UNVERIFIED = "unverified"  # QR yanlış ya da konum tesis sınırı dışında


FLAG_LABELS: dict[AttendanceFlag, str] = {
    AttendanceFlag.NO_CHECK_IN: "Giriş kaydı yok",
    AttendanceFlag.NO_CHECK_OUT: "Çıkış kaydı yok",
    AttendanceFlag.LATE: "Geç giriş",
    AttendanceFlag.EARLY_LEAVE: "Erken çıkış",
    AttendanceFlag.OVERTIME: "Vardiya sonrası çıkış",
    AttendanceFlag.UNVERIFIED: "Doğrulanamayan kayıt",
}


# --- Saat ve süre ---------------------------------------------------------------------------


def parse_time(value: str | None) -> int | None:
    """ "08:30" → 510 (gün başından dakika); biçimsizse None."""
    match = _TIME.match(value or "")
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def format_time(minutes: int) -> str:
    minutes %= DAY
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class Span:
    """Vardiyanın gün başından başlangıç/bitiş dakikası; gece vardiyasında bitiş > 1440."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


def span(start: int, end: int) -> Span:
    """Bitiş ≤ başlangıç → ertesi güne taşar (00:00–00:00 tam gün sayılır)."""
    return Span(start, end + DAY if end <= start else end)


def planned_minutes(s: Span, break_minutes: int) -> int:
    return max(0, s.length - break_minutes)


def night_minutes(s: Span) -> int:
    """22:00–06:00 arasına düşen dakikalar (mola düşülmez)."""
    return sum(max(0, min(s.end, hi) - max(s.start, lo)) for lo, hi in _NIGHT_WINDOWS)


def worked_minutes(planned: int, late: int, early: int, overtime: int) -> int:
    return max(0, planned - late - early + overtime)


def hours(minutes: int) -> Decimal:
    """Dakika → saat (2 ondalık, ROUND_HALF_UP): 400 → 6.67."""
    return (Decimal(minutes) / Decimal(60)).quantize(HOURS, rounding=ROUND_HALF_UP)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def days_between(first: date, last: date) -> int:
    """Takvim günü, iki uç dahil."""
    return (last - first).days + 1


def month_bounds(value: str) -> tuple[date, date] | None:
    """ "2026-10" → (1 Ekim, 31 Ekim); biçimsizse None."""
    match = _MONTH.match(value or "")
    if match is None:
        return None
    first = date(int(match.group(1)), int(match.group(2)), 1)
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, following - timedelta(days=1)


def dates(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


# --- Alan kontrolleri -----------------------------------------------------------------------


def check_payroll_no(value: str | None) -> str | None:
    """Boş → None. Harf, rakam, tire; ≤ 20 karakter. Büyük harfe çevrilir (tekillik)."""
    text = (value or "").strip()
    if not text:
        return None
    if not _PAYROLL.match(text):
        raise ValueError("Sicil no harf, rakam ve tire; en fazla 20 karakter.")
    return text.upper()


def check_fix_minutes(value: int | None) -> int:
    if value is None:
        return 0
    if not 0 <= value <= FIX_MINUTES_MAX:
        raise ValueError(f"0–{FIX_MINUTES_MAX} dakika.")
    return value


# --- Çakışma --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedShift:
    assignment_id: object  # uuid — saf katman tipe bağlanmaz
    staff_id: object
    day: date
    span: Span


@dataclass(frozen=True, slots=True)
class LeaveDays:
    staff_id: object
    first: date
    last: date
    leave_type: LeaveType


@dataclass(frozen=True, slots=True)
class Conflict:
    assignment_id: object
    kind: ConflictKind
    message: str


def _absolute(shift: PlannedShift) -> tuple[int, int]:
    base = shift.day.toordinal() * DAY
    return base + shift.span.start, base + shift.span.end


def _hours_text(minutes: int) -> str:
    value = (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def leave_on(leaves: Iterable[LeaveDays], staff_id: object, day: date) -> LeaveDays | None:
    return next((x for x in leaves if x.staff_id == staff_id and x.first <= day <= x.last), None)


def find_conflicts(shifts: Sequence[PlannedShift], leaves: Sequence[LeaveDays]) -> list[Conflict]:
    """Kişi bazında sıralı vardiyalar: önceki en geç bitişle çakışma ya da < 11 saat dinlenme
    sonraki atamaya yazılır; onaylı izin gününe düşen atama `leave`."""
    out: list[Conflict] = []
    by_staff: dict[object, list[PlannedShift]] = {}
    for shift in shifts:
        by_staff.setdefault(shift.staff_id, []).append(shift)
    for staff_id, items in by_staff.items():
        ordered = sorted(items, key=lambda x: (_absolute(x), str(x.assignment_id)))
        latest_end: int | None = None
        for shift in ordered:
            start, end = _absolute(shift)
            leave = leave_on(leaves, staff_id, shift.day)
            if leave is not None:
                out.append(
                    Conflict(
                        shift.assignment_id,
                        ConflictKind.LEAVE,
                        f"Onaylı izin günü ({LEAVE_LABELS[leave.leave_type]}) — vardiya atanmalı",
                    )
                )
            if latest_end is not None:
                if start < latest_end:
                    out.append(
                        Conflict(
                            shift.assignment_id, ConflictKind.OVERLAP, "Aynı kişiye çakışan vardiya"
                        )
                    )
                elif start - latest_end < REST_MIN_MINUTES:
                    out.append(
                        Conflict(
                            shift.assignment_id,
                            ConflictKind.REST,
                            f"İki vardiya arası dinlenme {_hours_text(start - latest_end)} saat "
                            "(11 saatten az)",
                        )
                    )
            latest_end = end if latest_end is None else max(latest_end, end)
    return out


# --- Giriş-çıkış karşılaştırması ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detected:
    status: TimesheetStatus
    late: int
    early: int
    overtime: int
    flags: tuple[AttendanceFlag, ...]


def detect(
    s: Span,
    *,
    check_in: int | None,
    check_out: int | None,
    unverified: bool,
    late_tolerance: int,
    early_tolerance: int,
) -> Detected:
    """Giriş-çıkış (vardiya gününün başından dakika) ↔ plan. Tolerans içi sapma sayılmaz;
    tolerans aşılırsa sapmanın **tamamı** istisna olur (belge S1: 20 dk geç, tolerans 10 →
    20 dk geç gelme). Giriş yoksa eksik vardiya: "gelmedi" önerilir, İK düzeltir."""
    flags: list[AttendanceFlag] = []
    if unverified:
        flags.append(AttendanceFlag.UNVERIFIED)
    if check_in is None:
        flags.append(AttendanceFlag.NO_CHECK_IN)
        return Detected(TimesheetStatus.ABSENT, 0, 0, 0, tuple(flags))
    late = check_in - s.start
    late = late if late > late_tolerance else 0
    if late:
        flags.append(AttendanceFlag.LATE)
    early = overtime = 0
    if check_out is None:
        flags.append(AttendanceFlag.NO_CHECK_OUT)
    else:
        gap = s.end - check_out
        if gap > early_tolerance:
            early = gap
            flags.append(AttendanceFlag.EARLY_LEAVE)
        elif -gap > late_tolerance:
            overtime = -gap
            flags.append(AttendanceFlag.OVERTIME)
    return Detected(
        TimesheetStatus.WORKED,
        min(late, FIX_MINUTES_MAX),
        min(early, FIX_MINUTES_MAX),
        min(overtime, FIX_MINUTES_MAX),
        tuple(flags),
    )


EARTH_RADIUS_M = 6_371_000


def distance_m(lat1: Decimal, lon1: Decimal, lat2: Decimal, lon2: Decimal) -> int:
    """İki nokta arası yaklaşık mesafe (haversine, metre)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2) - math.radians(lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)))


# --- Puantaj aktarım dosyası ----------------------------------------------------------------

EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("payroll_no", "Bordro sicil no"),
    ("name", "Ad soyad"),
    ("worked_days", "Çalışılan gün"),
    ("worked_hours", "Çalışma saati"),
    ("overtime_hours", "Fazla mesai saati"),
    ("night_hours", "Gece çalışması saati"),
    ("absent_days", "Devamsız gün"),
    ("leave_annual", "Yıllık izin"),
    ("leave_excuse", "Mazeret izni"),
    ("leave_sick", "Raporlu izin"),
    ("leave_unpaid", "Ücretsiz izin"),
    ("holiday_hours", "Resmî tatil çalışması"),
)
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


_HOUR_KEYS = frozenset({"worked_hours", "overtime_hours", "night_hours", "holiday_hours"})


def _csv_cell(value: object, key: str = "") -> str:
    if key in _HOUR_KEYS and isinstance(value, str) and value:
        value = Decimal(value)
    if isinstance(value, Decimal):
        return format(value, "f").replace(".", ",")
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_START):
        text = "'" + text  # formül enjeksiyonu: hücre metin kalsın
    if any(ch in text for ch in ';"\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def export_csv(month: str, rows: Sequence[dict[str, object]]) -> bytes:
    """`;` ayraçlı, UTF-8 BOM'lu CSV (Excel Türkçe yerel ayarı); ondalık virgül."""
    lines = [";".join(["Dönem", *(label for _, label in EXPORT_COLUMNS)])]
    for row in rows:
        lines.append(
            ";".join([month, *(_csv_cell(row.get(key), key) for key, _ in EXPORT_COLUMNS)])
        )
    return ("﻿" + "\r\n".join(lines) + "\r\n").encode("utf-8")
