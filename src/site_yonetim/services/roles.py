"""Roller ve rol atamaları — PLT-09, P18, K20. Açık site kapsamında; transaction'ı çağıran yönetir.

- Rol kataloğu: hazır şablonlar (kod) + sitenin şablon düzeyleri + siteye özel roller
  (`site_roles`).
- Rol ataması: kişi × rol × kapsam (tesis/blok/kapı) × (başlangıç–bitiş). Silinmez; kaldırma
  bitişi "şimdi" yapar. Etkin: başlangıç ≤ şimdi < bitiş.
- Atayan, kendi sahip olmadığı izni içeren rolü atayamaz/kaldıramaz/tanımlayamaz (403).
- Sitede `members.manage` izni olan en az bir etkin kullanıcı kalır (son yönetici).
- `assign_role` onay yürütücüsünün de çağırabileceği tek giriştir (P16b: `role_change` — kritik
  izinli rol ataması onaydan geçecek; yürütücü `check_actor=False` ile çağırır).
"""

import uuid
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import (
    ORGANIZATION_ROLE_TO_SITE_ROLE,
    TEMPLATE_KEYS,
    OrganizationRole,
    Permission,
    RoleScope,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.roles import (
    EXCEEDS_ASSIGN,
    EXCEEDS_DEFINE,
    EXCEEDS_REMOVE,
    RESIDENT_KEY,
    TEMPLATE_BY_KEY,
    RoleCatalog,
    RoleDef,
    RoleKind,
    RoleRuleError,
    StoredRole,
    build_catalog,
    check_role_fields,
    exceeding,
    periods_overlap,
)
from site_yonetim.models import (
    Block,
    OrganizationMembership,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    SiteRoleDefinition,
    User,
)

_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
LAST_MANAGER = "Sitede en az bir etkin yönetici kalmalı."


async def lock_members(session: AsyncSession, site: Site) -> None:
    """Üyelik/rol değişiklikleri site başına sıraya girer (son yönetici kontrolü yarışmasın)."""
    await session.execute(_LOCK, {"key": f"members:{site.id}"})


def stored(row: SiteRoleDefinition) -> StoredRole:
    return StoredRole(
        id=row.id,
        key=row.key,
        name=row.name,
        description=row.description,
        base_key=row.base_key,
        is_system=row.is_system,
        permissions=tuple(row.permissions) if row.permissions is not None else None,
        level=row.level,
        is_active=row.is_active,
    )


async def load_catalog(session: AsyncSession) -> RoleCatalog:
    return build_catalog(stored(r) for r in await session.scalars(select(SiteRoleDefinition)))


def active_at(now: datetime) -> list[ColumnElement[bool]]:
    """Atama `now` anında etkin: başlangıç ≤ şimdi < bitiş."""
    return [
        or_(SiteRoleAssignment.starts_at.is_(None), SiteRoleAssignment.starts_at <= now),
        or_(SiteRoleAssignment.ends_at.is_(None), SiteRoleAssignment.ends_at > now),
    ]


def not_ended(now: datetime) -> ColumnElement[bool]:
    """Bitmemiş (etkin ya da ileri tarihli) atama."""
    return or_(SiteRoleAssignment.ends_at.is_(None), SiteRoleAssignment.ends_at > now)


# --- Rol sahipleri --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Holder:
    """Etkin kullanıcı ve şu an etkin **tesis kapsamlı** rolleri (onay, son yönetici, bildirim)."""

    user: User
    roles: tuple[RoleDef, ...]  # düzey sırasıyla
    source: str  # site · organization

    @property
    def permissions(self) -> frozenset[Permission]:
        return frozenset().union(*(r.permissions for r in self.roles))


async def holders(
    session: AsyncSession, site: Site, now: datetime, catalog: RoleCatalog | None = None
) -> list[Holder]:
    catalog = catalog or await load_catalog(session)
    rows = (
        await session.execute(
            select(SiteRoleAssignment, User)
            .join(User, User.id == SiteRoleAssignment.user_id)
            .join(SiteMembership, SiteMembership.user_id == SiteRoleAssignment.user_id)
            .where(
                SiteMembership.is_active,
                User.is_active,
                SiteRoleAssignment.scope_kind == RoleScope.SITE.value,
                *active_at(now),
            )
        )
    ).all()
    by_user: dict[uuid.UUID, tuple[User, list[RoleDef]]] = {}
    for assignment, user in rows:
        role = catalog.by_key(assignment.role_key)
        if role is None or not role.is_active:
            continue
        entry = by_user.setdefault(user.id, (user, []))
        if role not in entry[1]:
            entry[1].append(role)
    order = {r.key: i for i, r in enumerate(catalog.roles)}
    result = [
        Holder(user, tuple(sorted(roles, key=lambda r: order[r.key])), "site")
        for user, roles in by_user.values()
    ]
    if site.organization_id is not None:
        explicit = set(await session.scalars(select(SiteMembership.user_id)))
        for membership, user in await session.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.organization_id == site.organization_id,
                OrganizationMembership.is_active,
                User.is_active,
            )
        ):
            if user.id in explicit:
                continue  # açık üyelik türetilmişi ezer
            template = ORGANIZATION_ROLE_TO_SITE_ROLE[OrganizationRole(membership.role)]
            role = catalog.by_key(TEMPLATE_KEYS[template])
            if role is not None:
                result.append(Holder(user, (role,), "organization"))
    return sorted(result, key=lambda h: (h.user.full_name.casefold(), h.user.id))


