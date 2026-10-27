"""Hizmetler ve talep akışı — saf kurallar (Yazılım Dokümanı §12, HIZ-01..05).

- **Kategori ve SLA (HIZ-03):** kategoriler site tanımlıdır (docs/12 D-26); her kategoride saat
  tipi (7/24 ya da çalışma saati) ve öncelik bazında çözüm süresi. Çalışma saati tipinde yalnız
  takvimdeki açık saatler sayılır — mesai dışında **sayaç durur** (§12.2). Resmî tatil takvimi
  platformdan (S-03) gelince düşülecek; şimdilik yok.
- Talep açılırken kural anlık görüntüsü talebe yazılır (hedef saat, saat tipi, takvim): kural
  değişikliği açık talepleri etkilemez (HIZ-03 adım 4).
- %80 → "süre doluyor", %100 → "SLA aşıldı" (HIZ-02 SLA akışı).
- **Ücretli hizmet (HIZ-04):** kalem tutarı = miktar × birim fiyat (yarım yukarı, kuruş);
  gerçekleşen teklifi aşarsa ek tutar sakin onayına gider.
- **Tadilat/taşınma (HIZ-05):** tarih bugünden önce olamaz; çalışma saatleri tesis ayarıdır.

Zaman hesabı `datetime` aritmetiğidir; saati okumaz (`now` çağıran verir).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from site_yonetim.domain.money import CENT, ZERO, round_money
from site_yonetim.domain.operations import RequestPriority


class HourType(StrEnum):
    ALWAYS = "24x7"
    BUSINESS = "business"


class SlaState(StrEnum):
    OK = "ok"
    WARNING = "warning"  # kullanılan ≥ %80
    BREACHED = "breached"  # kullanılan ≥ %100
    MET = "met"  # süresinde çözüldü
    MISSED = "missed"  # geç çözüldü


WARNING_RATIO = Decimal("0.8")
HOURS_MIN, HOURS_MAX = 1, 2160  # 90 gün
DEFAULT_HOURS: dict[RequestPriority, int] = {
    RequestPriority.URGENT: 4,
    RequestPriority.HIGH: 24,
    RequestPriority.NORMAL: 72,
    RequestPriority.LOW: 168,
}
AUTO_CLOSE_MIN, AUTO_CLOSE_MAX = 1, 30
MERGE_WINDOW_MIN, MERGE_WINDOW_MAX = 1, 168
LOW_RATING_MIN, LOW_RATING_MAX = 1, 4
DEFAULT_AUTO_CLOSE_DAYS = 3  # §12.3 "X gün (varsayılan 3)"
DEFAULT_MERGE_WINDOW_HOURS = 24  # §12.3 "son 24 saatte (tesis ayarı)"
DEFAULT_LOW_RATING = 2  # §12.3 "düşük puan (≤ 2)"
SCORE_MIN, SCORE_MAX = 1, 5


@dataclass(frozen=True, slots=True)
class CategoryDefault:
    key: str
    name: str
    hour_type: HourType
    priority: RequestPriority = RequestPriority.NORMAL


# HIZ-03 "Varsayılan kategoriler" + acil kategoriler (7/24). Eski sabit listenin anahtarları
# korunur (göç mevcut talepleri bu anahtarlarla bağlar); "common_area" eski listeden gelir.
DEFAULT_CATEGORIES: tuple[CategoryDefault, ...] = (
    CategoryDefault("plumbing", "Su / tesisat", HourType.ALWAYS),
    CategoryDefault("electrical", "Elektrik", HourType.BUSINESS),
    CategoryDefault("elevator", "Asansör", HourType.ALWAYS),
    CategoryDefault("heating", "Isıtma", HourType.BUSINESS),
    CategoryDefault("cleaning", "Ortak alan temizliği", HourType.BUSINESS),
    CategoryDefault("garden", "Bahçe", HourType.BUSINESS),
    CategoryDefault("security", "Güvenlik", HourType.ALWAYS),
    CategoryDefault("noise", "Gürültü / şikâyet", HourType.BUSINESS),
    CategoryDefault("parking", "Otopark", HourType.BUSINESS),
    CategoryDefault("meter_dispute", "Sayaç itirazı", HourType.BUSINESS, RequestPriority.LOW),
    CategoryDefault("common_area", "Ortak alan", HourType.BUSINESS),
    CategoryDefault("flood", "Su baskını", HourType.ALWAYS, RequestPriority.URGENT),
    CategoryDefault("elevator_trapped", "Asansörde mahsur kalma", HourType.ALWAYS,
                    RequestPriority.URGENT),
    CategoryDefault("gas_smell", "Gaz kokusu", HourType.ALWAYS, RequestPriority.URGENT),
    CategoryDefault("other", "Diğer", HourType.BUSINESS),
)  # fmt: skip
OTHER_KEY = "other"


class RuleError(Exception):
    """Alan hataları (422). `fields`: alan → Türkçe mesaj."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(fields.values()))
        self.fields = fields


