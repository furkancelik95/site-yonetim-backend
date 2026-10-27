"""Kullanıcının çözümlenmiş erişimi — `UserAccess` (docs/05 §4, PLT-02).

Kullanıcının bütün sitelerdeki üyeliklerini okumak site kapsamını aşan bir işlemdir: tek
bilinçli `all_sites_scope()` kullanımı buradadır ve sorgu **yalnız o kullanıcının** satırlarıyla
(ve o sitelerin rol tanımlarıyla) sınırlıdır. İstek oturumu bu kapsama sabitlenmesin diye kendi
kısa oturumunu açar.

Her istekte o anda etkin rol atamaları okunur: süresi dolan atama bir sonraki istekte yetki
vermez, kaldırılan rol bir sonraki istekte düşer (PLT-09).
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.access import (
    TEMPLATE_KEYS,
    ExplicitMembership,
    OrganizationMembership,
    RoleGrant,
    RoleScope,
    SiteRef,
    SiteRole,
    UserAccess,
    resolve_access,
)
from site_yonetim.domain.roles import RoleCatalog, build_catalog
from site_yonetim.models import OrganizationMembership as OrganizationMembershipRow
from site_yonetim.models import (
    Site,
    SiteMembership,
    SiteRoleAssignment,
    SiteRoleDefinition,
    StaffMember,
    User,
)
from site_yonetim.services.roles import active_at, stored


def _ref(site: Site) -> SiteRef:
    return SiteRef(site.id, site.name, site.slug, site.organization_id)


async def _catalogs(
    session: AsyncSession, site_ids: set[uuid.UUID]
) -> dict[uuid.UUID, RoleCatalog]:
    rows: dict[uuid.UUID, list[SiteRoleDefinition]] = {s: [] for s in site_ids}
    if site_ids:
        for row in await session.scalars(
            select(SiteRoleDefinition).where(SiteRoleDefinition.site_id.in_(site_ids))
        ):
            rows[row.site_id].append(row)
    return {s: build_catalog(stored(r) for r in items) for s, items in rows.items()}


async def load_user_access(
    factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> UserAccess:
    """`today` verilirse (istek günü) ayrılış tarihi geçmiş personel kartına bağlı üyelik pasif
    sayılır — K29: gece işi üyeliği kapatmadan önce de erişim bir sonraki istekte kapanır.
    `now`: rol atamalarının etkinliği bu ana göre (verilmezse saat okunur)."""
    moment = now or datetime.now(UTC)
    explicit: list[ExplicitMembership] = []
    organizations: list[OrganizationMembership] = []
    catalogs: dict[uuid.UUID, RoleCatalog] = {}

    if user.is_active and not user.is_platform_admin:
        async with factory() as session:
            org_rows = (
                await session.scalars(
                    select(OrganizationMembershipRow).where(
                        OrganizationMembershipRow.user_id == user.id
                    )
                )
            ).all()
            org_ids = [row.organization_id for row in org_rows]
            org_sites: dict[object, list[SiteRef]] = {org_id: [] for org_id in org_ids}
            if org_ids:
                for site in await session.scalars(
                    select(Site).where(Site.organization_id.in_(org_ids))
                ):
                    org_sites[site.organization_id].append(_ref(site))
            organizations = [
                OrganizationMembership(
                    row.organization_id,
                    row.role,
                    row.is_active,
                    tuple(org_sites[row.organization_id]),
                )
                for row in org_rows
            ]

            with all_sites_scope():
                rows = (
                    await session.execute(
                        select(SiteMembership, Site)
                        .join(Site, Site.id == SiteMembership.site_id)
                        .where(SiteMembership.user_id == user.id)
                    )
                ).all()
                assignments = list(
                    await session.scalars(
                        select(SiteRoleAssignment).where(
                            SiteRoleAssignment.user_id == user.id, *active_at(moment)
                        )
                    )
                )
                site_ids = {site.id for _, site in rows} | {
                    ref.id for refs in org_sites.values() for ref in refs
                }
                catalogs = await _catalogs(session, site_ids)
                departed: set[object] = set()
                if today is not None:
                    departed = set(
                        await session.scalars(
                            select(StaffMember.site_id).where(
                                and_(
                                    StaffMember.user_id == user.id,
                                    StaffMember.end_date < today,
                                )
                            )
                        )
                    )
            grants: dict[uuid.UUID, list[RoleGrant]] = {}
            for a in assignments:
                role = catalogs[a.site_id].by_key(a.role_key) if a.site_id in catalogs else None
                if role is None or not role.is_active:
                    continue
                grants.setdefault(a.site_id, []).append(
                    role.grant(
                        scope_kind=RoleScope(a.scope_kind),
                        block_id=a.block_id,
                        gate_id=a.gate_id,
                        ends_at=a.ends_at,
                        assignment_id=a.id,
                    )
                )
            explicit = [
                ExplicitMembership(
                    _ref(site),
                    tuple(grants.get(site.id, ())),
                    membership.is_active and site.id not in departed,
                    membership.person_id,
                )
                for membership, site in rows
            ]

    def site_level(site_id: uuid.UUID, role: SiteRole) -> int | None:
        catalog = catalogs.get(site_id)
        found = catalog.by_key(TEMPLATE_KEYS[role]) if catalog else None
        return found.level if found else None

    return resolve_access(
        user_id=user.id,
        full_name=user.full_name,
        kind=user.kind,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        explicit=explicit,
        organizations=organizations,
        site_level=site_level,
    )