async def role_holders(
    session: AsyncSession, site: Site, names: Iterable[str], now: datetime
) -> list[tuple[uuid.UUID, str, str]]:
    """Adı verilen rollerden birine sahip etkin kullanıcılar — kişi başına bir satır
    (kullanıcı, ad, eşleşen en üst rolün adı). Onay adımları buna göre çözülür."""
    wanted = {n.casefold() for n in names}
    found: list[tuple[uuid.UUID, str, str]] = []
    for holder in await holders(session, site, now):
        match = next((r for r in holder.roles if r.name.casefold() in wanted), None)
        if match is not None:
            found.append((holder.user.id, holder.user.full_name, match.name))
    return found


async def permission_holders(
    session: AsyncSession, site: Site, permission: Permission, now: datetime
) -> set[uuid.UUID]:
    return {h.user.id for h in await holders(session, site, now) if permission in h.permissions}


@asynccontextmanager
async def guarded(session: AsyncSession) -> AsyncIterator[None]:
    """Kural ihlalinde (ör. son yönetici) yalnız bu bloktaki değişiklikler geri alınır — çağıran
    istisnayı yakalayıp transaction'ı sürdürse bile yarım değişiklik kalmaz (SAVEPOINT)."""
    savepoint = await session.begin_nested()
    try:
        yield
    except BaseException:
        await savepoint.rollback()
        raise
    await savepoint.commit()


async def ensure_manager_remains(session: AsyncSession, site: Site, now: datetime) -> None:
    """Değişiklik flush edildikten sonra çağrılır; kimse `members.manage` tutmuyorsa 409."""
    await session.flush()
    if not await permission_holders(session, site, Permission.MEMBERS_MANAGE, now):
        raise RoleRuleError(409, "last_manager", LAST_MANAGER)


# --- Rol tanımları ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleInput:
    base_key: str | None = None
    name: str | None = None
    description: str | None = None
    permissions: list[str] | None = None
    level: int | None = None


def _custom_key() -> str:
    return f"custom-{uuid.uuid7().hex[-12:]}"


async def create_role(
    session: AsyncSession,
    site: Site,
    data: RoleInput,
    *,
    actor_permissions: frozenset[Permission],
    actor_name: str | None,
) -> RoleDef:
    """ "Kopyala ve özelleştir": şablonun (ya da başka özel rolün) kopyası; izinler verilmezse
    kaynağınkiler, düzey verilmezse kaynağınki."""
    await lock_members(session, site)
    catalog = await load_catalog(session)
    base = catalog.by_key(data.base_key or "")
    if base is None or base.key == RESIDENT_KEY:
        raise FormFieldError({"base_key": "Kopyalanacak rolü seçin."})
    permissions = (
        data.permissions
        if data.permissions is not None
        else sorted(p.value for p in base.permissions)
    )
    name, description, perms, level, errors = check_role_fields(
        name=data.name,
        description=data.description if data.description is not None else base.description,
        permissions=permissions,
        level=data.level if data.level is not None else base.level,
        catalog=catalog,
    )
    if errors:
        raise FormFieldError(errors)
    if exceeding(actor_permissions, perms):
        raise RoleRuleError(403, "exceeds_own_permissions", EXCEEDS_DEFINE)
    row = SiteRoleDefinition(
        key=_custom_key(),
        name=name,
        description=description or None,
        base_key=base.key,
        is_system=False,
        permissions=sorted(p.value for p in perms),
        level=level,
        is_active=True,
        created_by_name=actor_name,
    )
    session.add(row)
    await session.flush()
    role = build_catalog([stored(row)]).by_key(row.key)
    if role is None:  # pragma: no cover
        raise LookupError
    return role


