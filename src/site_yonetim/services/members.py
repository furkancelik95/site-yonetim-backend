"""Site kullanıcıları ve rolleri — frontend servis isteği 12; çoklu rol, kapsam ve süre (P18,
PLT-09). Açık site kapsamında; transaction'ı çağıran yönetir.

- Yeni e-posta → yeni kullanıcı + **geçici parola** (yalnız yanıtta döner; hash'li saklanır, loga
  ve denetim kaydına yazılmaz). İlk girişte parola değiştirmek zorunlu.
- Kayıtlı e-posta → yeni kullanıcı açılmaz, mevcut kullanıcıya bu sitede rol verilir.
- Bir kişinin birden çok rolü olabilir; roller `services/roles.py` ile atanır/kaldırılır.
- Yönetim şirketi üyeliğinden gelen erişim (`source = organization`) buradan değiştirilemez.
- Sitede `members.manage` izni olan en az bir etkin kullanıcı kalır (açık üyelik ya da şirket).
  Kontrol site başına işlem kilidi altında: eşzamanlı iki değişiklik son yöneticiyi düşüremez.
- Erişimi kapatma kaydı silmez; kullanıcının açık oturumları sonlandırılır.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.security import hash_password
from site_yonetim.domain.access import (
    ORGANIZATION_ROLE_TO_SITE_ROLE,
    TEMPLATE_KEYS,
    OrganizationRole,
    Permission,
    RoleScope,
    UserKind,
)
from site_yonetim.domain.members import FormFieldError, check_full_name
from site_yonetim.domain.roles import RESIDENT_KEY, RoleCatalog, RoleRuleError, is_active_at
from site_yonetim.domain.validation import normalize_email_address
from site_yonetim.models import (
    AuthSession,
    Block,
    OrganizationMembership,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    User,
)
from site_yonetim.services import roles as roles_svc

TEMPORARY_PASSWORD_BYTES = 12


class MemberRuleError(Exception):
    """409 kuralları: zaten üye, şirketten gelen erişim, son yönetici, kendi kaydı."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class MemberRole:
    assignment_id: uuid.UUID | None  # şirketten gelende boş
    key: str
    name: str
    level: int
    scope_kind: RoleScope
    block_id: uuid.UUID | None
    block_name: str | None
    starts_at: datetime | None
    ends_at: datetime | None
    is_current: bool  # şu an etkin (ileri tarihli değil)


@dataclass(frozen=True, slots=True)
class Member:
    id: uuid.UUID  # açık üyelikte site üyeliği, şirketten gelende şirket üyeliği kimliği
    user: User
    roles: tuple[MemberRole, ...]  # bitmemiş roller; ana rol önce
    is_active: bool
    source: str  # site · organization
    invited_at: datetime

    @property
    def primary(self) -> MemberRole | None:
        return next((r for r in self.roles if r.is_current), None) or (
            self.roles[0] if self.roles else None
        )


def _member_role(
    assignment: SiteRoleAssignment,
    catalog: RoleCatalog,
    blocks: dict[uuid.UUID, str],
    now: datetime,
) -> MemberRole | None:
    role = catalog.by_key(assignment.role_key)
    if role is None:
        return None
    return MemberRole(
        assignment_id=assignment.id,
        key=role.key,
        name=role.name,
        level=role.level,
        scope_kind=RoleScope(assignment.scope_kind),
        block_id=assignment.block_id,
        block_name=blocks.get(assignment.block_id) if assignment.block_id else None,
        starts_at=assignment.starts_at,
        ends_at=assignment.ends_at,
        is_current=is_active_at(assignment.starts_at, assignment.ends_at, now),
    )


def _sorted(roles: list[MemberRole]) -> tuple[MemberRole, ...]:
    return tuple(
        sorted(
            roles,
            key=lambda r: (
                not r.is_current,
                r.level,
                r.scope_kind is not RoleScope.SITE,
                r.name.casefold(),
            ),
        )
    )


async def _members(
    session: AsyncSession, site: Site, now: datetime, *, member_id: uuid.UUID | None = None
) -> list[Member]:
    catalog = await roles_svc.load_catalog(session)
    blocks = {b.id: b.name for b in await session.scalars(select(Block))}
    query = select(SiteMembership, User).join(User, User.id == SiteMembership.user_id)
    if member_id is not None:
        query = query.where(SiteMembership.id == member_id)
    memberships = (await session.execute(query)).all()
    user_ids = [u.id for _, u in memberships]
    assignments: dict[uuid.UUID, list[SiteRoleAssignment]] = {}
    if user_ids:
        for a in await session.scalars(
            select(SiteRoleAssignment).where(SiteRoleAssignment.user_id.in_(user_ids))
        ):
            assignments.setdefault(a.user_id, []).append(a)
    members: list[Member] = []
    for membership, user in memberships:
        rows = assignments.get(user.id, [])
        keys = {a.role_key for a in rows}
        if RESIDENT_KEY in keys and keys <= {RESIDENT_KEY}:
            continue  # yalnız sakin: bu ekranın dışında
        roles = [
            r
            for a in rows
            if a.role_key != RESIDENT_KEY and (a.ends_at is None or a.ends_at > now)
            if (r := _member_role(a, catalog, blocks, now)) is not None
        ]
        members.append(
            Member(
                membership.id, user, _sorted(roles), membership.is_active, "site",
                membership.created_at,
            )
        )  # fmt: skip
    if member_id is None and site.organization_id is not None:
        with_explicit = {u.id for _, u in memberships}
        for org, user in await session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.organization_id == site.organization_id,
                OrganizationMembership.is_active,
            )
        ):
            if user.id in with_explicit:
                continue
            template = ORGANIZATION_ROLE_TO_SITE_ROLE[OrganizationRole(org.role)]
            role = catalog.by_key(TEMPLATE_KEYS[template])
            if role is None:  # pragma: no cover
                continue
            derived = MemberRole(
                None, role.key, role.name, role.level, RoleScope.SITE, None, None, None, None, True
            )
            members.append(Member(org.id, user, (derived,), True, "organization", org.created_at))
    return sorted(members, key=lambda m: (m.user.full_name.casefold(), m.user.id))


