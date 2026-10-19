"""Bildirim kategorileri ve kanal tercihleri — saf (Yazılım Dokümanı PLT-05, ILT-05).

Uygulama içi bildirim (bildirim merkezi) **her zaman** yazılır; tercih yalnız dış kanallar
(push, SMS, e-posta) içindir. Dış gönderim henüz yok (açık karar K5: "yalnız kaydet"); tercihler
şimdiden saklanır ki sağlayıcı gelince kullanıcıya yeniden sorulmasın.

Kategori × kanal matrisi (PLT-05):
- Acil ve yönetişim: varsayılan kanallar kapatılamaz.
- Finansal ve güvenlik: kanal değiştirilebilir, ama en az bir kanal açık kalır.
- Operasyon ve duyuru: serbestçe kapatılabilir.
"""

from dataclasses import dataclass
from enum import StrEnum


class NotificationCategory(StrEnum):
    URGENT = "urgent"  # acil durum, su baskını duyurusu
    FINANCIAL = "financial"  # borç bildirimi, makbuz, gecikme hatırlatması
    SECURITY = "security"  # ziyaretçi onayı, kargo geldi
    OPERATIONS = "operations"  # talep durumu, rezervasyon, iş emri, onay talepleri
    GOVERNANCE = "governance"  # genel kurul çağrısı, oylama
    ANNOUNCEMENT = "announcement"  # genel duyurular


class Channel(StrEnum):
    PUSH = "push"
    SMS = "sms"
    EMAIL = "email"


class PushPlatform(StrEnum):
    IOS = "ios"
    ANDROID = "android"
    WEB = "web"


class Rule(StrEnum):
    LOCKED = "locked"  # varsayılan kanallar kapatılamaz
    AT_LEAST_ONE = "at_least_one"  # en az bir kanal açık kalır
    FREE = "free"


@dataclass(frozen=True, slots=True)
class CategoryInfo:
    label: str
    defaults: frozenset[Channel]
    rule: Rule


CATEGORIES: dict[NotificationCategory, CategoryInfo] = {
    NotificationCategory.URGENT: CategoryInfo(
        "Acil", frozenset({Channel.PUSH, Channel.SMS}), Rule.LOCKED
    ),
    NotificationCategory.FINANCIAL: CategoryInfo(
        "Finansal", frozenset({Channel.PUSH, Channel.EMAIL}), Rule.AT_LEAST_ONE
    ),
    NotificationCategory.SECURITY: CategoryInfo(
        "Güvenlik", frozenset({Channel.PUSH}), Rule.AT_LEAST_ONE
    ),
    NotificationCategory.OPERATIONS: CategoryInfo(
        "Operasyon", frozenset({Channel.PUSH}), Rule.FREE
    ),
    NotificationCategory.GOVERNANCE: CategoryInfo(
        "Yönetişim", frozenset({Channel.PUSH, Channel.EMAIL, Channel.SMS}), Rule.LOCKED
    ),
    NotificationCategory.ANNOUNCEMENT: CategoryInfo("Duyuru", frozenset({Channel.PUSH}), Rule.FREE),
}

Matrix = dict[NotificationCategory, dict[Channel, bool]]
Overrides = dict[tuple[NotificationCategory, Channel], bool]


def preference_matrix(overrides: Overrides) -> Matrix:
    """Varsayılanlar + kullanıcının kayıtlı tercihleri."""
    return {
        category: {
            channel: overrides.get((category, channel), channel in info.defaults)
            for channel in Channel
        }
        for category, info in CATEGORIES.items()
    }


def is_locked(category: NotificationCategory, channel: Channel) -> bool:
    """Kapatılamayan hücre (kilit ikonu): kilitli kategorinin varsayılan kanalı."""
    info = CATEGORIES[category]
    return info.rule is Rule.LOCKED and channel in info.defaults


class PreferenceError(ValueError):
    """Türkçe mesajla geçersiz tercih."""


def apply_changes(current: Matrix, changes: Overrides) -> Matrix:
    """Değişiklikleri uygular ve kurallara uyduğunu doğrular; uymuyorsa `PreferenceError`."""
    result = {category: dict(channels) for category, channels in current.items()}
    for (category, channel), enabled in changes.items():
        if not enabled and is_locked(category, channel):
            raise PreferenceError(
                f"{CATEGORIES[category].label} bildirimleri bu kanalda kapatılamaz."
            )
        result[category][channel] = enabled
    for category, info in CATEGORIES.items():
        if info.rule is Rule.AT_LEAST_ONE and not any(result[category].values()):
            raise PreferenceError(f"{info.label} bildirimleri için en az bir kanal açık kalmalı.")
    return result