async def _active_holder_count(session: AsyncSession, key: str, now: datetime) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(SiteRoleAssignment)
        .where(SiteRoleAssignment.role_key == key, not_ended(now))
    )
    return count or 0


async def update_role(
    session: AsyncSession,
    site: Site,
    key: str,
    data: RoleInput,
    *,
    is_active: bool | None,
    actor_permissions: frozenset[Permission],
    actor_name: str | None,
    now: datetime,
) -> RoleDef:
    """Özel rol: açıklama, izinler, düzey, arşiv. Hazır şablon: yalnız düzey (K20)."""
    await lock_members(session, site)
    catalog = await load_catalog(session)
    role = catalog.by_key(key)
    if role is None:
        raise LookupError
    if role.kind is RoleKind.SYSTEM:
        if data.permissions is not None or data.description is not None or is_active is not None:
            raise RoleRuleError(
                409,
                "system_role",
                "Hazır rol şablonu değiştirilemez; yalnız düzeyi değişir. "
                "Farklı izinler için kopyalayıp özel rol oluşturun.",
            )
        if role.key == RESIDENT_KEY:
            raise RoleRuleError(409, "system_role", "Sakin rolünün düzeyi değiştirilemez.")
        _, _, _, level, errors = check_role_fields(
            name=role.name, description=None, permissions=["members.manage"],
            level=data.level, catalog=catalog, renaming=False,
        )  # fmt: skip
        if "level" in errors:
            raise FormFieldError({"level": errors["level"]})
        row = await session.scalar(
            select(SiteRoleDefinition).where(SiteRoleDefinition.key == role.key)
        )
        if row is None:
            row = SiteRoleDefinition(
                key=role.key, name=role.name, is_system=True, permissions=None, level=level,
                is_active=True, created_by_name=actor_name,
            )  # fmt: skip
            session.add(row)
        else:
            row.level = level
        await session.flush()
    else:
        row = await session.scalar(
            select(SiteRoleDefinition).where(SiteRoleDefinition.key == role.key)
        )
        if row is None:  # pragma: no cover
            raise LookupError
        permissions = (
            data.permissions if data.permissions is not None else list(row.permissions or [])
        )
        _, description, perms, level, errors = check_role_fields(
            name=role.name,
            description=data.description if data.description is not None else row.description,
            permissions=permissions,
            level=data.level if data.level is not None else row.level,
            catalog=catalog,
            renaming=False,
        )
        if errors:
            raise FormFieldError(errors)
        # Hem eski hem yeni izin seti atayanın yetkisi içinde olmalı (yükseltme/düşürme).
        if exceeding(actor_permissions, perms | role.permissions):
            raise RoleRuleError(403, "exceeds_own_permissions", EXCEEDS_DEFINE)
        if (
            is_active is False
            and role.is_active
            and await _active_holder_count(session, role.key, now)
        ):
            raise RoleRuleError(
                409, "role_in_use", "Bu rolde etkin atama var; önce atamaları kaldırın."
            )
        async with guarded(session):
            row.description = description or None
            row.permissions = sorted(p.value for p in perms)
            row.level = level
            if is_active is not None:
                row.is_active = is_active
            await session.flush()
            await ensure_manager_remains(session, site, now)
    updated = (await load_catalog(session)).by_key(role.key)
    if updated is None:  # pragma: no cover
        raise LookupError
    return updated


# --- Atamalar ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssignmentInput:
    role_key: str | None
    scope_kind: str | None = RoleScope.SITE.value
    block_id: uuid.UUID | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None


async def _check_assignment(
    session: AsyncSession, catalog: RoleCatalog, data: AssignmentInput, now: datetime
) -> tuple[RoleDef, RoleScope]:
    errors: dict[str, str] = {}
    role = catalog.by_key(data.role_key or "")
    if role is None or not role.is_active or not role.is_staff:
        errors["role_key"] = "Rol seçin."
    try:
        scope = RoleScope(data.scope_kind or RoleScope.SITE.value)
    except ValueError:
        scope = RoleScope.SITE
        errors["scope_kind"] = "Kapsamı seçin: tesis geneli ya da blok."
    if scope is RoleScope.GATE:
        errors["scope_kind"] = "Kapı tanımları henüz yok; tesis geneli ya da blok seçin."
    elif scope is RoleScope.BLOCK:
        block = await session.get(Block, data.block_id) if data.block_id else None
        if block is None:
            errors["block_id"] = "Blok seçin."
    elif data.block_id is not None:
        errors["block_id"] = "Tesis geneli atamada blok seçilmez."
    if data.starts_at and data.ends_at and data.ends_at <= data.starts_at:
        errors["ends_at"] = "Bitiş başlangıçtan sonra olmalı."
    elif data.ends_at and data.ends_at <= now:
        errors["ends_at"] = "Bitiş ileri bir zaman olmalı."
    if errors or role is None:
        raise FormFieldError(errors)
    return role, scope


