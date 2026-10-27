"""Rezervasyon — saf kurallar (Ürün Dokümanı 8.10; Yazılım Dokümanı §14, REZ-01..03; servis
istekleri 59–61).

- Alan kuralları (REZ-01): açılış–kapanış, süre (30/60/90/120/180/240 dk), kapasite (aynı saatte
  kaç rezervasyon), kişi/misafir sınırı, bölüm başına haftalık sınır (0 = sınırsız), son iptal,
  ücret (ücretsiz · sabit · saat başı · misafir başı), kanal, onay, kullanım doğrulama, bekleme
  listesi ve no-show kuralı. **No-show yalnız doğrulama yöntemi "yok" değilse** açılır.
- Saatler açılıştan itibaren süre adımlarıyla dizilir; saat sonuna sığmayan son dilim yoktur.
- Haftalık sınır pazartesi başlangıçlı haftada bölüm × alan sayılır (bekleyen, onaylı, gelen,
  tamamlanan ve gelinmeyen).
- No-show kısıtı: son `months` ayda `count` gelinmeyen → son gelinmeyenin tarihi + `block_days`
  gününe kadar rezervasyon yapılamaz.
- K11 (PM — site kararı, varsayılan KAPALI): açıksa bölümün borcu eşik tutarı aşmış ve en eski
  açık borcun vadesi en az eşik gün geçmişse isteğe bağlı hizmet (rezervasyon) engellenir.

Saat okunmaz; `now`/`today` çağırandan gelir.
"""

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.service_desk import RuleError
from site_yonetim.domain.text import format_date_tr, format_money_tr


class FeeType(StrEnum):
    FREE = "free"
    FIXED = "fixed"  # rezervasyon başı
    HOURLY = "hourly"  # tutar × süre / 60
    PER_GUEST = "per_guest"  # tutar × misafir


class ChannelRule(StrEnum):
    RESIDENT = "resident"  # yalnız sakin açar
    STAFF = "staff"  # yalnız yönetim sakin adına açar
    BOTH = "both"


class ApprovalMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"  # yönetici onayı (onay kuralı "Rezervasyon")


class VerifyMethod(StrEnum):
    NONE = "none"
    QR = "qr"  # sakindeki QR (içerik `SY-REZ:{kod}`) görevliye okutulur
    CODE = "code"  # 6 haneli kod
    STAFF = "staff"  # görevli günlük listeden "geldi" işaretler


class BookingChannel(StrEnum):
    RESIDENT = "resident"
    STAFF = "staff"  # telefonla arayan sakin adına yönetim açtı


class ReservationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    CHECKED_IN = "checked_in"
    NO_SHOW = "no_show"
    COMPLETED = "completed"


class WaitlistStatus(StrEnum):
    WAITING = "waiting"
    OFFERED = "offered"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    LEFT = "left"


# Kapasiteyi tutan durumlar (veritabanındaki kısmi benzersiz indekslerle aynı küme)
LIVE = frozenset(
    {ReservationStatus.PENDING, ReservationStatus.APPROVED, ReservationStatus.CHECKED_IN}
)
LIVE_VALUES = tuple(sorted(s.value for s in LIVE))
WEEKLY_COUNTED = LIVE | {ReservationStatus.COMPLETED, ReservationStatus.NO_SHOW}
USED = frozenset(
    {
        ReservationStatus.APPROVED,
        ReservationStatus.CHECKED_IN,
        ReservationStatus.COMPLETED,
        ReservationStatus.NO_SHOW,
    }
)
OPEN_WAIT = frozenset({WaitlistStatus.WAITING, WaitlistStatus.OFFERED})

SLOT_CHOICES = (30, 60, 90, 120, 180, 240)
CAPACITY_MAX = 200
PEOPLE_MAX = 500
HOURS_MAX = 720  # son iptal: en fazla 30 gün önce
WAITLIST_MIN, WAITLIST_MAX = 5, 1440
NOSHOW_MONTHS_MAX, NOSHOW_COUNT_MAX, NOSHOW_DAYS_MAX = 24, 50, 365
CODE_LENGTH = 6
QR_PREFIX = "SY-REZ:"
REPORT_DAYS = 30
_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True, slots=True)
class NoShowRule:
    enabled: bool = False
    months: int = 3
    count: int = 2
    block_days: int = 14


