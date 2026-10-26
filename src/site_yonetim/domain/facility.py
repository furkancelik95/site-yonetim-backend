"""Operasyon ekleri ve teknik varlık — saf kurallar.

Yazılım Dokümanı OPS-01..06 (servis istekleri 53–58), HIZ-06 (periyodik bakım), HIZ-07 (varlık
kartı ve yasal muayene takvimi). PM kararı **K28**: mali demirbaş (`assets`) ile teknik varlık
(`technical_assets`) ayrı listelerdir.

Tarih ve saat parametre olarak gelir (domain saati okumaz).
"""

import calendar
import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from site_yonetim.domain.text import ascii_fold


class OpsError(Exception):
    """İş kuralı ihlali. `status` 404/409/422; `code` makine içindir, mesaj Türkçedir."""

    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


def conflict(code: str, message: str) -> OpsError:
    return OpsError(409, code, message)


def clean(value: str | None, limit: int = 200) -> str:
    """Boşlukları tekleştirir ve kırpar."""
    return " ".join((value or "").split())[:limit]


# --- OPS-01 Anahtar ve emanet -----------------------------------------------------------


class KeyKind(StrEnum):
    KEY = "key"
    CARD = "card"
    REMOTE = "remote"


# Anahtar teslim edilebilecek roller (boş liste: herkes). "Görevli" site rolü değil, saha unvanı.
KEY_ROLES = ("Yönetici", "Teknik Personel", "Güvenlik", "Görevli", "Sakin")


class DepositStatus(StrEnum):
    REQUESTED = "requested"  # sakin kaydı açtı, anahtar henüz bırakılmadı
    HELD = "held"  # emanette
    HANDED = "handed"  # yetkili kişiye verildi
    RETURNED = "returned"  # sahibine iade
    EXPIRED = "expired"  # yalnız okumada: geçerlilik geçti (saklanmaz)


STORED_DEPOSIT_STATUSES = (
    DepositStatus.REQUESTED,
    DepositStatus.HELD,
    DepositStatus.HANDED,
    DepositStatus.RETURNED,
)


def deposit_status(stored: str, valid_until: date, today: date) -> DepositStatus:
    """Emanette ya da teslim beklenen kayıt geçerlilik bitince `expired` görünür."""
    status = DepositStatus(stored)
    if status in (DepositStatus.REQUESTED, DepositStatus.HELD) and valid_until < today:
        return DepositStatus.EXPIRED
    return status


def same_name(a: str, b: str) -> bool:
    """Harf duyarsız ad eşleşmesi; Türkçe harfler katlanır ("ISMAIL ISIK" = "İsmail Işık")."""
    return clean(ascii_fold(a)) == clean(ascii_fold(b))


# --- OPS-02 Stok sayımı -----------------------------------------------------------------


class StockCountStatus(StrEnum):
    APPLIED = "applied"  # fark hareketleri yazıldı (ya da fark yok)
    APPROVAL_PENDING = "approval_pending"  # "Stok sayım farkı" onayında
    REJECTED = "rejected"  # onay reddedildi — hareket yazılmadı
    WITHDRAWN = "withdrawn"  # talep geri çekildi
    FAILED = "failed"  # onaylandı ama yürütülemedi (ör. stok artık yetersiz)


STOCK_COUNT_NOTE = "Sayım farkı"


# --- OPS-03 Denetim listeleri -----------------------------------------------------------


class ChecklistItemType(StrEnum):
    YESNO = "yesno"
    NUMBER = "number"
    SCORE = "score"


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    text: str
    type: ChecklistItemType
    photo_required: bool = False
    min: Decimal | None = None
    max: Decimal | None = None


SCORE_PASS = 3  # puan maddesi 1–5; ≥ 3 uygun
CHECKLIST_ITEMS_MAX = 40


def _number(value: str) -> Decimal | None:
    try:
        number = Decimal(value.strip().replace(",", "."))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None


def answer_ok(item: ChecklistItem, value: str) -> bool | None:
    """Yanıt uygun mu; yanıt bu madde tipine uymuyorsa `None`.

    evet/hayır → "yes" uygun; sayı → min/maks aralığında; puan (1–5) → ≥ 3.
    """
    if item.type is ChecklistItemType.YESNO:
        return {"yes": True, "no": False}.get(value)
    number = _number(value)
    if number is None:
        return None
    if item.type is ChecklistItemType.SCORE:
        if number != number.to_integral_value() or not 1 <= number <= 5:
            return None
        return number >= SCORE_PASS
    return (item.min is None or number >= item.min) and (item.max is None or number <= item.max)


