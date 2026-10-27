"""Dış kanal gönderim kuralları — saf (Yazılım Dokümanı PLT-05, ILT-05, §16.2, §4.8).

- Kanal seçimi: kişinin kategori × kanal tercihi (kapatılamayan hücreler zaten açık kalır,
  `domain/notifications.py`); kanal için adres yoksa (push jetonu, telefon, e-posta) o kanal
  atlanır.
- Sessiz saatler: site ayarı, varsayılan 22:00–08:00 (gece yarısını geçebilir). Acil kategori
  aşar; diğerleri sessiz saat bitişine ertelenir.
- Kritik bildirim (acil): push 2 dakika içinde teslim edilemezse SMS yedeği; push hiç
  gönderilemiyorsa (jeton yok) SMS hemen.
- SMS adedi: GSM-7 160/153, Unicode 70/67 karakter. SMS başında site adı yer alır.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from site_yonetim.domain.notifications import Matrix, NotificationCategory, NotificationChannel

DEFAULT_QUIET_FROM = time(22, 0)
DEFAULT_QUIET_TO = time(8, 0)
CRITICAL_FALLBACK_AFTER = timedelta(minutes=2)
QUOTA_WARN_PERCENTS = (80, 100)

CRITICAL_CATEGORIES = frozenset({NotificationCategory.URGENT})
# Sessiz saati aşan kategoriler (PLT-05 matrisi: "Aşar").
QUIET_BYPASS = frozenset({NotificationCategory.URGENT})


class DeliveryStatus(StrEnum):
    QUEUED = "queued"  # gönderim zamanı bekleniyor (sessiz saat dahil)
    SENT = "sent"  # sağlayıcıya iletildi
    DELIVERED = "delivered"  # sağlayıcı teslim bildirdi
    FAILED = "failed"
    SKIPPED = "skipped"  # adres yok / iptal


class SmsAccountMode(StrEnum):
    PLATFORM = "platform"  # paket kotasıyla platform hesabı (varsayılan)
    OWN = "own"  # sitenin kendi sağlayıcı hesabı ve başlığı


DELIVERY_STATUS_LABELS = {
    DeliveryStatus.QUEUED: "Kuyrukta",
    DeliveryStatus.SENT: "Gönderildi",
    DeliveryStatus.DELIVERED: "Teslim edildi",
    DeliveryStatus.FAILED: "Başarısız",
    DeliveryStatus.SKIPPED: "Gönderilmedi",
}


def in_quiet_hours(local: time, quiet_from: time, quiet_to: time) -> bool:
    if quiet_from == quiet_to:
        return False
    if quiet_from < quiet_to:
        return quiet_from <= local < quiet_to
    return local >= quiet_from or local < quiet_to


def quiet_hours_end(now: datetime, tz: ZoneInfo, quiet_from: time, quiet_to: time) -> datetime:
    """Şu an sessiz saatteyse bitiş anı (UTC), değilse `now`."""
    local = now.astimezone(tz)
    if not in_quiet_hours(local.time(), quiet_from, quiet_to):
        return now
    end = datetime.combine(local.date(), quiet_to, tzinfo=tz)
    if end <= local:
        end = datetime.combine(local.date() + timedelta(days=1), quiet_to, tzinfo=tz)
    return end.astimezone(UTC)


def send_time(
    category: NotificationCategory,
    now: datetime,
    tz: ZoneInfo,
    quiet_from: time,
    quiet_to: time,
) -> datetime:
    if category in QUIET_BYPASS:
        return now
    return quiet_hours_end(now, tz, quiet_from, quiet_to)


@dataclass(frozen=True, slots=True)
class Reachability:
    """Kişinin dış kanal adresleri."""

    has_push: bool
    phone: str | None
    email: str | None


def channels_for(
    category: NotificationCategory, matrix: Matrix, reach: Reachability
) -> list[NotificationChannel]:
    """Tercihe göre açık ve adresi olan kanallar.

    Kritik kategoride SMS **yedektir**: push gidebiliyorsa önce push, 2 dakikada teslim
    edilmezse SMS (`CRITICAL_FALLBACK_AFTER`); push jetonu yoksa SMS hemen (PLT-05 S2)."""
    wanted = [channel for channel, on in matrix[category].items() if on]
    available = {
        NotificationChannel.PUSH: reach.has_push,
        NotificationChannel.SMS: reach.phone is not None,
        NotificationChannel.EMAIL: reach.email is not None,
    }
    chosen = [c for c in wanted if available[c]]
    if category in CRITICAL_CATEGORIES:
        if NotificationChannel.PUSH in chosen:
            chosen = [c for c in chosen if c is not NotificationChannel.SMS]
        elif reach.phone is not None and NotificationChannel.SMS not in chosen:
            chosen.append(NotificationChannel.SMS)
    return chosen


# --- SMS -----------------------------------------------------------------------------

_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà^{}\\[~]|€"
)


@dataclass(frozen=True, slots=True)
class SmsSize:
    encoding: str  # "GSM-7" | "Unicode"
    chars: int
    segments: int


def sms_size(text: str) -> SmsSize:
    gsm = all(ch in _GSM7 for ch in text)
    single, multi = (160, 153) if gsm else (70, 67)
    length = len(text)
    if length == 0:
        segments = 0
    elif length <= single:
        segments = 1
    else:
        segments = -(-length // multi)
    return SmsSize("GSM-7" if gsm else "Unicode", length, segments)


def sms_text(site_name: str | None, body: str) -> str:
    """SMS başında site adı (Ürün Dokümanı §3.3)."""
    return f"{site_name}: {body}" if site_name else body


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    quota: int
    used: int

    @property
    def remaining(self) -> int:
        return max(0, self.quota - self.used)

    @property
    def overage(self) -> int:
        return max(0, self.used - self.quota)

    @property
    def percent(self) -> int:
        if self.quota <= 0:
            return 100 if self.used else 0
        return (self.used * 100) // self.quota

    def forecast(self, day: int, days_in_month: int) -> int:
        """Ay sonu tahmini: bugüne kadarki kullanımın doğrusal uzatması."""
        if day <= 0:
            return self.used
        return (self.used * days_in_month + day - 1) // day

    def crossed(self, before: int) -> list[int]:
        """`before` kullanımından şimdiki kullanıma geçerken aşılan uyarı eşikleri (%80/%100)."""
        old = QuotaUsage(self.quota, before).percent
        return [p for p in QUOTA_WARN_PERCENTS if old < p <= self.percent]
