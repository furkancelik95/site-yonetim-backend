"""Demo verisini yazar — docs/10 §1–2. **Yalnız geliştirme ortamında.**

Üretimde herkesçe bilinen `Demo1234!` parolalı hesap oluşmamalı (docs/02 §7, docs/09 §2):
ortam kontrolü burada, yazmadan önce yapılır. İdempotent: demo verisi varsa hiçbir şey yapmaz.
"""

import logging
import random
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import Environment, Settings
from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import UserKind
from site_yonetim.domain.charging.payments import PaymentMethod
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import (
    BudgetStatus,
    ExpenseCategoryKind,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.money import distribute
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.domain.text import normalize_person_name
from site_yonetim.models import (
    AccountBalance,
    AllocationRule,
    Block,
    BudgetItem,
    BudgetPlan,
    Charge,
    ChargeType,
    ExpenseCategory,
    LedgerAccount,
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
    BUDGET_ITEMS,
    CASH_SHARE,
    CHARGED_MONTHS,
    CURRENT_MONTH_FACTOR,
    DEMO_PASSWORD,
    LAST_MONTH_FACTOR,
    OLDER_MONTHS_BONUS,
    ORGANIZATION_NAME,
    ORGANIZATION_PLAN,
    ORGANIZATION_TAX_NUMBER,
    PAYMENT_DAYS,
    PLANS,
    RESIDENT,
    SEED,
    SITES,
    SiteSpec,
    occupancy_for,
    units_for,
)
from site_yonetim.services import charging, payments
from site_yonetim.services.charging import BUSINESS_TZ
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


async def seed_demo(
    settings: Settings, factory: async_sessionmaker[AsyncSession], *, today: date | None = None
) -> bool:
    """Demo verisini yükler. Yüklediyse True, zaten varsa False döner.

    Tahakkuklar `today`'e göre son 6 ay için kesilir (varsayılan: Türkiye saatiyle bugün).
    """
    ensure_demo_allowed(settings)
    today = today or datetime.now(BUSINESS_TZ).date()

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
                await _seed_finance(session, spec, today)
                session.add_all(
                    SiteMembership(user_id=user_ids[account.email], role=account.site_role)
                    for account in ACCOUNTS
                    if account.site_slug == spec.slug
                )
                await session.flush()  # kiracı satırları kapsam kapanmadan yazılmalı

    async with factory() as session:
        aksu = await session.scalar(select(Site.id).where(Site.slug == RESIDENT.site_slug))
    if aksu is not None:
        await _seed_resident(factory, aksu, password_hash)

    logger.info("Demo verisi yüklendi: %d site, %d hesap.", len(SITES), len(ACCOUNTS) + 1)
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


async def _seed_finance(session: AsyncSession, spec: SiteSpec, today: date) -> None:
    """Kesinleşmiş işletme projesi + son 6 ayın tahakkuku — tutarlar motordan (docs/10 §1.4)."""
    rules = {r.name: r.id for r in await session.scalars(select(AllocationRule))}
    types = {t.name: t for t in await session.scalars(select(ChargeType))}
    categories = {c.kind: c.id for c in await session.scalars(select(ExpenseCategory))}
    elevator_blocks = list(await session.scalars(select(Block.id).where(Block.has_elevator)))
    year = today.year
    plan = BudgetPlan(
        fiscal_year=year,
        name=f"{year} İşletme Projesi",
        status=BudgetStatus.FINALIZED.value,
        notified_on=date(year, 1, 2),
        objection_deadline=date(year, 1, 9),
        finalized_on=date(year, 1, 10),
    )
    session.add(plan)
    await session.flush()
    specs = [s for s in BUDGET_ITEMS if elevator_blocks or not s.elevator_only]
    annual = spec.monthly_budget * 12
    amounts = distribute(annual, [Decimal(s.percent) for s in specs])
    for order, (item, amount) in enumerate(zip(specs, amounts, strict=True)):
        charge_type = types[item.charge_type]
        owner_pays = charge_type.payer_rule == PayerRule.OWNER.value
        category = (
            ExpenseCategoryKind.CAPITAL_IMPROVEMENT if owner_pays else ExpenseCategoryKind.OPERATING
        )
        session.add(
            BudgetItem(
                budget_plan_id=plan.id,
                name=item.name,
                expense_category_id=categories[category.value],
                charge_type_id=charge_type.id,
                allocation_rule_id=rules[item.rule],
                annual_amount=amount,
                frequency=Frequency.MONTHLY.value,
                scope_kind=(ScopeKind.BLOCKS if item.elevator_only else ScopeKind.WHOLE_SITE).value,
                scope_block_ids=elevator_blocks if item.elevator_only else None,
                sort_order=order,
            )
        )
    await session.flush()

    months = [YearMonth.of(today)]
    while len(months) < CHARGED_MONTHS:
        months.insert(0, months[0].previous())
    runs = []
    for month in months:
        prepared = await charging.prepare(session, today, charge_date=month.first_day)
        posted_at = datetime.combine(month.first_day, time(9, 30), BUSINESS_TZ)
        runs.append(await charging.post(session, prepared, posted_by="Demo verisi", now=posted_at))
    await _seed_payments(session, spec, [r.run.id for r in runs], months, today)


def _pay_probability(rate: Decimal, months_ago: int) -> Decimal:
    if months_ago == 0:
        return rate * CURRENT_MONTH_FACTOR
    if months_ago == 1:
        return rate * LAST_MONTH_FACTOR
    return min(Decimal(1), rate + OLDER_MONTHS_BONUS)


async def _seed_payments(
    session: AsyncSession,
    spec: SiteSpec,
    run_ids: list[uuid.UUID],
    months: list[YearMonth],
    today: date,
) -> None:
    """Her borç, ayına göre olasılıkla tamamen ödenir; tahsilat gerçek servisten geçer (FIFO)."""
    rng = random.Random(f"{SEED}:payments:{spec.slug}")  # noqa: S311  # nosec B311
    accounts = {a.id: a for a in await session.scalars(select(LedgerAccount))}
    planned: list[tuple[date, uuid.UUID, Decimal, PaymentMethod]] = []
    for months_ago, (run_id, month) in enumerate(
        zip(reversed(run_ids), reversed(months), strict=True)
    ):
        chance = _pay_probability(spec.collection_rate, months_ago)
        charges = await session.scalars(
            select(Charge).where(Charge.charge_run_id == run_id).order_by(Charge.id)
        )
        for charge in charges:
            if Decimal(str(rng.random())) >= chance:
                continue
            day = min(month.first_day.replace(day=rng.randint(*PAYMENT_DAYS)), today)
            cash = rng.random() < CASH_SHARE
            method = PaymentMethod.CASH if cash else PaymentMethod.BANK_TRANSFER
            planned.append((day, charge.ledger_account_id, charge.amount, method))
    for day, account_id, amount, method in sorted(planned, key=lambda p: (p[0], p[1])):
        await payments.record_payment(
            session,
            accounts[account_id],
            amount=amount,
            day=day,
            method=method,
            reference=None,
            note=None,
            today=today,
            recorded_by="Demo verisi",
        )


async def _seed_resident(
    factory: async_sessionmaker[AsyncSession], site_id: uuid.UUID, password_hash: str
) -> None:
    """`sakin@demo.local`: Aksu'da en borçlu oturan hesabın kişisi (docs/10 §2)."""
    with site_scope(site_id):
        async with factory() as session:
            person_id = await session.scalar(
                select(LedgerAccount.person_id)
                .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
                .where(LedgerAccount.kind == AccountKind.OCCUPANT.value)
                .order_by(AccountBalance.balance.desc(), LedgerAccount.reference_code)
                .limit(1)
            )
            person = await session.get(Person, person_id)
    if person is None:  # pragma: no cover - demo sitesinde hep borçlu hesap var
        return
    async with factory() as session, session.begin():
        user = User(
            email=RESIDENT.email,
            password_hash=password_hash,
            full_name=f"{person.first_name} {person.last_name}",
            kind=UserKind.RESIDENT.value,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    with site_scope(site_id):
        async with factory() as session, session.begin():
            session.add(
                SiteMembership(user_id=user_id, role=RESIDENT.site_role, person_id=person.id)
            )
            linked = await session.get(Person, person.id)
            if linked is not None:
                linked.user_id = user_id


def _person(first: str, last: str) -> Person:
    first_name, last_name = normalize_person_name(first, last)
    return Person(first_name=first_name, last_name=last_name)