async def list_members(session: AsyncSession, site: Site, now: datetime) -> list[Member]:
    return await _members(session, site, now)


async def get_member(
    session: AsyncSession, site: Site, member_id: uuid.UUID, now: datetime
) -> Member:
    found = await _members(session, site, now, member_id=member_id)
    if not found:
        derived = await session.scalar(
            select(OrganizationMembership.id).where(OrganizationMembership.id == member_id)
        )
        if derived is not None and site.organization_id is not None:
            raise MemberRuleError(
                "derived_membership",
                "Bu erişim yönetim şirketi üyeliğinden geliyor; şirket ayarlarından değiştirilir.",
            )
        raise LookupError
    return found[0]


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: uuid.UUID
    name: str
    permissions: frozenset[Permission]


@dataclass(frozen=True, slots=True)
class Added:
    member: Member
    temporary_password: str | None  # yalnız yeni kullanıcıda; saklanmaz


async def add_member(
    session: AsyncSession,
    site: Site,
    *,
    full_name: str,
    email: str,
    assignment: roles_svc.AssignmentInput,
    actor: Actor,
    now: datetime,
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
    catalog = await roles_svc.load_catalog(session)
    role = catalog.by_key(assignment.role_key or "")
    if role is None or not role.is_active or not role.is_staff:
        errors["role_key"] = "Rol seçin."
    if errors:
        raise FormFieldError(errors)

    user: User | None = await session.scalar(select(User).where(User.email == address))
    password = None
    if user is not None:
        taken = await session.scalar(
            select(SiteMembership.id).where(SiteMembership.user_id == user.id)
        )
        if taken is not None:
            raise MemberRuleError("already_member", "Bu e-posta zaten sitede kullanıcı.")
    else:
        password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
        user = User(
            email=address,
            full_name=name,
            password_hash=hash_password(password),
            kind=UserKind.STAFF.value,
            must_change_password=True,
        )
        session.add(user)
        await session.flush()
    membership = SiteMembership(user_id=user.id, is_active=True)
    session.add(membership)
    await session.flush()
    await roles_svc.assign_role(
        session, site, user_id=user.id, data=assignment, actor_id=actor.user_id,
        actor_permissions=actor.permissions, actor_name=actor.name, now=now,
    )  # fmt: skip
    return Added(await get_member(session, site, membership.id, now), password)


async def _revoke_sessions(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )


async def update_member(
    session: AsyncSession,
    site: Site,
    member_id: uuid.UUID,
    *,
    role_key: str | None,
    is_active: bool | None,
    actor: Actor,
    now: datetime,
) -> tuple[Member, bool]:
    """(üye, oturumları kapatıldı mı). `role_key` → ana rolü değiştir (geriye uyum): kişinin
    bitmemiş diğer tesis kapsamlı personel rolleri kapanır, bu rol yoksa atanır."""
    await roles_svc.lock_members(session, site)
    member = await get_member(session, site, member_id, now)
    if member.user.id == actor.user_id:
        raise MemberRuleError(
            "self_change", "Kendi rolünüzü değiştiremez, kendi erişiminizi kapatamazsınız."
        )
    membership = await session.get(SiteMembership, member_id, with_for_update=True)
    if membership is None:  # pragma: no cover
        raise LookupError
    closed = False
    async with roles_svc.guarded(session):
        if role_key is not None:
            catalog = await roles_svc.load_catalog(session)
            if catalog.by_key(role_key) is None or role_key == RESIDENT_KEY:
                raise FormFieldError({"role_key": "Rol seçin."})
            current = list(
                await session.scalars(
                    select(SiteRoleAssignment).where(
                        SiteRoleAssignment.user_id == member.user.id,
                        SiteRoleAssignment.role_key != RESIDENT_KEY,
                        SiteRoleAssignment.scope_kind == RoleScope.SITE.value,
                        roles_svc.not_ended(now),
                    )
                )
            )
            kept = [a for a in current if a.role_key == role_key]
            if not kept:
                await roles_svc.assign_role(
                    session, site, user_id=member.user.id,
                    data=roles_svc.AssignmentInput(role_key=role_key), actor_id=actor.user_id,
                    actor_permissions=actor.permissions, actor_name=actor.name, now=now,
                )  # fmt: skip
            for assignment in current:
                if assignment.role_key != role_key:
                    await roles_svc.end_assignment(
                        session, site, assignment, actor_id=actor.user_id,
                        actor_permissions=actor.permissions, actor_name=actor.name, now=now,
                    )  # fmt: skip
        if is_active is not None and is_active != membership.is_active:
            membership.is_active = is_active
            closed = not is_active
            if closed:
                await _revoke_sessions(session, member.user.id, now)
        try:
            await roles_svc.ensure_manager_remains(session, site, now)
        except RoleRuleError as exc:
            raise MemberRuleError(exc.code, exc.message) from exc
    return await get_member(session, site, member_id, now), closed
