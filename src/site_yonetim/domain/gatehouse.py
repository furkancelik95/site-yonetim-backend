"""Kapı işlerinin genişlemesi — saf kurallar (servis istekleri 40–45; Yazılım Dokümanı GUV-02..08,
OTP-02). Kargo/ziyaretçi temel kuralları `domain/security.py`'de.

- Ayarlar: habersiz ziyaretçi yöntemi (bilgi / sakin onayı), kapı istisnası, kargo teslim yöntemi,
  ziyaretçi alanları, saklama süresi (üst sınır 24 ay; K8 varsayılanı 6 ay), misafir parkı.
- Ön davet: 6 haneli kod (tahmin edilemez; yalnız özeti saklanır), tek kullanımlık / tarih
  aralıklı / süreli kalıcı (gün ve saat kuralıyla).
- Habersiz ziyaretçi: süre içinde yalnız sakin, süre dolunca yalnız görevli (gerekçeli) karar verir.
- Plaka: büyük harf, boşluksuz; arama büyük/küçük harf ve boşluk duyarsız.
- Nöbet defteri: kayıt değişmez; düzeltme ek notla.
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import StrEnum

from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import tr_lower

# --- Kapılar ve güvenlik ayarları (servis isteği 40, GUV-08) ---------------------------------


class GateKind(StrEnum):
    VEHICLE = "vehicle"
    PEDESTRIAN = "pedestrian"
    SERVICE = "service"


class UnannouncedMethod(StrEnum):
    INFO = "info"  # görevli kaydeder, ziyaretçi girer, sakine bilgi
    APPROVAL = "approval"  # sakin onayı; süre dolunca görevli gerekçeli karar verir


class ParcelMethod(StrEnum):
    CODE = "code"
    QR = "qr"
    BOTH = "both"


RETENTION_MAX_MONTHS = 24  # platform üst sınırı (GUV-08)
APPROVAL_TIMEOUT_RANGE = (30, 600)
LONG_WAIT_RANGE = (1, 60)
GUEST_PARKING_RANGE = (0, 500)
GATE_NAME_MAX = 40


@dataclass(frozen=True, slots=True)
class VisitorFields:
    """Ziyaretçiden alınacak isteğe bağlı alanlar. Ad soyad ve bölüm her zaman; tam T.C. kimlik
    no hiçbir seçenekte yok — "kimlik görüldü" yalnız işarettir. Kapalı alan kaydedilmez."""

    plate: bool = True
    phone: bool = False
    id_seen: bool = False


@dataclass(frozen=True, slots=True)
class SecuritySettingsData:
    """Varsayılanlar Yazılım Dokümanı §10.2 ve GUV-08; saklama K8 (dokümandaki 6 ay)."""

    unannounced_method: UnannouncedMethod = UnannouncedMethod.INFO
    approval_timeout_seconds: int = 90
    gate_methods: dict[str, UnannouncedMethod] = field(default_factory=dict)
    parcel_method: ParcelMethod = ParcelMethod.BOTH
    visitor_fields: VisitorFields = field(default_factory=VisitorFields)
    retention_months: int = 6
    # Süre dolan kayıtların gece anonimleştirilmesi: hukuk onayına kadar KAPALI (K8).
    auto_anonymize: bool = False
    parcel_long_wait_days: int = 5
    guest_parking_capacity: int = 10

    def method_for(self, gate_id: str | None) -> UnannouncedMethod:
        """Kapı istisnası site ayarını ezer."""
        if gate_id and gate_id in self.gate_methods:
            return self.gate_methods[gate_id]
        return self.unannounced_method


def _int_in(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def check_settings(
    *,
    unannounced_method: str | None,
    approval_timeout_seconds: object,
    gate_methods: dict[str, str] | None,
    parcel_method: str | None,
    visitor_fields: VisitorFields,
    retention_months: object,
    auto_anonymize: bool,
    parcel_long_wait_days: object,
    guest_parking_capacity: object,
    gate_ids: set[str],
) -> SecuritySettingsData:
    """Ayar formunu doğrular. Saklama üst sınırı ayrı kodla (`retention_limit`) reddedilir."""
    errors: dict[str, str] = {}
    if unannounced_method not in set(UnannouncedMethod):
        errors["unannounced_method"] = "Habersiz ziyaretçi yöntemini seçin."
    if not _int_in(approval_timeout_seconds, *APPROVAL_TIMEOUT_RANGE):
        errors["approval_timeout_seconds"] = "Bekleme süresi 30–600 saniye olmalı."
    methods: dict[str, UnannouncedMethod] = {}
    for gate_id, method in (gate_methods or {}).items():
        if gate_id not in gate_ids or method not in set(UnannouncedMethod):
            errors["gate_methods"] = "Kapı istisnasında tanımsız kapı ya da yöntem var."
            break
        methods[gate_id] = UnannouncedMethod(method)
    if parcel_method not in set(ParcelMethod):
        errors["parcel_method"] = "Kargo teslim yöntemini seçin."
    if not _int_in(parcel_long_wait_days, *LONG_WAIT_RANGE):
        errors["parcel_long_wait_days"] = "Uzun bekleme 1–60 gün olmalı."
    if not _int_in(guest_parking_capacity, *GUEST_PARKING_RANGE):
        errors["guest_parking_capacity"] = "Misafir parkı kapasitesi 0–500 olmalı."
    if not _int_in(retention_months, 1, 10_000):
        errors["retention_months"] = "Saklama süresi en az 1 ay olmalı."
    if errors:
        raise FormFieldError(errors)
    if int(str(retention_months)) > RETENTION_MAX_MONTHS:
        raise OperationRuleError(
            "retention_limit",
            "Saklama süresi en fazla 2 yıl (24 ay) olabilir.",
            field="retention_months",
        )
    return SecuritySettingsData(
        unannounced_method=UnannouncedMethod(str(unannounced_method)),
        approval_timeout_seconds=int(str(approval_timeout_seconds)),
        gate_methods=methods,
        parcel_method=ParcelMethod(str(parcel_method)),
        visitor_fields=visitor_fields,
        retention_months=int(str(retention_months)),
        auto_anonymize=auto_anonymize,
        parcel_long_wait_days=int(str(parcel_long_wait_days)),
        guest_parking_capacity=int(str(guest_parking_capacity)),
    )


def check_gate(name: str | None, kind: str | None, taken: set[str]) -> tuple[str, GateKind]:
    """`taken`: diğer kapıların `tr_lower` adları."""
    errors: dict[str, str] = {}
    text = " ".join((name or "").split())
    if not text:
        errors["name"] = "Kapı adını yazın."
    elif len(text) > GATE_NAME_MAX:
        errors["name"] = f"Kapı adı en fazla {GATE_NAME_MAX} karakter olabilir."
    elif tr_lower(text) in taken:
        errors["name"] = "Bu adla kapı var."
    if kind not in set(GateKind):
        errors["kind"] = "Kapı tipini seçin."
    if errors:
        raise FormFieldError(errors)
    return text, GateKind(str(kind))


def months_before(day: date, months: int) -> date:
    """Saklama sınırı: `day`'den `months` ay önce (ay sonu taşarsa ayın son günü)."""
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    raise AssertionError("ulaşılamaz")  # pragma: no cover


