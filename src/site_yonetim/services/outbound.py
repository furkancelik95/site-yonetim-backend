"""Dış kanal (SMS) arayüz noktası — bu paketin dışarıya ileti gerektiren adımları için.

Okuma onayı SMS yedeği (16.3) gibi adımlar burayı çağırır. Sağlayıcıdan bağımsız gönderim
katmanı (SmsProvider adaptörü, kota, teslim kaydı) paralel P16c paketinde yazılıyor; o birleşince
`register_sms_sender(...)` ile bağlanacak. O zamana kadar varsayılan gönderici **hiçbir şey
göndermez**: kaç kişiye gideceği kayda geçer, yanıtta "gönderilmedi" açıkça yazılır (K5).

Acil durum ve kritik bildirimler ayrıca `services/notifications.notify` (kategori `urgent`)
ile uygulama içine yazılır; P16c katmanı aynı çağrıdan push + SMS'i (sessiz saati aşarak) üretir.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

NOT_CONNECTED = (
    "SMS sağlayıcısı bağlı değil (K5; P16c ile bağlanacak): kişi sayısı kayda geçti, "
    "dışarıya ileti gitmedi."
)


@dataclass(frozen=True, slots=True)
class OutboundResult:
    sent: int  # sağlayıcıya gerçekten verilen ileti sayısı
    note: str


SmsSender = Callable[[AsyncSession, Sequence[uuid.UUID], str, str], Awaitable[OutboundResult]]


async def _not_connected(
    _session: AsyncSession, _user_ids: Sequence[uuid.UUID], _kind: str, _text: str
) -> OutboundResult:
    return OutboundResult(0, NOT_CONNECTED)


_sender: SmsSender = _not_connected


def register_sms_sender(sender: SmsSender) -> None:
    """P16c gönderim katmanı uygulama açılışında kendi göndericisini kaydeder."""
    global _sender
    _sender = sender


async def send_sms(
    session: AsyncSession, user_ids: Sequence[uuid.UUID], kind: str, text: str
) -> OutboundResult:
    """Kullanıcılara SMS (kotadan düşer). `kind` şablon kodu (ör. `ack_reminder`)."""
    if not user_ids:
        return OutboundResult(0, "Gönderilecek kişi yok.")
    return await _sender(session, user_ids, kind, text)
