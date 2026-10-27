"""Tek kullanımlık kod: üretim, gönderim ve doğrulama (Yazılım Dokümanı §4.7, PLT-01, PLT-10).

- Hedef (telefon/e-posta) kayıtlı olsun olmasın aynı yol izlenir — yanıt ve süre aynı
  (kullanıcı numaralandırması yok). Kayıtlı değilse `user_id` boş kalır.
- Oran sınırı hedef başınadır ve amaçtan bağımsızdır (60 sn / günde 10 / 5 hatada 15 dk kilit).
  Aynı hedefe eşzamanlı istekler işlem kilidiyle sıraya girer.
- Kodun yalnız HMAC özeti saklanır; kod yalnız sağlayıcıya (test sağlayıcısında süreç
  belleğine) gider. Her adım `auth_events`'e yazılır (hedef yazılmaz).
- Transaction'ı bu modül kapatır: hatalı deneme sayacı hata yanıtından önce kalıcı olmalı.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.notifications import NotificationChannel
from site_yonetim.domain.otp import (
    CODE_TTL,
    DAILY_WINDOW,
    LOCK_DURATION,
    RESEND_AFTER,
    OtpChannel,
    OtpPurpose,
    RequestBlock,
    VerifyResult,
    after_wrong_attempt,
    code_digest,
    code_matches,
    new_code,
    request_decision,
)
from site_yonetim.models import AuthEventKind, OtpChallenge
from site_yonetim.services.auth_events import Client, event
from site_yonetim.services.channels import Channels, OutboundMessage

_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")

MESSAGES = {
    OtpPurpose.LOGIN: "Giriş kodunuz: {code}. 3 dakika geçerlidir; kimseyle paylaşmayın.",
    OtpPurpose.PASSWORD_RESET: "Parola sıfırlama kodunuz: {code}. 3 dakika geçerlidir.",
    OtpPurpose.PHONE_CHANGE: "Telefon doğrulama kodunuz: {code}. 3 dakika geçerlidir.",
    OtpPurpose.STEP_UP: "İşlem doğrulama kodunuz: {code}. 3 dakika geçerlidir.",
}


class CodeBlockedError(Exception):
    """Oran sınırı: kilit, 60 sn ya da günlük sınır."""

    def __init__(self, block: RequestBlock, retry_after_seconds: int) -> None:
        super().__init__(block.value)
        self.block = block
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class SentCode:
    expires_at: datetime
    resend_after_seconds: int
    code: str  # yalnız test gelen kutusu açıkken dışarı verilir (API karar verir)


async def _lock(session: AsyncSession, target: str) -> None:
    await session.execute(_LOCK, {"key": f"otp:{target}"})


async def _locked_until(session: AsyncSession, target: str, now: datetime) -> datetime | None:
    value: datetime | None = await session.scalar(
        select(func.max(OtpChallenge.locked_until)).where(
            OtpChallenge.target == target, OtpChallenge.locked_until > now
        )
    )
    return value


async def request_code(
    session: AsyncSession,
    *,
    purpose: OtpPurpose,
    channel: OtpChannel,
    target: str,
    user_id: uuid.UUID | None,
    now: datetime,
    key: bytes,
    channels: Channels,
    client: Client,
    send: bool = True,
) -> SentCode:
    """Kod üretir ve gönderir. Sınır aşıldıysa `CodeBlockedError` (kayıt yazılmaz).

    `send=False`: hesap yokken gönderim anlamsızsa (parola sıfırlama) kayıt ve sınır aynen
    işler ama mesaj gitmez — yanıt yine aynıdır."""
    await _lock(session, target)
    window = await session.execute(
        select(
            func.max(OtpChallenge.created_at), func.min(OtpChallenge.created_at), func.count()
        ).where(OtpChallenge.target == target, OtpChallenge.created_at > now - DAILY_WINDOW)
    )
    last_sent, oldest, count = window.one()
    decision = request_decision(
        now=now,
        locked_until=await _locked_until(session, target, now),
        last_sent_at=last_sent,
        sent_last_24h=int(count),
        oldest_in_window=oldest,
    )
    if decision.block is not None:
        await session.rollback()
        raise CodeBlockedError(decision.block, decision.retry_after_seconds)

    code = new_code()
    challenge = OtpChallenge(
        id=uuid.uuid7(),
        purpose=purpose.value,
        channel=channel.value,
        target=target,
        user_id=user_id,
        code_digest=code_digest(key, purpose, target, code),
        expires_at=now + CODE_TTL,
        attempts=0,
        ip=client.ip,
        created_at=now,
    )
    # Aynı amaçla bekleyen eski kodlar geçersiz: yalnız son kod doğrulanır.
    await session.execute(
        update(OtpChallenge)
        .where(
            OtpChallenge.target == target,
            OtpChallenge.purpose == purpose.value,
            OtpChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    session.add(challenge)
    if send:
        provider = channels.sms if channel is OtpChannel.SMS else channels.email
        result = await provider.send(
            OutboundMessage(
                channel=NotificationChannel(channel.value),
                to=target,
                body=MESSAGES[purpose].format(code=code),
                subject="Doğrulama kodunuz" if channel is OtpChannel.EMAIL else None,
            ),
            now=now,
        )
        challenge.provider = result.provider
        challenge.provider_ref = result.provider_ref
        challenge.cost_units = result.cost_units
    event(session, AuthEventKind.CODE_SENT, client, user_id=user_id)
    await session.commit()
    return SentCode(now + CODE_TTL, int(RESEND_AFTER.total_seconds()), code)


@dataclass(frozen=True, slots=True)
class CodeCheck:
    result: VerifyResult
    remaining: int = 0
    user_id: uuid.UUID | None = None
    retry_after_seconds: int = 0


async def verify_code(
    session: AsyncSession,
    *,
    purpose: OtpPurpose,
    target: str,
    code: str,
    now: datetime,
    key: bytes,
    client: Client,
) -> CodeCheck:
    """Son kodu sabit süreli karşılaştırır; sonucu kalıcı yazar (commit) ve döner.

    OK → kod tüketildi; çağıran kendi işini yeni transaction'da yapar."""
    await _lock(session, target)
    locked = await _locked_until(session, target, now)
    if locked is not None:
        await session.rollback()
        return CodeCheck(
            VerifyResult.LOCKED, retry_after_seconds=int((locked - now).total_seconds()) + 1
        )
    challenge = await session.scalar(
        select(OtpChallenge)
        .where(
            OtpChallenge.target == target,
            OtpChallenge.purpose == purpose.value,
            OtpChallenge.consumed_at.is_(None),
        )
        .order_by(OtpChallenge.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    if challenge is None or challenge.expires_at <= now:
        await session.rollback()
        return CodeCheck(VerifyResult.EXPIRED)
    user_id = challenge.user_id
    if code_matches(key, purpose, target, code, challenge.code_digest):
        challenge.consumed_at = now
        await session.commit()
        return CodeCheck(VerifyResult.OK, user_id=user_id)
    outcome = after_wrong_attempt(challenge.attempts)
    challenge.attempts = outcome.attempts
    if outcome.result is VerifyResult.LOCKED:
        challenge.locked_until = now + LOCK_DURATION
        challenge.consumed_at = now
        event(session, AuthEventKind.CODE_FAILED, client, user_id=user_id)
        event(session, AuthEventKind.CODE_LOCKED, client, user_id=user_id)
    else:
        event(session, AuthEventKind.CODE_FAILED, client, user_id=user_id)
    await session.commit()
    return CodeCheck(
        outcome.result,
        remaining=outcome.remaining,
        user_id=user_id,
        retry_after_seconds=int(LOCK_DURATION.total_seconds()),
    )