def inspection_score(results: list[bool]) -> int:
    """Uygun madde oranı (0–100, yuvarlanmış)."""
    if not results:
        return 0
    ratio = Decimal(sum(results) * 100) / Decimal(len(results))
    return int(ratio.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _yn(text: str, photo: bool = False) -> ChecklistItem:
    return ChecklistItem(text, ChecklistItemType.YESNO, photo)


# Hazır şablonlar (Yazılım Dokümanı OPS-03 "Hazır şablonlar").
DEFAULT_CHECKLISTS: tuple[tuple[str, tuple[ChecklistItem, ...]], ...] = (
    (
        "Temizlik kalitesi",
        (
            _yn("Giriş holü temiz"),
            _yn("Asansör kabini temiz"),
            _yn("Çöp odası düzenli", photo=True),
            ChecklistItem("Genel puan", ChecklistItemType.SCORE, False, Decimal(1), Decimal(5)),
        ),
    ),
    (
        "Havuz hijyeni",
        (
            ChecklistItem("Klor (ppm)", ChecklistItemType.NUMBER, False, Decimal(1), Decimal(3)),
            ChecklistItem("pH", ChecklistItemType.NUMBER, False, Decimal("7.2"), Decimal("7.8")),
            _yn("Su berrak"),
        ),
    ),
    (
        "Güvenlik turu",
        (
            _yn("Çevre aydınlatması çalışıyor"),
            _yn("Yangın kapıları kapalı ve önleri boş"),
            _yn("Kamera kayıtları alınıyor"),
            _yn("Kapı ve bariyerler çalışıyor"),
        ),
    ),
    (
        "Taşınma hasar kontrolü",
        (
            _yn("Asansör iç kaplaması sağlam", photo=True),
            _yn("Koridor duvarları sağlam"),
            _yn("Anahtar ve kartlar teslim alındı"),
        ),
    ),
    (
        "Sosyal tesis açılış kontrolü",
        (
            _yn("Salon temiz ve havalandırılmış"),
            _yn("Acil çıkışlar açık"),
            _yn("Klima ve aydınlatma çalışıyor"),
            ChecklistItem("Genel puan", ChecklistItemType.SCORE, False, Decimal(1), Decimal(5)),
        ),
    ),
)


# --- OPS-04 Sigorta -----------------------------------------------------------------------


class PolicyKind(StrEnum):
    BUILDING = "building"  # bina (deprem/yangın)
    LIABILITY = "liability"  # üçüncü şahıs sorumluluk
    ELEVATOR = "elevator"
    PERSONNEL = "personnel"  # personel kaza
    GLASS = "glass"
    OTHER = "other"


class ClaimStatus(StrEnum):
    OPEN = "open"
    SUBMITTED = "submitted"  # sigortacıya iletildi
    PAID = "paid"  # tazminat ödendi
    REJECTED = "rejected"


CLAIM_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.OPEN: frozenset({ClaimStatus.SUBMITTED, ClaimStatus.REJECTED}),
    ClaimStatus.SUBMITTED: frozenset({ClaimStatus.PAID, ClaimStatus.REJECTED, ClaimStatus.OPEN}),
    ClaimStatus.PAID: frozenset(),
    ClaimStatus.REJECTED: frozenset(),
}


class PolicyWarning(StrEnum):
    DAYS_60 = "60"
    DAYS_30 = "30"
    EXPIRED = "expired"


POLICY_EXPIRED_NOTICE_DAYS = 30  # bitişten sonra bu kadar gün "süresi doldu" bildirimi


def policy_warning(days_left: int) -> PolicyWarning | None:
    """Bitişe 60 ve 30 gün kala uyarı (OPS-04 adım 2); geçmişse `expired`."""
    if days_left < 0:
        return PolicyWarning.EXPIRED
    if days_left <= 30:
        return PolicyWarning.DAYS_30
    if days_left <= 60:
        return PolicyWarning.DAYS_60
    return None


# --- OPS-05 Sayaç -------------------------------------------------------------------------


class MeterKind(StrEnum):
    COLD_WATER = "cold_water"
    HOT_WATER = "hot_water"
    HEAT = "heat"  # ısı pay ölçer
    ELECTRIC = "electric"
    GAS = "gas"
    COMMON = "common"  # ortak alan sayacı


class MeterStatus(StrEnum):
    ACTIVE = "active"
    FAULTY = "faulty"
    REPLACED = "replaced"  # değişti — yeni sayaç `replaced_by`