# --- Plaka (servis isteği 43, GUV-04) -----------------------------------------------------

PLATE_RE = re.compile(r"^(0[1-9]|[1-7]\d|8[01])[A-Z]{1,3}\d{2,5}$")
PLATE_FORMAT_MESSAGE = "Plakayı 34 ABC 123 biçiminde yazın."
PLATE_MIN_LENGTH = 4
_PLATE_FOLD = str.maketrans({"i": "I", "ı": "I", "İ": "I"})
_PLATE_PARTS = re.compile(r"^(\d{2})([A-Z]+)(\d+)$")


def normalize_plate(value: str | None) -> str:
    """Büyük harf, boşluksuz, yalnız harf ve rakam: `34 abc 123` → `34ABC123`."""
    upper = (value or "").translate(_PLATE_FOLD).upper()
    return "".join(c for c in upper if c.isascii() and c.isalnum())[:15]


def is_plate(value: str) -> bool:
    return PLATE_RE.match(value) is not None


def display_plate(plate: str) -> str:
    """`34ABC123` → `34 ABC 123`; Türk plakası biçiminde değilse olduğu gibi."""
    return _PLATE_PARTS.sub(r"\1 \2 \3", plate) if is_plate(plate) else plate


class PlateResult(StrEnum):
    REGISTERED = "registered"  # kayıtlı araç (bölüm, sahip adı)
    GUEST = "guest"  # geçerli ön davet ya da içerideki ziyaretçi
    UNKNOWN = "unknown"


class GateDirection(StrEnum):
    IN = "in"
    OUT = "out"


class ViolationNotice(StrEnum):
    RESIDENT = "resident"  # plaka bir bölüme bağlı → o bölümün sakinlerine
    MANAGEMENT = "management"  # kayıtsız plaka → yönetime


LOCATION_MIN, LOCATION_MAX = 2, 120
VIOLATION_NOTE_MAX = 300


