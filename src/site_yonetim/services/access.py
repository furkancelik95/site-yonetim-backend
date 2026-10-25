"""Kullanıcının çözümlenmiş erişimi — `UserAccess` (docs/05 §4).

Kullanıcının bütün sitelerdeki üyeliklerini okumak site kapsamını aşan bir işlemdir: tek
bilinçli `all_sites_scope()` kullanımı buradadır ve sorgu **yalnız o kullanıcının** satırlarıyla
sınırlıdır. İstek oturumu bu kapsama sabitlenmesin diye kendi kısa oturumunu açar.
"""

from datetime import date

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.access import (
    ExplicitMembership,
    OrganizationMembership,
    SiteRef,
    UserAccess,
    resolve_access,
)
from site_yonetim.models import OrganizationMembership as OrganizationMembershipRow
from site_yonetim.models import Site, SiteMembership, StaffMember, User


def _ref(site: Site) -> SiteRef:
    return SiteRef(site.id, site.name, site.slug, site.organization_id)


async def load_user_access(
    factory: async_sessionmaker[AsyncSession], user: User, *, today: date | None = None
) -> UserAccess:
    """`today` verilirse (istek günü) ayrılış tarihi geçmiş personel kartına bağlı üyelik pasif
    sayılır — K29: gece işi üyeliği kapatmadan önce de erişim bir sonraki istekte kapanır."""
    explicit: list[ExplicitMembership] = []
    organizations: list[OrganizationMembership] = []

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
            explicit = [
                ExplicitMembership(
                    _ref(site),
                    membership.role,
                    membership.is_active and site.id not in departed,
                    membership.person_id,
                )
                for membership, site in rows
            ]

    return resolve_access(
        user_id=user.id,
        full_name=user.full_name,
        kind=user.kind,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        explicit=explicit,
        organizations=organizations,
    )