async def assign_role(
    session: AsyncSession,
    site: Site,
    *,
    user_id: uuid.UUID,
    data: AssignmentInput,
    actor_id: uuid.UUID | None,
    actor_permissions: frozenset[Permission],
    actor_name: str | None,
    now: datetime,
    check_actor: bool = True,
) -> SiteRoleAssignment:
    """Kişiye rol ata (PLT-09). Üyelik yoksa açılır. `check_actor=False` yalnız onaylanmış
    "Rol değişikliği" talebinin yürütücüsü içindir (izin kontrolü talep açılırken yapılmıştır)."""
    await lock_members(session, site)
    catalog = await load_catalog(session)
    role, scope = await _check_assignment(session, catalog, data, now)
    if check_actor:
        if actor_id == user_id:
            raise RoleRuleError(
                409, "self_change", "Kendi rolünüzü değiştiremez, kendinize rol atayamazsınız."
            )
        if exceeding(actor_permissions, role.permissions):
            raise RoleRuleError(403, "exceeds_own_permissions", EXCEEDS_ASSIGN)
    membership = await session.scalar(
        select(SiteMembership).where(SiteMembership.user_id == user_id)
    )
    if membership is None:
        session.add(SiteMembership(user_id=user_id, is_active=True))
    same = await session.scalars(
        select(SiteRoleAssignment).where(
            SiteRoleAssignment.user_id == user_id,
            SiteRoleAssignment.role_key == role.key,
            SiteRoleAssignment.scope_kind == scope.value,
            SiteRoleAssignment.block_id.is_(None)
            if data.block_id is None or scope is RoleScope.SITE
            else SiteRoleAssignment.block_id == data.block_id,
            not_ended(now),
        )
    )
    starts = data.starts_at if data.starts_at and data.starts_at > now else None
    if any(periods_overlap(a.starts_at, a.ends_at, starts, data.ends_at) for a in same):
        raise RoleRuleError(
            409, "assignment_exists", "Bu kişide bu rol bu kapsam ve tarihlerde zaten var."
        )
    assignment = SiteRoleAssignment(
        user_id=user_id,
        role_key=role.key,
        scope_kind=scope.value,
        block_id=data.block_id if scope is RoleScope.BLOCK else None,
        starts_at=starts,
        ends_at=data.ends_at,
        assigned_by_name=actor_name,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def end_assignment(
    session: AsyncSession,
    site: Site,
    assignment: SiteRoleAssignment,
    *,
    actor_id: uuid.UUID | None,
    actor_permissions: frozenset[Permission],
    actor_name: str | None,
    now: datetime,
    check_actor: bool = True,
) -> None:
    """Rol kaldırma: bitiş = şimdi (ileri tarihli atamada başlangıç); geçmiş kalır (PLT-09)."""
    await lock_members(session, site)
    if assignment.ends_at is not None and assignment.ends_at <= now:
        raise RoleRuleError(409, "assignment_ended", "Bu rol ataması zaten sona ermiş.")
    if check_actor:
        if actor_id == assignment.user_id:
            raise RoleRuleError(
                409, "self_change", "Kendi rolünüzü değiştiremez, kendi erişiminizi kapatamazsınız."
            )
        role = (await load_catalog(session)).by_key(assignment.role_key)
        if role is not None and exceeding(actor_permissions, role.permissions):
            raise RoleRuleError(403, "exceeds_own_permissions", EXCEEDS_REMOVE)
    end = now
    if assignment.starts_at is not None and assignment.starts_at > now:
        end = assignment.starts_at  # hiç başlamadan biter
    async with guarded(session):
        assignment.ends_at = end
        assignment.removed_at = now
        assignment.removed_by_name = actor_name
        await ensure_manager_remains(session, site, now)


def template_description(key: str) -> str:
    template = TEMPLATE_BY_KEY.get(key)
    return template.description if template else ""
