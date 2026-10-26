"""Dış kanal adaptörleri: SMS, e-posta, push — Yazılım Dokümanı PLT-05, §16.2.

Modüller doğrudan sağlayıcıya gitmez; `services/delivery.py` (NotificationService) ve kimlik
kodları (`services/otp.py`) bu arayüzleri kullanır. Sağlayıcı açık karar K5'te seçilecek
(Netgsm, İleti Merkezi, Verimor adayları); şimdilik yalnız **test sağlayıcısı** var:

- Gönderim dışarı gitmez; teslim kaydı ve maliyet birimi (SMS adedi) gerçekteki gibi yazılır.
- Son mesajlar süreç belleğinde (son 100) tutulur — veritabanına **kod düz metin yazılmaz**.
  Geliştirme/demo ortamında platform yöneticisi "test gelen kutusu"ndan görür; giriş ekranı
  demo kodunu gösterebilir. Üretimde (`Settings.test_inbox_active` False) hiçbiri dışarı
  verilmez.
"""

import itertools
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from site_yonetim.core.config import Settings
from site_yonetim.domain.delivery import DeliveryStatus, sms_size
from site_yonetim.domain.notifications import NotificationChannel


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    channel: NotificationChannel
    to: str  # telefon (E.164), e-posta ya da kullanıcı kimliği (push)
    body: str
    subject: str | None = None
    sender: str | None = None  # SMS başlığı (kendi hesabı)
    push_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SendResult:
    status: DeliveryStatus
    provider: str
    provider_ref: str | None
    cost_units: int = 0
    error: str | None = None


class ChannelProvider(Protocol):
    name: str

    async def send(self, message: OutboundMessage, *, now: datetime) -> SendResult: ...


@dataclass(frozen=True, slots=True)
class InboxItem:
    id: int
    at: datetime
    channel: str
    to: str
    subject: str | None
    body: str
    provider_ref: str


@dataclass
class TestInbox:
    """Test sağlayıcısının gönderdikleri (süreç belleği, son `size` mesaj)."""

    __test__ = False  # pytest bunu test sınıfı sanmasın

    size: int = 100
    _items: deque[InboxItem] = field(default_factory=deque)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, message: OutboundMessage, *, now: datetime, ref: str) -> None:
        item = InboxItem(
            id=next(self._ids),
            at=now,
            channel=message.channel.value,
            to=message.to,
            subject=message.subject,
            body=message.body,
            provider_ref=ref,
        )
        with self._lock:
            self._items.append(item)
            while len(self._items) > self.size:
                self._items.popleft()

    def items(self, *, to: str | None = None, limit: int = 50) -> list[InboxItem]:
        with self._lock:
            rows = [i for i in reversed(self._items) if to is None or i.to == to]
        return rows[:limit]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


TEST_INBOX = TestInbox()


@dataclass
class TestProvider:
    """Kayda geçirir, göndermez (K5). Push için test: jeton varsa "gönderildi"."""

    __test__ = False

    channel: NotificationChannel
    inbox: TestInbox = field(default_factory=lambda: TEST_INBOX)
    name: str = "test"

    async def send(self, message: OutboundMessage, *, now: datetime) -> SendResult:
        ref = f"test-{uuid.uuid7().hex[:16]}"
        if self.channel is NotificationChannel.PUSH and not message.push_tokens:
            return SendResult(DeliveryStatus.FAILED, self.name, None, 0, "Push jetonu yok.")
        self.inbox.add(message, now=now, ref=ref)
        cost = sms_size(message.body).segments if self.channel is NotificationChannel.SMS else 0
        return SendResult(DeliveryStatus.SENT, self.name, ref, cost)


@dataclass(frozen=True, slots=True)
class Channels:
    sms: ChannelProvider
    email: ChannelProvider
    push: ChannelProvider

    def for_channel(self, channel: NotificationChannel) -> ChannelProvider:
        return {
            NotificationChannel.SMS: self.sms,
            NotificationChannel.EMAIL: self.email,
            NotificationChannel.PUSH: self.push,
        }[channel]


def channels_from_settings(settings: Settings) -> Channels:
    """Ayardaki sağlayıcılar. Şimdilik hepsi "test" (K5 sonrası gerçek adaptör eklenir)."""
    del settings  # tek seçenek "test"; seçim sağlayıcı gelince buradan yapılır
    return Channels(
        sms=TestProvider(NotificationChannel.SMS),
        email=TestProvider(NotificationChannel.EMAIL),
        push=TestProvider(NotificationChannel.PUSH),
    )
