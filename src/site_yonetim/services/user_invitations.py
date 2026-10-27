"""Hesap daveti — Yazılım Dokümanı PLT-09 adım 6–10 (P5: geçici parola yerine davet).

- Yönetici yeni kullanıcı ekleyince 72 saat geçerli, tek kullanımlık bağlantı e-postayla
  (telefon varsa SMS ile) gider. Jetonun yalnız SHA-256 özeti saklanır; yeni davet eskisini
  geçersiz kılar.
- Kişi bağlantıyı açar, parolasını belirler; oturum açılır. Yönetici rolündeyse iki adımlı
  doğrulama kurulumu girişte istenir (PLT-01 adım 6).
- Test sağlayıcısında bağlantı dışarı gitmez; geliştirme/demo ortamında yanıtta
  `preview_url` olarak görünür (üretimde asla).
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.masking import mask_email, mask_phone
from site_yonetim.domain.notifications import NotificationChannel
from site_yonetim.models import AuthEventKind, User, UserInvitation
from site_yonetim.services.auth_events import Client, event
from site_yonetim.services.channels import Channels, OutboundMessage

INVITATION_TTL = timedelta(hours=72)
INVITE_PATH = "/hesap-davet/"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SentInvitation:
    invitation: UserInvitation
    url: str  # gizli: yalnız gönderime ve (test kutusu açıkken) önizlemeye
    sent_to: list[str]  # maskeli adresler


async def invite(
    session: AsyncSession,
    user: User,
    *,
    site_name: str | None,
    role_label: str | None,
    invited_by_name: str | None,
    now: datetime,
    web_url: str,
    channels: Channels,
    client: Client,
) -> SentInvitation:
    """Davet oluşturur ve gönderir; bekleyen eski davetleri kapatır. Commit etmez."""
    await session.execute(
        update(UserInvitation)
        .where(
            UserInvitation.user_id == user.id,
            UserInvitation.accepted_at.is_(None),
            UserInvitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    invitation = UserInvitation(
        id=uuid.uuid7(),
        user_id=user.id,
        token_digest=digest(token),
        site_name=site_name,
        role_label=role_label,
        invited_by_name=invited_by_name,
        expires_at=now + INVITATION_TTL,
    )
    session.add(invitation)
    url = f"{web_url}{INVITE_PATH}{token}"
    where = f"{site_name} için " if site_name else ""
    text = (
        f"{user.full_name}, {where}{role_label + ' olarak ' if role_label else ''}"
        f"hesabınız açıldı. Parolanızı belirlemek için bağlantı (72 saat geçerli): {url}"
    )
    sent_to: list[str] = []
    if user.email:
        await channels.email.send(
            OutboundMessage(
                channel=NotificationChannel.EMAIL,
                to=user.email,
                subject="Hesap davetiniz",
                body=text,
            ),
            now=now,
        )
        sent_to.append(mask_email(user.email))
    if user.phone:
        await channels.sms.send(
            OutboundMessage(channel=NotificationChannel.SMS, to=user.phone, body=text), now=now
        )
        sent_to.append(mask_phone(user.phone))
    event(session, AuthEventKind.INVITATION_SENT, client, user_id=user.id)
    return SentInvitation(invitation, url, sent_to)


@dataclass(frozen=True, slots=True)
class OpenInvitation:
    invitation: UserInvitation
    user: User
    status: str  # pending · accepted · expired


async def find(session: AsyncSession, token: str, *, now: datetime) -> OpenInvitation | None:
    """Jetonla davet; yoksa ya da iptal edildiyse None (404)."""
    row = (
        await session.execute(
            select(UserInvitation, User)
            .join(User, User.id == UserInvitation.user_id)
            .where(UserInvitation.token_digest == digest(token))
        )
    ).first()
    if row is None:
        return None
    invitation, user = row
    if invitation.revoked_at is not None or not user.is_active:
        return None
    if invitation.accepted_at is not None:
        status = "accepted"
    elif invitation.expires_at <= now:
        status = "expired"
    else:
        status = "pending"
    return OpenInvitation(invitation, user, status)


async def lock_pending(
    session: AsyncSession, token: str, *, now: datetime
) -> OpenInvitation | None:
    """Kabul için: bekleyen daveti ve kullanıcıyı kilitler (çift kabul olmaz)."""
    row = (
        await session.execute(
            select(UserInvitation, User)
            .join(User, User.id == UserInvitation.user_id)
            .where(
                UserInvitation.token_digest == digest(token),
                UserInvitation.accepted_at.is_(None),
                UserInvitation.revoked_at.is_(None),
                UserInvitation.expires_at > now,
                User.is_active,
            )
            .with_for_update()
        )
    ).first()
    if row is None:
        return None
    return OpenInvitation(row[0], row[1], "pending")


def mark_accepted(
    session: AsyncSession, found: OpenInvitation, *, now: datetime, client: Client
) -> None:
    found.invitation.accepted_at = now
    event(session, AuthEventKind.INVITATION_ACCEPTED, client, user_id=found.user.id)
