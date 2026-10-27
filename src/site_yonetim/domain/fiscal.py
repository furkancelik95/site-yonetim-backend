"""Dönem kapanışı ve kilidi — saf kurallar (Yazılım Dokümanı FIN-13, YAP-02; servis isteği 27).

Durumlar: Açık → Kapanışta → Kilitli · Kilitli → (istisnai) Süreli açık → Kilitli.
- **Kapanışta ve kilitli dönemde hiçbir finansal kayıt eklenemez/değişemez** (D-11): tahakkuk,
  tahsilat, gider, kasa hareketi, iade, ters kayıt, açılış bakiyesi. Kapanıştaki döneme gelen
  yeni işlem sonraki açık döneme yönlendirilir (mesajda en erken açık dönem).
- Süreli açma süresi dolunca dönem kendiliğinden kilitlidir (okurken de, gece işinde de).
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import FinanceRuleError, PeriodStatus
from site_yonetim.domain.text import format_date_tr

MONTHS_TR = (
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
)
EXCEPTION_MIN = 10  # gerekçeli istisna en az 10 karakter
UNLOCK_REASON_MIN = 20  # süreli açma gerekçesi en az 20 karakter
UNLOCK_HOURS = (4, 24, 72)
NOTE_MAX = 500

WRITE_BLOCKING = frozenset({PeriodStatus.CLOSING, PeriodStatus.LOCKED})


def period_label(period: YearMonth) -> str:
    """period_label(YearMonth(2026, 9)) → "Eylül 2026" """
    return f"{MONTHS_TR[period.month - 1]} {period.year}"


def period_key(period: YearMonth) -> str:
    """ "2026-09" — API'deki dönem anahtarı."""
    return f"{period.year:04d}-{period.month:02d}"


def parse_period_key(value: str) -> YearMonth | None:
    """ "2026-09" → YearMonth; biçim ya da ay geçersizse None."""
    if len(value) != 7 or value[4] != "-" or not (value[:4] + value[5:]).isdigit():
        return None
    year, month = int(value[:4]), int(value[5:])
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return None
    return YearMonth(year, month)


def effective_status(status: str, unlock_until: datetime | None, now: datetime) -> PeriodStatus:
    """Süresi dolmuş süreli açma kilitli sayılır (iş henüz yeniden kilitlemediyse de)."""
    current = PeriodStatus(status)
    if current is PeriodStatus.TEMP_OPEN and (unlock_until is None or unlock_until <= now):
        return PeriodStatus.LOCKED
    return current


def earliest_open(period: YearMonth, blocked: set[YearMonth]) -> YearMonth:
    """Verilen dönemden sonraki ilk yazılabilir dönem (kilitli/kapanıştakiler atlanır)."""
    candidate = period.next()
    while candidate in blocked:
        candidate = candidate.next()
    return candidate


def locked_error(
    day: date, status: PeriodStatus, earliest: YearMonth, *, original: bool = False
) -> FinanceRuleError:
    """409 `period_locked`: kilitli/kapanıştaki döneme yazma. `original`: ters kaydı alınan
    kaydın dönemi kilitli (orijinal değişmez; düzeltme için kilidin süreli açılması gerekir)."""
    label = period_label(YearMonth.of(day))
    target = f"en erken {period_label(earliest)} ({format_date_tr(earliest.first_day)})"
    if original:
        message = (
            f"{label} dönemi kilitli; bu döneme ait kayıt ters kaydedilemez ya da değiştirilemez. "
            "Düzeltme için dönem kilidinin süreli açılmasını isteyin."
        )
    elif status is PeriodStatus.CLOSING:
        message = f"{label} dönemi kapanışta; yeni işlem sonraki açık döneme yazılmalı — {target}."
    else:
        message = f"Tarih kilitli döneme düşüyor ({label}); kayıt yazılamaz — {target}."
    return FinanceRuleError("period_locked", message, conflict=True)


class CloseItem(StrEnum):
    """Kapanış kontrol listesi (FIN-13 adım 2)."""

    CHARGE = "charge"
    CASH_DAYS = "cash_days"
    APPROVALS = "approvals"
    EXPENSES = "expenses"
    BANK = "bank"
    ACCOUNTING = "accounting"


CLOSE_ITEMS: dict[CloseItem, tuple[str, str, bool]] = {
    # madde → (etiket, ekrandaki bağlantı, zorunlu mu)
    CloseItem.CHARGE: ("Dönem tahakkuku kesinleşti", "tahakkuk", True),
    CloseItem.CASH_DAYS: ("Kasa günleri sayımla kapatıldı", "kasa", True),
    CloseItem.APPROVALS: ("Bekleyen finans onayı yok", "onaylar", True),
    CloseItem.EXPENSES: ("Dönem giderleri ödendi", "giderler", False),
    CloseItem.BANK: ("Eşleşmeyen banka hareketi yok", "banka-aktarim", False),
    CloseItem.ACCOUNTING: ("Muhasebe fişleri aktarıldı", "muhasebe-aktarimi", True),
}


@dataclass(frozen=True, slots=True)
class CheckResult:
    item: CloseItem
    ok: bool
    detail: str

    @property
    def label(self) -> str:
        return CLOSE_ITEMS[self.item][0]

    @property
    def required(self) -> bool:
        return CLOSE_ITEMS[self.item][2]


def clean_exceptions(raw: dict[str, str] | None) -> dict[str, str]:
    """Bilinen maddeler için boş olmayan, sadeleştirilmiş gerekçeler."""
    known = {i.value for i in CloseItem}
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        text = " ".join((value or "").split())[:NOTE_MAX]
        if key in known and text:
            out[key] = text
    return out


def missing_items(results: list[CheckResult], exceptions: dict[str, str]) -> list[CheckResult]:
    """Zorunlu olup sağlanmayan ve en az 10 karakterlik gerekçeli istisnası olmayan maddeler."""
    return [
        r
        for r in results
        if r.required and not r.ok and len(exceptions.get(r.item.value, "")) < EXCEPTION_MIN
    ]


def check_can_start_close(status: PeriodStatus, period: YearMonth, today: date) -> None:
    if status is not PeriodStatus.OPEN:
        raise FinanceRuleError(
            "invalid_status", "Yalnız açık dönem kapanışa alınır.", conflict=True
        )
    if period >= YearMonth.of(today):
        raise FinanceRuleError(
            "period_not_ended",
            "İçinde bulunulan ya da gelecek dönem kapatılamaz.",
            conflict=True,
        )


def check_unlock_request(reason: str | None, hours: int | None) -> tuple[str, int]:
    text = " ".join((reason or "").split())[:NOTE_MAX]
    if len(text) < UNLOCK_REASON_MIN:
        raise FinanceRuleError("validation", "Gerekçe en az 20 karakter olmalı.", field="reason")
    if hours not in UNLOCK_HOURS:
        raise FinanceRuleError("validation", "Süre 4, 24 ya da 72 saat olmalı.", field="hours")
    return text, hours
