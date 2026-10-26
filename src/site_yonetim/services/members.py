"""Site kullanıcıları ve roller — frontend servis isteği 12. Açık site kapsamında; transaction'ı
çağıran yönetir.

Roller sabittir (docs/05 §3); burada kişiye rol verilir. Sakin üyelikleri bu ekranın dışında.

- Yeni e-posta → yeni kullanıcı + **72 saatlik davet bağlantısı** (P5, PLT-09): kişi parolasını
  bağlantıdan belirler; geçici parola yok. Bağlantı e-postayla gider (test sağlayıcısında
  geliştirme/demo yanıtında önizleme).
- Kayıtlı e-posta → yeni kullanıcı açılmaz, mevcut kullanıcıya bu sitede rol verilir.
- Yönetim şirketi üyeliğinden gelen erişim (`source = organization`) buradan değiştirilemez.
- Sitede en az bir etkin yönetici kalır (açık üyelik ya da şirketten gelen). Kontrol site başına
  işlem kilidi altında: eşzamanlı iki değişiklik son yöneticiyi birlikte düşüremez.
- Erişimi kapatma kaydı silmez; kullanıcının açık oturumları sonlandırılır.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import (
    ORGANIZATION_ROLE_TO_SITE_ROLE,
    ROLE_PERMISSIONS,
    OrganizationRole,
    Permission,
    SiteRole,
    UserKind,
)
from site_yonetim.domain.members import BY_KEY, BY_ROLE, FormFieldError, check_full_name
from site_yonetim.domain.validation import normalize_email_address
from site_yonetim.models import AuthSession, OrganizationMembership, Site, SiteMembership, User
from site_yonetim.services import invitations
from site_yonetim.services.auth_events import Client
from site_yonetim.services.channels import Channels

_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


class MemberRuleError(Exception):
    """409 kuralları: zaten üye, şirketten gelen erişim, son yönetici, kendi kaydı."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Member:
    id: uuid.UUID  # açık üyelikte site üyeliği, şirketten gelende şirket üyeliği kimliği
    user: User
    role: SiteRole
    is_active: bool
    source: str  # site · organization
    invited_at: datetime


async def _explicit(session: AsyncSession) -> list[tuple[SiteMembership, User]]:
    rows = await session.execute(
        select(SiteMembership, User).join(User, User.id == SiteMembership.user_id)
    )
    return [(m, u) for m, u in rows]


async def _organization(
    session: AsyncSession, site: Site
) -> list[tuple[OrganizationMembership, User]]:
    if site.organization_id is None:
        return []
    rows = await session.execute(
        select(OrganizationMembership, User)
        .join(User, User.id == OrganizationMembership.user_id)
        .where(
            OrganizationMembership.organization_id == site.organization_id,
            OrganizationMembership.is_active,
        )
    )
    return [(m, u) for m, u in rows]


async def list_members(session: AsyncSession, site: Site) -> list[Member]:
    explicit = await _explicit(session)
    members = [
        Member(m.id, u, SiteRole(m.role), m.is_active, "site", m.created_at)
        for m, u in explicit
        if m.role != SiteRole.RESIDENT.value
    ]
    with_explicit = {u.id for _, u in explicit}
    members += [
        Member(
            m.id,
            u,
            ORGANIZATION_ROLE_TO_SITE_ROLE[OrganizationRole(m.role)],
            True,
            "organization",
            m.created_at,
        )
        for m, u in await _organization(session, site)
        if u.id not in with_explicit
    ]
    return sorted(members, key=lambda m: m.user.full_name.casefold())


def _role(key: str | None) -> SiteRole:
    info = BY_KEY.get(key or "")
    if info is None:
        raise FormFieldError({"role_key": "Rol seçin."})
    return info.role


@dataclass(frozen=True, slots=True)
class Added:
    member: Member
    invitation: invitations.SentInvitation | None  # yalnız yeni kullanıcıda


@dataclass(frozen=True, slots=True)
class Inviter:
    """Davet gönderimi için bağlam (API katmanından)."""

    name: str
    now: datetime
    web_url: str
    channels: Channels
    client: Client


async def add_member(
    session: AsyncSession,
    site: Site,
    *,
    full_name: str,
    email: str,
    role_key: str,
    inviter: Inviter,
) -> Added:
    errors: dict[str, str] = {}
    try:
        name = check_full_name(full_name)
    except ValueError as exc:
        errors["full_name"] = str(exc)
    try:
        address = normalize_email_address(email)
    except ValueError:
        errors["email"] = "Geçerli bir e-posta adresi girin."
    if BY_KEY.get(role_key) is None:
        errors["role_key"] = "Rol seçin."
    if errors:
        raise FormFieldError(errors)
    role = _role(role_key)

    user: User | None = await session.scalar(select(User).where(User.email == address))
    sent = None
    if user is not None:
        taken = await session.scalar(
            select(SiteMembership.id).where(SiteMembership.user_id == user.id)
        )
        if taken is not None:
            raise MemberRuleError("already_member", "Bu e-posta zaten sitede kullanıcı.")
    else:
        user = User(email=address, full_name=name, password_hash=None, kind=UserKind.STAFF.value)
        session.add(user)
        await session.flush()
        sent = await invitations.invite(
            session,
            user,
            site_name=site.name,
            role_label=role.value,
            invited_by_name=inviter.name,
            now=inviter.now,
            web_url=inviter.web_url,
            channels=inviter.channels,
            client=inviter.client,
        )
    membership = SiteMembership(user_id=user.id, role=role.value, is_active=True)
    session.add(membership)
    await session.flush()
    await session.refresh(membership, ["created_at"])
    member = Member(membership.id, user, role, True, "site", membership.created_at)
    return Added(member, sent)


