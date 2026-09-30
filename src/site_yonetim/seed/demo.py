"""Demo verisini yazar — docs/10 §1–2. **Yalnız geliştirme ortamında.**

Üretimde herkesçe bilinen `Demo1234!` parolalı hesap oluşmamalı (docs/02 §7, docs/09 §2):
ortam kontrolü burada, yazmadan önce yapılır. İdempotent: demo verisi varsa hiçbir şey yapmaz.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import Environment, Settings
from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import normalize_person_name
from site_yonetim.models import (
    Block,
    Organization,
    OrganizationMembership,
    Person,
    Plan,
    Site,
    SiteMembership,
    Unit,
    UnitType,
    User,
)
from site_yonetim.seed.demo_data import (
    ACCOUNTS,
    DEMO_PASSWORD,
    ORGANIZATION_NAME,
    ORGANIZATION_PLAN,
    ORGANIZATION_TAX_NUMBER,
    PLANS,
    SITES,
    SiteSpec,
    occupancy_for,
    units_for,
)
from site_yonetim.services.provisioning import provision_site
from site_yonetim.services.sites import set_module_enabled
from site_yonetim.services.structure import add_party

logger = logging.getLogger(__name__)


class DemoSeedRefusedError(RuntimeError):
    """Demo verisi bu ortamda yüklenemez."""


def ensure_demo_allowed(settings: Settings) -> None:
    if settings.environment is Environment.PRODUCTION or not settings.demo_data_allowed:
        raise DemoSeedRefusedError(
            "Demo verisi yalnız ENVIRONMENT=development ve SEED_DEMO_DATA=true iken yüklenir."
        )


async def seed_demo(settings: Settings, factory: async_sessionmaker[AsyncSession]) -> bool:
    """Demo verisini yükler. Yüklediyse True, zaten varsa False döner."""
    ensure_demo_allowed(settings)

    async with factory() as session:
        if await session.scalar(
            select(Organization.id).where(Organization.name == ORGANIZATION_NAME)
        ):
            logger.info("Demo verisi zaten var; atlandı.")
            return False

    # 1) Global: planlar, şirket, kullanıcılar, şirket üyelikleri
    password_hash = hash_password(DEMO_PASSWORD)
    async with factory() as session, session.begin():
        plans = {spec.name: Plan(name=spec.name, max_units=spec.max_units,
                                 max_storage_mb=spec.max_storage_mb,
                                 allowed_modules=[m.value for m in spec.modules],
                                 sort_order=spec.sort_order)
                 for spec in PLANS}  # fmt: skip
        session.add_all(plans.values())
        await session.flush()
        organization = Organization(
            name=ORGANIZATION_NAME,
            tax_number=ORGANIZATION_TAX_NUMBER,
            plan_id=plans[ORGANIZATION_PLAN].id,
        )
        session.add(organization)
        users = {
            spec.email: User(
                email=spec.email,
                password_hash=password_hash,
                full_name=spec.full_name,
                is_platform_admin=spec.is_platform_admin,
            )
            for spec in ACCOUNTS
        }
        session.add_all(users.values())
        await session.flush()
        session.add_all(
            OrganizationMembership(
                organization_id=organization.id, user_id=users[spec.email].id,
                role=spec.organization_role,
            )
            for spec in ACCOUNTS
            if spec.organization_role
        )  # fmt: skip
        plan_ids = {name: plan.id for name, plan in plans.items()}
        org_id = organization.id
        user_ids = {email: user.id for email, user in users.items()}

    # 2) Her site kendi oturumunda (oturum tek site kapsamına sabitlenir)
    for spec in SITES:
        async with factory() as session, session.begin():
            site = await provision_site(
                session,
                name=spec.name,
                slug=spec.slug,
                plan_id=plan_ids[spec.plan],
                organization_id=org_id,
                property_kind=spec.property_kind,
                city=spec.city,
                district=spec.district,
                iban=spec.iban,
            )
            with site_scope(site.id):
                await _seed_structure(session, site, spec)
                for key in spec.extra_modules:
                    await set_module_enabled(session, site, key, enable=True)
                session.add_all(
                    SiteMembership(user_id=user_ids[account.email], role=account.site_role)
                    for account in ACCOUNTS
                    if account.site_slug == spec.slug
                )
                await session.flush()  # kiracı satırları kapsam kapanmadan yazılmalı

    logger.info("Demo verisi yüklendi: %d site, %d hesap.", len(SITES), len(ACCOUNTS))
    return True


async def _seed_structure(session: AsyncSession, site: Site, spec: SiteSpec) -> None:
    types = {t.name: t.id for t in await session.scalars(select(UnitType))}
    blocks = {}
    for order, block_spec in enumerate(spec.blocks):
        block = Block(
            name=block_spec.name,
            has_elevator=block_spec.has_elevator,
            floor_count=block_spec.floor_count,
            sort_order=order,
        )
        session.add(block)
        blocks[block_spec.name] = block
    await session.flush()

    units, denominator = units_for(spec)
    session.add_all(
        Unit(
            block_id=blocks[unit.block].id,
            number=unit.number,
            floor=unit.floor,
            unit_type_id=types[unit.unit_type],
            gross_area=unit.gross_area,
            net_area=unit.net_area,
            land_share_numerator=unit.land_share_numerator,
            land_share_denominator=denominator,
            usage=unit.usage.value,
            commercial_title=unit.commercial_title,
        )
        for unit in units
    )
    await session.flush()
    logger.info("%s: %d blok, %d bölüm", site.name, len(blocks), len(units))
    await _seed_people(session, spec, blocks)


async def _seed_people(session: AsyncSession, spec: SiteSpec, blocks: dict[str, Block]) -> None:
    units = {(unit.block_id, unit.number): unit for unit in await session.scalars(select(Unit))}
    for occ in occupancy_for(spec):
        block = blocks[occ.block]
        unit = units[(block.id, occ.number)]
        owner = _person(*occ.owner)
        session.add(owner)
        await session.flush()
        await add_party(session, unit=unit, block=block, person=owner, role=PartyRole.OWNER,
                        start_date=occ.owner_since)  # fmt: skip
        if occ.tenant is not None and occ.tenant_since is not None:
            tenant = _person(*occ.tenant)
            session.add(tenant)
            await session.flush()
            await add_party(session, unit=unit, block=block, person=tenant,
                            role=PartyRole.TENANT, start_date=occ.tenant_since)  # fmt: skip


def _person(first: str, last: str) -> Person:
    first_name, last_name = normalize_person_name(first, last)
    return Person(first_name=first_name, last_name=last_name)