# --- Çalışma takvimi ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DayHours:
    closed: bool
    open: time
    close: time

    def as_json(self) -> dict[str, Any]:
        return {"closed": self.closed, "open": f"{self.open:%H:%M}", "close": f"{self.close:%H:%M}"}


Calendar = tuple[DayHours, ...]  # 7 gün, 0 = pazartesi
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _hm(value: object) -> time | None:
    if not isinstance(value, str) or not _HM.match(value):
        return None
    return time(int(value[:2]), int(value[3:]))


# Varsayılan: hafta içi 09–18, cumartesi 10–14, pazar kapalı (servis isteği 47).
DEFAULT_CALENDAR: Calendar = (
    *(DayHours(False, time(9), time(18)) for _ in range(5)),
    DayHours(False, time(10), time(14)),
    DayHours(True, time(9), time(18)),
)


def parse_calendar(raw: object) -> Calendar:
    """`[{closed, open: "09:00", close: "18:00"}] × 7` → takvim; hatalıysa `RuleError`."""
    message = "Açık günlerde açılış kapanıştan önce olmalı (SS:DD)."
    if not isinstance(raw, list) or len(raw) != 7:
        raise RuleError({"calendar": "Haftanın 7 günü için çalışma saatlerini girin."})
    days: list[DayHours] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuleError({"calendar": message})
        closed = bool(item.get("closed"))
        opens, closes = _hm(item.get("open")), _hm(item.get("close"))
        if closed:
            days.append(DayHours(True, opens or time(9), closes or time(18)))
            continue
        if opens is None or closes is None or closes <= opens:
            raise RuleError({"calendar": message})
        days.append(DayHours(False, opens, closes))
    if all(d.closed for d in days):
        raise RuleError({"calendar": "En az bir çalışma günü olmalı."})
    return tuple(days)


def calendar_json(calendar: Calendar) -> list[dict[str, Any]]:
    return [d.as_json() for d in calendar]


_MAX_DAYS = 40_000  # 2160 saat, haftada tek açık saatle bile sığar