@dataclass(frozen=True, slots=True)
class Rules:
    """Kuralı hiç kaydedilmemiş alan bu varsayılanlarla çalışır (servis isteği 59)."""

    enabled: bool = True
    open_from: time = time(9, 0)
    open_to: time = time(22, 0)
    slot_minutes: int = 60
    capacity: int = 1
    max_people: int = 10
    max_guests: int = 5
    weekly_limit: int = 2
    cancel_hours: int = 24
    late_cancel_refund: bool = False
    fee_type: FeeType = FeeType.FREE
    fee_amount: Decimal = ZERO
    channel: ChannelRule = ChannelRule.BOTH
    approval: ApprovalMode = ApprovalMode.AUTO
    verify: VerifyMethod = VerifyMethod.CODE
    waitlist: bool = True
    waitlist_minutes: int = 30
    noshow: NoShowRule = NoShowRule()


DEFAULT_RULES = Rules()


def hm(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def parse_hm(value: str | None) -> time | None:
    if value is None or not _HM.match(value):
        return None
    return time(int(value[:2]), int(value[3:]))


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def check_rules(raw: dict[str, Any]) -> Rules:
    """Formun tamamı (servis isteği 59) → kurallar; hatalar alan bazında (422)."""
    errors: dict[str, str] = {}
    open_from, open_to = parse_hm(raw.get("open_from")), parse_hm(raw.get("open_to"))
    slot = _int(raw.get("slot_minutes"))
    if open_from is None or open_to is None or open_to <= open_from:
        errors["open_to"] = "Kapanış açılıştan sonra olmalı (SS:DD)."
    elif slot not in SLOT_CHOICES or _minutes(open_to) - _minutes(open_from) < slot:
        errors["slot_minutes"] = "Süre 30/60/90/120/180/240 dakika olmalı ve açık saatlere sığmalı."

    def bounded(name: str, low: int, high: int, message: str) -> int:
        value = _int(raw.get(name))
        if value is None or not low <= value <= high:
            errors[name] = message
            return low
        return value

    capacity = bounded("capacity", 1, CAPACITY_MAX, f"Kapasite 1–{CAPACITY_MAX}.")
    max_people = bounded("max_people", 1, PEOPLE_MAX, f"Kişi sınırı 1–{PEOPLE_MAX}.")
    max_guests = bounded("max_guests", 0, PEOPLE_MAX, f"Misafir sınırı 0–{PEOPLE_MAX}.")
    weekly = bounded("weekly_limit", 0, 50, "Haftalık sınır 0 (sınırsız) ile 50 arasında.")
    cancel = bounded("cancel_hours", 0, HOURS_MAX, f"Son iptal 0–{HOURS_MAX} saat.")
    try:
        fee_type = FeeType(str(raw.get("fee_type")))
    except ValueError:
        errors["fee_type"] = "Ücret tipini seçin."
        fee_type = FeeType.FREE
    amount = ZERO
    if fee_type is not FeeType.FREE:
        try:
            amount = Decimal(str(raw.get("fee_amount") or "0"))
        except InvalidOperation:
            amount = ZERO
        if not amount.is_finite() or amount <= 0 or amount != round_money(amount):
            errors["fee_amount"] = "Ücreti girin (sıfırdan büyük, en çok iki ondalık)."
            amount = ZERO
    choices: dict[str, Any] = {}
    for name, kind, label in (
        ("channel", ChannelRule, "Kanalı seçin."),
        ("approval", ApprovalMode, "Onay tipini seçin."),
        ("verify", VerifyMethod, "Doğrulama yöntemini seçin."),
    ):
        try:
            choices[name] = kind(str(raw.get(name)))
        except ValueError:
            errors[name] = label
    waitlist = bool(raw.get("waitlist"))
    minutes = _int(raw.get("waitlist_minutes"))
    if waitlist and (minutes is None or not WAITLIST_MIN <= minutes <= WAITLIST_MAX):
        errors["waitlist_minutes"] = f"Teklif süresi {WAITLIST_MIN}–{WAITLIST_MAX} dakika."
    found = raw.get("noshow")
    ns_raw: dict[str, Any] = found if isinstance(found, dict) else {}
    noshow = NoShowRule(
        enabled=bool(ns_raw.get("enabled")),
        months=_int(ns_raw.get("months")) or 0,
        count=_int(ns_raw.get("count")) or 0,
        block_days=_int(ns_raw.get("block_days")) or 0,
    )
    if noshow.enabled:
        if choices.get("verify") is VerifyMethod.NONE:
            errors["noshow"] = "No-show kuralı için kullanım doğrulaması gerekir."
        elif not (
            1 <= noshow.months <= NOSHOW_MONTHS_MAX
            and 1 <= noshow.count <= NOSHOW_COUNT_MAX
            and 1 <= noshow.block_days <= NOSHOW_DAYS_MAX
        ):
            errors["noshow"] = "No-show ay, sayı ve kısıt günü en az 1 olmalı."
    if errors:
        raise RuleError(errors)
    return Rules(
        enabled=bool(raw.get("enabled")),
        open_from=open_from or DEFAULT_RULES.open_from,
        open_to=open_to or DEFAULT_RULES.open_to,
        slot_minutes=slot or DEFAULT_RULES.slot_minutes,
        capacity=capacity,
        max_people=max_people,
        max_guests=max_guests,
        weekly_limit=weekly,
        cancel_hours=cancel,
        late_cancel_refund=bool(raw.get("late_cancel_refund")),
        fee_type=fee_type,
        fee_amount=round_money(amount),
        channel=choices["channel"],
        approval=choices["approval"],
        verify=choices["verify"],
        waitlist=waitlist,
        waitlist_minutes=minutes if waitlist and minutes else DEFAULT_RULES.waitlist_minutes,
        noshow=noshow if noshow.enabled else replace(noshow, enabled=False),
    )


def slots(rules: Rules) -> list[tuple[time, time]]:
    out: list[tuple[time, time]] = []
    start, end = _minutes(rules.open_from), _minutes(rules.open_to)
    while start + rules.slot_minutes <= end:
        stop = start + rules.slot_minutes
        out.append((time(start // 60, start % 60), time(stop // 60, stop % 60)))
        start = stop
    return out


def slot_of(rules: Rules, start: time | None) -> tuple[time, time] | None:
    return next((s for s in slots(rules) if s[0] == start), None)


def fee_of(rules: Rules, guests: int) -> Decimal:
    if rules.fee_type is FeeType.FREE:
        return ZERO
    if rules.fee_type is FeeType.FIXED:
        return rules.fee_amount
    if rules.fee_type is FeeType.HOURLY:
        return round_money(rules.fee_amount * rules.slot_minutes / Decimal(60))
    return round_money(rules.fee_amount * guests)


def local(day: date, at: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, at, tzinfo=tz)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def add_months(day: date, months: int) -> date:
    """Ay ekle/çıkar; ayın son gününe kırpar (31.03 − 1 ay → 28/29.02)."""
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)  # pragma: no cover


def noshow_window_start(rules: Rules, today: date) -> date:
    return add_months(today, -rules.noshow.months)


def blocked_until(rules: Rules, no_show_dates: list[date], today: date) -> date | None:
    """Son `months` ayda `count` gelinmeyen → son tarihin + `block_days`'ine kadar kısıt."""
    if not rules.noshow.enabled or rules.verify is VerifyMethod.NONE:
        return None
    since = noshow_window_start(rules, today)
    recent = sorted(d for d in no_show_dates if d >= since)
    if len(recent) < rules.noshow.count:
        return None
    until = recent[-1] + timedelta(days=rules.noshow.block_days)
    return until if until >= today else None


def blocked_message(area_name: str, until: date) -> str:
    return (
        f"Gelinmeyen rezervasyonlar nedeniyle {format_date_tr(until)} tarihine kadar "
        f"{area_name} rezervasyonu yapamazsınız."
    )


@dataclass(frozen=True, slots=True)
class BookingInput:
    day: date | None
    start: time | None
    people: int | None
    guests: int | None


def check_booking(
    rules: Rules, data: BookingInput, *, now_local: datetime
) -> tuple[date, time, time, int, int]:
    """Tarih, saat, kişi ve misafir (422). `now_local` iş saat diliminde."""
    errors: dict[str, str] = {}
    slot = slot_of(rules, data.start)
    if data.day is None or data.day < now_local.date():
        errors["date"] = "Bugün ya da ileri bir tarih seçin."
    if slot is None:
        errors["start"] = "Saat seçin."
    elif data.day == now_local.date() and slot[0] <= now_local.time():
        errors["start"] = "Geçmiş saat seçilemez."
    people = data.people if data.people is not None else 1
    guests = data.guests if data.guests is not None else 0
    if not 1 <= people <= rules.max_people:
        errors["people"] = f"Kişi sayısı 1–{rules.max_people}."
    if not 0 <= guests <= rules.max_guests:
        errors["guests"] = f"Misafir en fazla {rules.max_guests}."
    if errors or slot is None or data.day is None:
        raise RuleError(errors)
    return data.day, slot[0], slot[1], people, guests


def is_late(starts_at: datetime, rules: Rules, now: datetime) -> bool:
    """Başlangıca `cancel_hours`'tan az kaldıysa geç iptal."""
    return starts_at - now < timedelta(hours=rules.cancel_hours)


def close_status(verify: VerifyMethod) -> ReservationStatus:
    """Bitmiş ve girişi kaydedilmemiş onaylı rezervasyon: doğrulama yoksa tamamlandı, varsa
    gelinmedi (REZ-03 adım 7–8)."""
    return ReservationStatus.COMPLETED if verify is VerifyMethod.NONE else ReservationStatus.NO_SHOW


def occupancy(used: int, slot_count: int, capacity: int, days: int) -> Decimal:
    """(onaylı + gelen + tamamlanan + gelinmeyen) / (saat × kapasite × gün) × 100, bir ondalık
    (yarım yukarı)."""
    room = slot_count * capacity * days
    if not room:
        return Decimal("0.0")
    return (Decimal(used * 100) / Decimal(room)).quantize(Decimal("0.1"), ROUND_HALF_UP)


def no_show_rate(verify: VerifyMethod, no_show: int, ended: int) -> int | None:
    if verify is VerifyMethod.NONE or ended == 0:
        return None
    return (no_show * 200 + ended) // (2 * ended)  # yüzde, yarım yukarı


def check_code(raw: str | None) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != CODE_LENGTH:
        raise RuleError({"code": "6 haneli kodu girin."})
    return digits


# --- K11: borçlu bölüme isteğe bağlı hizmet kısıtı ---------------------------------------


@dataclass(frozen=True, slots=True)
class DebtRule:
    enabled: bool = False
    min_amount: Decimal | None = None
    min_days: int | None = None


DEBT_DAYS_MAX = 3650


def check_debt_rule(enabled: bool, min_amount: Decimal | None, min_days: int | None) -> DebtRule:
    if not enabled:
        return DebtRule(False, min_amount, min_days)
    errors: dict[str, str] = {}
    if min_amount is None or min_amount <= 0:
        errors["debt_min_amount"] = "Borç eşiğini girin (sıfırdan büyük)."
    if min_days is None or not 1 <= min_days <= DEBT_DAYS_MAX:
        errors["debt_min_days"] = f"Gecikme günü 1–{DEBT_DAYS_MAX}."
    if errors:
        raise RuleError(errors)
    return DebtRule(True, round_money(min_amount) if min_amount else None, min_days)


def debt_blocked(rule: DebtRule, balance: Decimal, oldest_due: date | None, today: date) -> bool:
    if not rule.enabled or rule.min_amount is None or rule.min_days is None:
        return False
    if oldest_due is None or balance < rule.min_amount:
        return False
    return (today - oldest_due).days >= rule.min_days


def debt_message(rule: DebtRule, balance: Decimal) -> str:
    return (
        f"Bölümün {format_money_tr(balance)} tutarında, vadesi {rule.min_days} günden fazla geçmiş "
        "borcu olduğu için site yönetimi kararıyla rezervasyon yapılamıyor. Borç ödendiğinde "
        "rezervasyon yeniden açılır; ayrıntı için yönetimle görüşün."
    )
