"""Davetler, hane üyeleri ve tesis kurulum durumu — saf kurallar (servis istekleri 20, 23, 26;
Yazılım Dokümanı YAP-01, YAP-04, YAP-07).

SMS sağlayıcısı seçilmedi (K5): davet bağlantısı üretilir, **gönderilmez**; yönetim ekrandan
kopyalayıp iletir. Bağlantının belirteci yalnız özet (SHA-256) olarak saklanır — bağlantı
yalnız oluşturma/yeniden gönderme yanıtında bir kez görünür.
"""

import hashlib
import secrets
from datetime import datetime
from enum import StrEnum

TOKEN_BYTES = 16
INVITE_DAYS_MIN, INVITE_DAYS_MAX, INVITE_DAYS_DEFAULT = 1, 30, 7
HOUSEHOLD_LIMIT = 6


class InvitationRelation(StrEnum):
    OWNER = "owner"
    TENANT = "tenant"
    HOUSEHOLD = "household"  # hane üyesi daveti (istek 20) — aynı kabul akışı


class InvitationStatus(StrEnum):
    SENT = "sent"
    OPENED = "opened"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


OPEN_INVITATION = frozenset({InvitationStatus.SENT, InvitationStatus.OPENED})


class HouseholdStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    ENDED = "ended"


class SiteLifecycle(StrEnum):
    SETUP = "setup"  # kurulumda: sakin daveti ve herkese açık kayıt kapalı
    LIVE = "live"


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def effective_status(status: str, expires_at: datetime, now: datetime) -> InvitationStatus:
    """Süresi dolan açık davet okunurken `expired` görünür (gece işi de kalıcı yazar)."""
    current = InvitationStatus(status)
    if current in OPEN_INVITATION and expires_at <= now:
        return InvitationStatus.EXPIRED
    return current


def split_full_name(full_name: str) -> tuple[str, str]:
    """ "Ayşe Nur Yılmaz" → ("Ayşe Nur", "Yılmaz"); tek kelimede soyad da aynı kelime olmaz →
    ValueError."""
    words = full_name.split()
    if len(words) < 2:
        raise ValueError("Ad ve soyadı birlikte yazın.")
    return " ".join(words[:-1]), words[-1]