def check_violation(
    plate: str | None, location: str | None, note: str | None
) -> tuple[str, str, str | None]:
    errors: dict[str, str] = {}
    clean = normalize_plate(plate)
    if not is_plate(clean):
        errors["plate"] = PLATE_FORMAT_MESSAGE
    where = " ".join((location or "").split())
    if not LOCATION_MIN <= len(where) <= LOCATION_MAX:
        errors["location"] = "Yeri yazın (ör. B blok önü, engelli park yeri)."
    text = " ".join((note or "").split()) or None
    if text is not None and len(text) > VIOLATION_NOTE_MAX:
        errors["note"] = f"Not en fazla {VIOLATION_NOTE_MAX} karakter olabilir."
    if errors:
        raise FormFieldError(errors)
    return clean, where, text


# --- Ön davet (servis isteği 41, GUV-02) --------------------------------------------------


class InviteType(StrEnum):
    ONCE = "once"  # tek kullanımlık
    RANGE = "range"  # tarih aralıklı
    RECURRING = "recurring"  # süreli kalıcı: gün ve saat kuralıyla (bakıcı hafta içi 08–18)


class InviteStatus(StrEnum):
    ACTIVE = "active"
    USED = "used"
    CANCELLED = "cancelled"


INVITE_CODE_DIGITS = 6
INVITE_MAX_DAYS = 366
INVITE_NAME_RANGE = (3, 60)
INVITE_NOTE_MAX = 200
INVITE_CODE_RE = re.compile(r"^\d{6}$")


@dataclass(frozen=True, slots=True)
class InviteWindow:
    type: InviteType
    date_from: date
    date_to: date
    weekdays: tuple[int, ...] = ()  # 0 = pazartesi
    time_from: time | None = None
    time_to: time | None = None

    def valid_on(self, day: date) -> bool:
        if not self.date_from <= day <= self.date_to:
            return False
        return self.type is not InviteType.RECURRING or day.weekday() in self.weekdays

    def valid_at(self, day: date, at: time) -> bool:
        if not self.valid_on(day):
            return False
        if self.time_from is not None and self.time_to is not None:
            return self.time_from <= at <= self.time_to
        return True


def _letters_name(text: str) -> bool:
    return all(c in " .'-" or unicodedata.category(c).startswith("L") for c in text)


def check_person_name(value: str | None, field_name: str, message: str, *, low: int = 3) -> str:
    """Ziyaretçi / teslim yetkilisi adı: 3–60 (kapıda 2), harf, boşluk, nokta, kesme ve tire."""
    text = " ".join((value or "").split())
    if not low <= len(text) <= INVITE_NAME_RANGE[1] or not _letters_name(text):
        raise FormFieldError({field_name: message})
    return text


@dataclass(frozen=True, slots=True)
class InviteForm:
    visitor_name: str
    window: InviteWindow
    plate: str | None
    note: str | None


def check_invite(
    *,
    visitor_name: str | None,
    type_: str | None,
    date_from: date | None,
    date_to: date | None,
    weekdays: list[int] | None,
    time_from: time | None,
    time_to: time | None,
    plate: str | None,
    note: str | None,
    today: date,
) -> InviteForm:
    errors: dict[str, str] = {}
    name = " ".join((visitor_name or "").split())
    low, high = INVITE_NAME_RANGE
    if not low <= len(name) <= high or not _letters_name(name):
        errors["visitor_name"] = "Ziyaretçinin adını yazın (yalnız harf, en az 3 karakter)."
    kind = InviteType(type_) if type_ in set(InviteType) else None
    if kind is None:
        errors["type"] = "Davet tipini seçin."
    if date_from is None or date_from < today:
        errors["date_from"] = "Tarih bugünden önce olamaz."
    end = date_from if kind is InviteType.ONCE else date_to
    if kind is not InviteType.ONCE and date_from is not None:
        if end is None or end < date_from:
            errors["date_to"] = "Bitiş tarihini başlangıçtan sonra seçin."
        elif (end - date_from).days > INVITE_MAX_DAYS:
            errors["date_to"] = "Davet en fazla 1 yıl geçerli olabilir."
    days = tuple(sorted(set(weekdays or ())))
    if kind is InviteType.RECURRING and (not days or any(not 0 <= d <= 6 for d in days)):
        errors["weekdays"] = "En az bir gün seçin."
    if (time_from is None) != (time_to is None) or (
        time_from is not None and time_to is not None and time_to <= time_from
    ):
        errors["time_to"] = "Saat aralığını kontrol edin (bitiş başlangıçtan sonra)."
    clean_plate = normalize_plate(plate) or None
    if clean_plate is not None and not is_plate(clean_plate):
        errors["plate"] = PLATE_FORMAT_MESSAGE
    text = " ".join((note or "").split()) or None
    if text is not None and len(text) > INVITE_NOTE_MAX:
        errors["note"] = f"Not en fazla {INVITE_NOTE_MAX} karakter olabilir."
    if errors or kind is None or date_from is None or end is None:
        raise FormFieldError(errors)
    return InviteForm(
        visitor_name=name,
        window=InviteWindow(
            kind,
            date_from,
            end,
            days if kind is InviteType.RECURRING else (),
            time_from,
            time_to,
        ),
        plate=clean_plate,
        note=text,
    )