def _window(day: date, hours: DayHours, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(day, hours.open, tz), datetime.combine(day, hours.close, tz)


def add_business_minutes(
    start: datetime, minutes: int, calendar: Calendar, tz: ZoneInfo
) -> datetime:
    """Takvimin açık saatlerinde `minutes` ilerler; mesai dışında sayaç durur.

    Örnek (HIZ-02 S1): mesai 09–18, cuma 17:00 + 4 saat → pazartesi 12:00.
    """
    rest = timedelta(minutes=minutes)
    cursor = start.astimezone(tz)
    day = cursor.date()
    for _ in range(_MAX_DAYS):
        hours = calendar[day.weekday()]
        if not hours.closed:
            opens, closes = _window(day, hours, tz)
            begin = max(cursor, opens)
            if begin < closes:
                available = closes - begin
                if rest <= available:
                    return begin + rest
                rest -= available
        day += timedelta(days=1)
        cursor = datetime.combine(day, time.min, tz)
    return cursor  # pragma: no cover — doğrulanmış takvimde ulaşılmaz


def business_minutes_between(
    start: datetime, end: datetime, calendar: Calendar, tz: ZoneInfo
) -> int:
    """İki an arasındaki çalışma dakikası (takvim dışı sayılmaz)."""
    if end <= start:
        return 0
    total = timedelta()
    day = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    while day <= last:
        hours = calendar[day.weekday()]
        if not hours.closed:
            opens, closes = _window(day, hours, tz)
            begin, finish = max(start, opens), min(end, closes)
            if finish > begin:
                total += finish - begin
        day += timedelta(days=1)
    return int(total.total_seconds() // 60)


def is_open_at(moment: datetime, calendar: Calendar, tz: ZoneInfo) -> bool:
    local = moment.astimezone(tz)
    hours = calendar[local.weekday()]
    return not hours.closed and hours.open <= local.time() < hours.close


@dataclass(frozen=True, slots=True)
class SlaRule:
    """Talebe yazılan kural anlık görüntüsü."""

    hour_type: HourType
    target_hours: int
    calendar: Calendar | None  # 7/24'te None


def due_at(
    rule: SlaRule, start: datetime, tz: ZoneInfo, *, ratio: Decimal = Decimal(1)
) -> datetime:
    """SLA bitişi (`ratio=0.8` → "süre doluyor" anı)."""
    minutes = int((Decimal(rule.target_hours * 60) * ratio).to_integral_value(ROUND_HALF_UP))
    if rule.hour_type is HourType.ALWAYS or rule.calendar is None:
        return start + timedelta(minutes=minutes)
    return add_business_minutes(start, minutes, rule.calendar, tz)


@dataclass(frozen=True, slots=True)
class SlaStatus:
    due_at: datetime
    hour_type: HourType
    target_hours: int
    used_minutes: int
    remaining_minutes: int
    state: SlaState
    paused: bool


def sla_status(
    rule: SlaRule,
    *,
    created_at: datetime,
    due: datetime,
    finished_at: datetime | None,
    now: datetime,
    tz: ZoneInfo,
) -> SlaStatus:
    """`finished_at`: çözülme anı (çözüldü/kapandı) — sayaç orada durur."""
    end = finished_at or now
    if rule.hour_type is HourType.ALWAYS or rule.calendar is None:
        used = max(0, int((end - created_at).total_seconds() // 60))
        paused = False
    else:
        used = business_minutes_between(created_at, end, rule.calendar, tz)
        paused = finished_at is None and not is_open_at(now, rule.calendar, tz)
    total = rule.target_hours * 60
    if finished_at is not None:
        state = SlaState.MET if finished_at <= due else SlaState.MISSED
    elif used >= total:
        state = SlaState.BREACHED
    elif used >= WARNING_RATIO * total:
        state = SlaState.WARNING
    else:
        state = SlaState.OK
    return SlaStatus(
        due_at=due,
        hour_type=rule.hour_type,
        target_hours=rule.target_hours,
        used_minutes=used,
        remaining_minutes=max(0, total - used),
        state=state,
        paused=paused,
    )


# --- Ayar doğrulama -------------------------------------------------------------------


def check_hours(raw: object, prefix: str) -> dict[RequestPriority, int]:
    """Öncelik → saat (1–2160). Hata alanı `{prefix}.{öncelik}`."""
    errors: dict[str, str] = {}
    hours: dict[RequestPriority, int] = {}
    data = raw if isinstance(raw, dict) else {}
    for priority in RequestPriority:
        value = data.get(priority.value)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not (HOURS_MIN <= value <= HOURS_MAX)
        ):
            errors[f"{prefix}.{priority.value}"] = f"{HOURS_MIN}–{HOURS_MAX} saat."
        else:
            hours[priority] = value
    if errors:
        raise RuleError(errors)
    return hours


def check_range(value: object, field: str, low: int, high: int, unit: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
        raise RuleError({field: f"{low}–{high} {unit}."})
    return value


# --- Kapanış onayı --------------------------------------------------------------------


def check_feedback(solved: bool, score: int | None, comment: str | None) -> str | None:
    """Sakin değerlendirmesi: çözüldüyse 1–5 puan zorunlu; devam ediyorsa açıklama zorunlu."""
    text = " ".join(comment.split()) if comment else None
    if solved and (score is None or not SCORE_MIN <= score <= SCORE_MAX):
        raise RuleError({"score": "1–5 arası puan verin."})
    if not solved and (text is None or len(text) < 5):
        raise RuleError({"comment": "Sorunun neden devam ettiğini yazın."})
    return text or None


# --- Ücretli hizmet -------------------------------------------------------------------


class QuoteStatus(StrEnum):
    PENDING = "pending"  # sakin onayı bekliyor
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXTRA_PENDING = "extra_pending"  # gerçekleşen teklifi aştı: ek tutar onayı bekliyor
    BILLED = "billed"  # cariye yansıdı


OPEN_QUOTE_STATUSES = frozenset(
    {QuoteStatus.PENDING, QuoteStatus.ACCEPTED, QuoteStatus.EXTRA_PENDING}
)
QTY_MAX = Decimal("10000")
QUOTE_LINES_MAX = 30


@dataclass(frozen=True, slots=True)
class QuoteLine:
    description: str
    qty: Decimal
    unit_price: Decimal
    total: Decimal


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def quote_lines(raw: list[tuple[str, object, object]]) -> list[QuoteLine]:
    """`(açıklama, miktar, birim fiyat)` → kalemler. Boş açıklamalı satır yok sayılır."""
    rows = [(" ".join(d.split()), q, p) for d, q, p in raw if d and d.strip()]
    if not rows:
        raise RuleError({"lines": "En az bir kalem girin."})
    if len(rows) > QUOTE_LINES_MAX:
        raise RuleError({"lines": f"En fazla {QUOTE_LINES_MAX} kalem."})
    lines: list[QuoteLine] = []
    for description, qty_raw, price_raw in rows:
        qty, price = _decimal(qty_raw), _decimal(price_raw)
        if (
            qty is None
            or price is None
            or qty <= 0
            or price <= 0
            or qty > QTY_MAX
            or qty != qty.quantize(CENT)
            or price != price.quantize(CENT)
        ):
            raise RuleError({"lines": "Miktar ve birim fiyat sıfırdan büyük olmalı."})
        lines.append(QuoteLine(description[:200], qty, price, round_money(qty * price)))
    return lines


def quote_total(lines: list[QuoteLine]) -> Decimal:
    return sum((line.total for line in lines), ZERO)


def billed_amount(offer: Decimal, actual: Decimal, *, extra_accepted: bool | None) -> Decimal:
    """Faturalanacak tutar (HIZ-04 adım 5–7): gerçekleşen ≤ teklif → gerçekleşen; aşarsa sakin
    ek tutarı onaylarsa gerçekleşen, reddederse teklif."""
    if actual <= offer:
        return actual
    return actual if extra_accepted else offer


# --- Tadilat ve taşınma ---------------------------------------------------------------


class PermitType(StrEnum):
    RENOVATION = "renovation"
    MOVE_IN = "move_in"
    MOVE_OUT = "move_out"


class PermitStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


PERMIT_TYPE_LABELS = {
    PermitType.RENOVATION: "Tadilat",
    PermitType.MOVE_IN: "Taşınma (giriş)",
    PermitType.MOVE_OUT: "Taşınma (çıkış)",
}
_RANGE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_PERMIT_CHECKLIST = (
    "Ortak alanlar temiz bırakıldı",
    "Asansör ve koridorda hasar yok",
    "Moloz site dışına çıkarıldı",
    "Tesisat ve elektrik kapakları kapalı",
)
MOVE_OUT_CHECKLIST = (
    "Anahtar ve kartlar teslim alındı",
    "Asansör ve koridorda hasar yok",
    "Sayaçlar okundu",
)
PERMIT_RENOVATION_DAYS_MAX = 180


def check_hours_range(value: str | None, field: str, *, optional: bool) -> str | None:
    """ "09:00-18:00" (başlangıç < bitiş); `optional` → boş = o gün yasak."""
    text = (value or "").strip()
    if not text and optional:
        return None
    if not _RANGE.match(text) or text[:5] >= text[6:]:
        raise RuleError({field: "SS:DD-SS:DD biçiminde."})
    return text


def check_permit(
    kind: PermitType,
    date_from: date,
    date_to: date | None,
    description: str | None,
    today: date,
) -> tuple[date, str]:
    """Başvuru doğrulama → (bitiş tarihi, açıklama). Taşınma tek günlüktür."""
    errors: dict[str, str] = {}
    if date_from < today:
        errors["date_from"] = "Tarih bugünden önce olamaz."
    end = date_from
    if kind is PermitType.RENOVATION:
        if date_to is None or date_to < date_from:
            errors["date_to"] = "Bitiş tarihi başlangıçtan önce olamaz."
        elif (date_to - date_from).days > PERMIT_RENOVATION_DAYS_MAX:
            errors["date_to"] = f"Tadilat en fazla {PERMIT_RENOVATION_DAYS_MAX} gün sürebilir."
        else:
            end = date_to
    text = " ".join((description or "").split())
    if len(text) < 5:
        errors["description"] = (
            "Yapılacak işi kısaca yazın."
            if kind is PermitType.RENOVATION
            else "Kısa açıklama yazın (ör. nakliye firması, saat)."
        )
    if errors:
        raise RuleError(errors)
    return end, text