async def resend_invitation(
    session: AsyncSession, site: Site, member_id: uuid.UUID, *, inviter: Inviter
) -> invitations.SentInvitation:
    """Parolasını henüz belirlememiş kullanıcıya yeni davet (eskisi geçersiz)."""
    row = (
        await session.execute(
            select(SiteMembership, User)
            .join(User, User.id == SiteMembership.user_id)
            .where(SiteMembership.id == member_id)
        )
    ).first()
    if row is None or row[0].role == SiteRole.RESIDENT.value:
        raise LookupError
    membership, user = row
    if user.password_hash is not None:
        raise MemberRuleError(
            "already_active", "Kullanıcı parolasını belirlemiş; davet gerekmiyor."
        )
    return await invitations.invite(
        session,
        user,
        site_name=site.name,
        role_label=membership.role,
        invited_by_name=inviter.name,
        now=inviter.now,
        web_url=inviter.web_url,
        channels=inviter.channels,
        client=inviter.client,
    )


async def users_with_permission(
    session: AsyncSession, site: Site, permission: Permission
) -> list[uuid.UUID]:
    """Sitede bu izne sahip etkin yönetim kullanıcıları (bildirim alıcısı)."""
    return [
        m.user.id
        for m in await list_members(session, site)
        if m.is_active and m.user.is_active and permission in ROLE_PERMISSIONS[m.role]
    ]


async def _managers(session: AsyncSession, site: Site, *, excluding: uuid.UUID) -> int:
    """Etkin yöneticiler (`excluding` kullanıcısı hariç): açık üyelik + şirketten gelen."""
    explicit = await _explicit(session)
    count = sum(
        1
        for m, u in explicit
        if m.is_active and m.role == SiteRole.MANAGER.value and u.id != excluding and u.is_active
    )
    with_explicit = {u.id for _, u in explicit}
    count += sum(
        1
        for m, u in await _organization(session, site)
        if u.id not in with_explicit
        and u.id != excluding
        and u.is_active
        and ORGANIZATION_ROLE_TO_SITE_ROLE[OrganizationRole(m.role)] is SiteRole.MANAGER
    )
    return count


async def update_member(
    session: AsyncSession,
    site: Site,
    member_id: uuid.UUID,
    *,
    role_key: str | None,
    is_active: bool | None,
    actor_id: uuid.UUID,
    now: datetime,
) -> tuple[Member, bool]:
    """(üye, oturumları kapatıldı mı)."""
    await session.execute(_LOCK, {"key": f"members:{site.id}"})
    row = (
        await session.execute(
            select(SiteMembership, User)
            .join(User, User.id == SiteMembership.user_id)
            .where(SiteMembership.id == member_id)
            .with_for_update(of=SiteMembership)
        )
    ).first()
    if row is None or row[0].role == SiteRole.RESIDENT.value:
        derived = await session.scalar(
            select(OrganizationMembership.id).where(OrganizationMembership.id == member_id)
        )
        if derived is not None and site.organization_id is not None:
            raise MemberRuleError(
                "derived_membership",
                "Bu erişim yönetim şirketi üyeliğinden geliyor; şirket ayarlarından değiştirilir.",
            )
        raise LookupError
    membership, user = row
    if user.id == actor_id:
        raise MemberRuleError(
            "self_change", "Kendi rolünüzü değiştiremez, kendi erişiminizi kapatamazsınız."
        )
    new_role = _role(role_key) if role_key is not None else SiteRole(membership.role)
    new_active = membership.is_active if is_active is None else is_active
    was_manager = membership.is_active and membership.role == SiteRole.MANAGER.value
    stays_manager = new_active and new_role is SiteRole.MANAGER
    if was_manager and not stays_manager and await _managers(session, site, excluding=user.id) == 0:
        raise MemberRuleError("last_manager", "Sitede en az bir etkin yönetici kalmalı.")
    closed = membership.is_active and not new_active
    membership.role = new_role.value
    membership.is_active = new_active
    if closed:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    await session.flush()
    return Member(membership.id, user, new_role, new_active, "site", membership.created_at), closed


def role_name(role: SiteRole) -> str:
    return role.value


def role_key(role: SiteRole) -> str:
    return BY_ROLE[role.value].key