class ReadingStatus(StrEnum):
    OK = "ok"
    ANOMALY = "anomaly"  # sapma eşiği aşıldı — onay ya da yeniden okuma bekler
    APPROVED = "approved"  # anormal okuma yönetici onayıyla geçerli
    UNREADABLE = "unreadable"
    REREAD = "reread"  # geçersiz: yeniden okunacak (eski kayıt)


UNREADABLE_REASONS = ("Evde yok", "Sayaç erişilemez", "Arızalı")
DEFAULT_UNIT_LABEL = {MeterKind.ELECTRIC: "kWh", MeterKind.HEAT: "birim"}
ANOMALY_PERCENT_DEFAULT = 50  # ±%X (Ürün Dokümanı 8.9, varsayılan %50) — site ayarı
_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
INDEX_PLACES = Decimal("0.001")


def valid_period(value: str | None) -> bool:
    return bool(value and _PERIOD.match(value))


def unit_label_for(kind: MeterKind) -> str:
    return DEFAULT_UNIT_LABEL.get(kind, "m³")


def consumption(value: Decimal, previous: Decimal, multiplier: Decimal) -> Decimal:
    """Tüketim = (endeks − önceki endeks) × çarpan; 3 ondalık."""
    return ((value - previous) * multiplier).quantize(INDEX_PLACES, rounding=ROUND_HALF_UP)