def check_check_in(status: InviteStatus, window: InviteWindow, day: date, at: time) -> None:
    """Kodla giriş: kullanılmış ya da geçerlilik dışı → 409 `invite_not_valid`."""
    if status is InviteStatus.USED:
        raise OperationRuleError(
            "invite_not_valid", "Bu tek kullanımlık davet kullanılmış.", conflict=True
        )
    if status is not InviteStatus.ACTIVE or not window.valid_at(day, at):
        raise OperationRuleError(
            "invite_not_valid",
            "Davet şu an geçerli değil (tarih, gün ya da saat dışında). "
            "Habersiz ziyaretçi olarak kaydedin.",
            conflict=True,
        )


# --- Habersiz ziyaretçi ve sakin onayı (servis isteği 42, GUV-03) ------------------------


class VisitRequestStatus(StrEnum):
    AWAITING = "awaiting"
    ALLOWED = "allowed"
    DENIED = "denied"


class DecisionSource(StrEnum):
    RESIDENT = "resident"
    GUARD = "guard"


class VisitDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


REASON_MIN, REASON_MAX = 5, 300


def seconds_left(expires_at: datetime, now: datetime) -> int:
    return max(0, math.ceil((expires_at - now).total_seconds()))


def _decision(value: str | None) -> VisitDecision:
    if value not in set(VisitDecision):
        raise FormFieldError({"decision": "Karar seçin (içeri al ya da reddet)."})
    return VisitDecision(str(value))


def check_guard_decision(
    status: VisitRequestStatus,
    expires_at: datetime,
    now: datetime,
    decision: str | None,
    reason: str | None,
) -> tuple[VisitDecision, str]:
    """Görevli yalnız süre dolunca ve gerekçeyle karar verir."""
    if status is not VisitRequestStatus.AWAITING:
        raise OperationRuleError(
            "already_decided", "Bu ziyaretçi için karar verilmiş.", conflict=True
        )
    if now < expires_at:
        raise OperationRuleError(
            "awaiting_resident",
            "Sakin yanıtı bekleniyor; süre dolunca karar verebilirsiniz.",
            conflict=True,
        )
    choice = _decision(decision)
    text = " ".join((reason or "").split())
    if not REASON_MIN <= len(text) <= REASON_MAX:
        raise FormFieldError(
            {"reason": "Kararınızın gerekçesini yazın (ör. diafonla ulaşıldı, sakin onayladı)."}
        )
    return choice, text


def check_resident_decision(
    status: VisitRequestStatus, expires_at: datetime, now: datetime, decision: str | None
) -> VisitDecision:
    if status is not VisitRequestStatus.AWAITING:
        raise OperationRuleError(
            "already_decided", "Bu ziyaretçi için karar verilmiş.", conflict=True
        )
    if now >= expires_at:
        raise OperationRuleError(
            "timed_out",
            "Süre doldu; görevli sizinle diafon/telefonla iletişime geçecek.",
            conflict=True,
        )
    return _decision(decision)


# --- Kargo teslim yetkilisi (servis isteği 44, GUV-05) ------------------------------------

DELEGATE_LIMIT = 3


# --- Nöbet defteri (servis isteği 45, GUV-06) ----------------------------------------------


class ShiftLogKind(StrEnum):
    NOTE = "note"
    EVENT = "event"
    HANDOVER = "handover"


SHIFT_TEXT_MIN, SHIFT_TEXT_MAX = 5, 1000
ADDENDUM_MIN, ADDENDUM_MAX = 3, 500


def check_shift_entry(kind: str | None, text: str | None) -> tuple[ShiftLogKind, str]:
    errors: dict[str, str] = {}
    if kind not in set(ShiftLogKind):
        errors["kind"] = "Kayıt türünü seçin."
    clean = (text or "").strip()
    if len(clean) < SHIFT_TEXT_MIN:
        errors["text"] = "En az 5 karakter yazın."
    elif len(clean) > SHIFT_TEXT_MAX:
        errors["text"] = f"En fazla {SHIFT_TEXT_MAX} karakter yazın."
    if errors:
        raise FormFieldError(errors)
    return ShiftLogKind(str(kind)), clean


def check_addendum(text: str | None) -> str:
    clean = (text or "").strip()
    if not ADDENDUM_MIN <= len(clean) <= ADDENDUM_MAX:
        raise FormFieldError({"text": "Ek notu yazın (en az 3 karakter)."})
    return clean