def deviation_percent(current: Decimal, previous: Decimal | None) -> int | None:
    """Önceki döneme göre tüketim değişimi (%). Önceki tüketim yoksa ya da sıfırsa `None`."""
    if previous is None or previous <= 0:
        return None
    ratio = (current - previous) * 100 / previous
    return int(ratio.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def is_anomaly(percent: int | None, threshold: int) -> bool:
    return percent is not None and abs(percent) > threshold


# --- OPS-06 Rutin görevler ----------------------------------------------------------------


class TaskFrequency(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"  # haftanın seçili günleri (0 = pazartesi)
    MONTHLY = "monthly"  # ayın günü (1–28)


class TaskStatus(StrEnum):
    UPCOMING = "upcoming"
    DUE = "due"  # bugün, saat aralığı içinde
    LATE = "late"
    DONE = "done"
    FAILED = "failed"


class CompletionStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"


MONTH_DAY_MAX = 28
TASK_FAIL_REASONS = ("Malzeme yok", "Alana erişilemedi", "Başka acil iş", "Hava koşulları")


def task_applies(
    frequency: TaskFrequency, weekdays: list[int], month_day: int | None, day: date
) -> bool:
    if frequency is TaskFrequency.DAILY:
        return True
    if frequency is TaskFrequency.WEEKDAYS:
        return day.weekday() in weekdays
    return month_day == day.day


def task_status(
    completion: CompletionStatus | None,
    *,
    day: date,
    today: date,
    now: time,
    time_from: time,
    time_to: time,
) -> TaskStatus:
    """Kapatılmışsa kapanış durumu; günü geçmiş ya da bugün saat aralığı bitmişse `late`."""
    if completion is not None:
        return TaskStatus(completion.value)
    if day < today or (day == today and now > time_to):
        return TaskStatus.LATE
    if day == today and now >= time_from:
        return TaskStatus.DUE
    return TaskStatus.UPCOMING


def task_rate(expected: int, done: int) -> int | None:
    if expected == 0:
        return None
    return int((Decimal(done * 100) / Decimal(expected)).quantize(Decimal(1), ROUND_HALF_UP))


# --- HIZ-07 Teknik varlık ve yasal muayene -----------------------------------------------


class TechnicalAssetCategory(StrEnum):
    ELEVATOR = "elevator"
    GENERATOR = "generator"
    HYDROPHORE = "hydrophore"
    FIRE_SYSTEM = "fire_system"
    CAMERA = "camera"
    BARRIER = "barrier"
    ELECTRICAL_PANEL = "electrical_panel"
    POOL_EQUIPMENT = "pool_equipment"
    HVAC = "hvac"
    BOILER = "boiler"
    OTHER = "other"


class TechnicalAssetStatus(StrEnum):
    WORKING = "working"  # çalışıyor
    FAULTY = "faulty"  # arızalı
    OUT_OF_SERVICE = "out_of_service"  # devre dışı
    NONCONFORMING = "nonconforming"  # son muayene olumsuz (HIZ-07 adım 8)


class InspectionResult(StrEnum):
    PASS = "pass"  # olumlu  # noqa: S105  # nosec B105
    FAIL = "fail"  # olumsuz → düzeltme iş emri, varlık uygunsuz


@dataclass(frozen=True, slots=True)
class InspectionTemplate:
    key: str
    name: str
    categories: tuple[TechnicalAssetCategory, ...]
    period_months: int


C = TechnicalAssetCategory
# Ürün Dokümanı 8.8 / Yazılım Dokümanı 12.4 hazır şablonları. Periyot önerisidir; site değiştirir
# (yasal süre mevzuata ve ekipmana göre değişebilir — kural uydurulmaz, alan düzenlenebilir).
INSPECTION_TEMPLATES: tuple[InspectionTemplate, ...] = (
    InspectionTemplate("elevator_periodic", "Asansör periyodik kontrolü", (C.ELEVATOR,), 12),
    InspectionTemplate("fire_extinguisher", "Yangın tüpü dolum ve kontrol", (C.FIRE_SYSTEM,), 12),
    InspectionTemplate(
        "pressure_vessel", "Basınçlı kap kontrolü", (C.HYDROPHORE, C.BOILER, C.HVAC), 12
    ),
    InspectionTemplate(
        "electrical_grounding",
        "Elektrik tesisatı ve topraklama ölçümü",
        (C.ELECTRICAL_PANEL, C.GENERATOR, C.POOL_EQUIPMENT),
        12,
    ),
    InspectionTemplate("lightning_rod", "Paratoner kontrolü", (C.ELECTRICAL_PANEL, C.OTHER), 12),
    InspectionTemplate("generator_load_test", "Jeneratör yük testi", (C.GENERATOR,), 12),
)
TEMPLATE_BY_KEY = {t.key: t for t in INSPECTION_TEMPLATES}
PERIOD_MONTHS_MAX = 120


def suggested_inspections(category: TechnicalAssetCategory) -> list[InspectionTemplate]:
    return [t for t in INSPECTION_TEMPLATES if category in t.categories]


def add_months(day: date, months: int) -> date:
    """Ay ekler; ayın son gününü aşarsa son güne çeker (31.01 + 1 ay → 28/29.02)."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class DueWarning(StrEnum):
    DAYS_60 = "60"
    DAYS_30 = "30"
    DAYS_7 = "7"
    OVERDUE = "overdue"


def due_warning(days_left: int) -> DueWarning | None:
    """Yasal muayene: 60/30/7 gün kala uyarı (HIZ-07 adım 5); geçmişse `overdue`."""
    if days_left < 0:
        return DueWarning.OVERDUE
    for limit, warning in ((7, DueWarning.DAYS_7), (30, DueWarning.DAYS_30)):
        if days_left <= limit:
            return warning
    return DueWarning.DAYS_60 if days_left <= 60 else None


def technical_asset_code(sequence: int) -> str:
    """technical_asset_code(1) → "TV-0001" (demirbaş kodu DB-… ile karışmaz)."""
    return f"TV-{sequence:04d}"


# --- HIZ-06 Periyodik bakım ---------------------------------------------------------------


class PeriodUnit(StrEnum):
    WEEK = "week"
    MONTH = "month"


class MaintenanceRunStatus(StrEnum):
    OPEN = "open"  # iş emri açıldı, bakım bekleniyor
    DONE = "done"
    SKIPPED = "skipped"  # bu dönem yapılmadı (gerekçeli)


def advance(day: date, unit: PeriodUnit, count: int) -> date:
    if unit is PeriodUnit.WEEK:
        return day + timedelta(weeks=count)
    return add_months(day, count)


def next_maintenance(due_on: date, done_on: date, unit: PeriodUnit, count: int) -> date:
    """Sonraki tarih plan takviminden yürür; bakım gecikmiş yapıldıysa bitişten sonraya kayar."""
    following = advance(due_on, unit, count)
    while following <= done_on:
        following = advance(following, unit, count)
    return following


def request_category_for(category: TechnicalAssetCategory) -> str:
    """Varlık kategorisinden talep kategorisi (talep modülünün sabit listesi)."""
    return {
        C.ELEVATOR: "elevator",
        C.ELECTRICAL_PANEL: "electrical",
        C.GENERATOR: "electrical",
        C.BOILER: "heating",
        C.HVAC: "heating",
        C.HYDROPHORE: "plumbing",
        C.FIRE_SYSTEM: "security",
        C.CAMERA: "security",
        C.BARRIER: "security",
        C.POOL_EQUIPMENT: "common_area",
    }.get(category, "other")
