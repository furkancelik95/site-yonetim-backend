"""Demo verisini yazar — docs/10 §1–2. **Yalnız geliştirme ortamında.**

Üretimde herkesçe bilinen `Demo1234!` parolalı hesap oluşmamalı (docs/02 §7, docs/09 §2):
ortam kontrolü burada, yazmadan önce yapılır. İdempotent: demo verisi varsa hiçbir şey yapmaz.
"""

import base64
import logging
import random
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import Environment, Settings
from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import UserKind
from site_yonetim.domain.cash import CashSource
from site_yonetim.domain.charging.payments import PaymentMethod
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.facility import add_months
from site_yonetim.domain.finance import (
    BudgetStatus,
    ExpenseCategoryKind,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.invitations import token_hash
from site_yonetim.domain.management import Employer
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.money import distribute, round_money
from site_yonetim.domain.operations import (
    Audience,
    Importance,
    RequestPriority,
    RequestStatus,
)
from site_yonetim.domain.parking import check_space
from site_yonetim.domain.patrol import check_plan, check_point
from site_yonetim.domain.roles import RESIDENT_KEY, TEMPLATE_BY_NAME
from site_yonetim.domain.security import VisitorKind
from site_yonetim.domain.service_desk import PermitType
from site_yonetim.domain.staffing import week_start
from site_yonetim.domain.structure import AccountKind, CommonAreaKind, PartyRole
from site_yonetim.domain.text import ascii_fold, normalize_person_name
from site_yonetim.domain.vehicles import VehicleKind, VehicleStatus
from site_yonetim.models import (
    AccountBalance,
    AllocationRule,
    Announcement,
    Block,
    BudgetItem,
    BudgetPlan,
    CashAccount,
    Charge,
    ChargeType,
    CommonArea,
    CustomField,
    ExpenseCategory,
    HouseholdMember,
    Invitation,
    KeyLoan,
    LedgerAccount,
    Organization,
    OrganizationMembership,
    Person,
    Plan,
    Request,
    ServiceTariff,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    StaffMember,
    StructureSettings,
    Unit,
    UnitParty,
    UnitType,
    User,
    Vehicle,
)
from site_yonetim.models.audit import bulk_operation
from site_yonetim.seed.demo_data import (
    ACCOUNTS,
    ANNOUNCEMENTS,
    BANK_OPENING_FACTOR,
    BUDGET_ITEMS,
    CASH_OPENING,
    CASH_SHARE,
    CHARGED_MONTHS,
    CURRENT_MONTH_FACTOR,
    DEMO_PASSWORD,
    DOCUMENTS,
    EXPENSES,
    FACILITY_KEYS,
    INSURANCE_POLICIES,
    LAST_MONTH_FACTOR,
    MAINTENANCE_PLANS,
    OLDER_MONTHS_BONUS,
    ORGANIZATION_NAME,
    ORGANIZATION_PLAN,
    ORGANIZATION_TAX_NUMBER,
    PAYMENT_DAYS,
    PLANS,
    REQUESTS,
    RESIDENT,
    RESIDENT_DOCUMENT,
    SEED,
    SHIFT_TEMPLATES,
    SITES,
    STAFF_CARDS,
    TARIFFS,
    TASK_TEMPLATES,
    TECHNICAL_ASSETS,
    UNPAID_SHARE_THIS_MONTH,
    DocumentSpec,
    SiteSpec,
    occupancy_for,
    units_for,
)
from site_yonetim.seed.demo_finance_ops import seed_finance_ops
from site_yonetim.services import (
    announcements,
    cash,
    charging,
    documents,
    expenses,
    gatehouse,
    paid_services,
    parking,
    patrol,
    payments,
    permits,
    requests,
    security,
    service_desk,
)
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import checklists as checklists_svc
from site_yonetim.services import communication as comm_svc
from site_yonetim.services import insurance as insurance_svc
from site_yonetim.services import inventory as inventory_svc
from site_yonetim.services import keys as keys_svc
from site_yonetim.services import leaves as leaves_svc
from site_yonetim.services import meters as meters_svc
from site_yonetim.services import routine_tasks as tasks_svc
from site_yonetim.services import shifts as shifts_svc
from site_yonetim.services import staff as staff_svc
from site_yonetim.services import technical_assets as assets_svc
from site_yonetim.services import timesheets as timesheets_svc
from site_yonetim.services import transparency as transparency_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.facility_common import PhotoSink
from site_yonetim.services.files import FileStore
from site_yonetim.services.provisioning import provision_site
from site_yonetim.services.sites import available_modules, set_module_enabled
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
    Toplu iş: binlerce satır denetim kaydına tek tek yazılmaz (`bulk_operation`).
    """
    with bulk_operation():
        return await _seed_demo(settings, factory, today=today)


async def _seed_demo(
    settings: Settings, factory: async_sessionmaker[AsyncSession], *, today: date | None
) -> bool:
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
    store = FileStore(settings.file_storage_root)
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
                await _seed_operations(session, site, spec, today)
                await _seed_communication(session, site, today)
                await _seed_gate(session, spec, today)
                await _seed_directory(session, spec, today)
                await _seed_security_setup(session, spec, today)
                first_month = YearMonth.of(today)
                for _ in range(CHARGED_MONTHS - 1):
                    first_month = first_month.previous()
                await seed_finance_ops(session, site, first_month, today)
                if ModuleKey.DOCUMENTS in await available_modules(session, site):
                    await _seed_documents(session, store, today)
                for account in ACCOUNTS:
                    if account.site_slug == spec.slug and account.site_role:
                        user_id = user_ids[account.email]
                        session.add(SiteMembership(user_id=user_id))
                        session.add(
                            SiteRoleAssignment(
                                user_id=user_id,
                                role_key=TEMPLATE_BY_NAME[account.site_role].key,
                                assigned_by_name="Demo verisi",
                            )
                        )
                await session.flush()  # kiracı satırları kapsam kapanmadan yazılmalı

    async with factory() as session:
        aksu = await session.scalar(select(Site.id).where(Site.slug == RESIDENT.site_slug))
    if aksu is not None:
        await _seed_resident(factory, aksu, password_hash, store, today)
        await _seed_staff(factory, aksu, user_ids, today)
        await _seed_facility(factory, aksu, user_ids, store, today)

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
    banks = await _seed_cash_accounts(session, spec, months[0].first_day)
    await _seed_payments(session, spec, [r.run.id for r in runs], months, today, banks)
    await _seed_expenses(session, spec, months, today, banks)


async def _seed_cash_accounts(
    session: AsyncSession, spec: SiteSpec, opened_on: date
) -> dict[PaymentMethod, CashAccount]:
    """Site açılışındaki iki hesaba açılış bakiyesi. Açılış demo geçmişinin başında: ekstrede
    tüm tahsilat ve giderler açılıştan sonra görünsün (docs/10 §1.4 "3 ay önce" yerine)."""
    accounts = {a.name: a for a in await session.scalars(select(CashAccount))}
    bank, till = accounts["Banka Hesabı"], accounts["Kasa"]
    for account, opening in (
        (bank, spec.monthly_budget * BANK_OPENING_FACTOR),
        (till, CASH_OPENING),
    ):
        account.opening_balance = opening
        account.opening_date = opened_on
        await cash.add_movement(
            session, account, day=opened_on, inflow=opening, description="Açılış bakiyesi",
            source=CashSource.OPENING, created_by="Demo verisi",
        )  # fmt: skip
    return {PaymentMethod.CASH: till, PaymentMethod.BANK_TRANSFER: bank}


async def _seed_expenses(
    session: AsyncSession,
    spec: SiteSpec,
    months: list[YearMonth],
    today: date,
    accounts: dict[PaymentMethod, CashAccount],
) -> None:
    """Son 6 ayın giderleri; bu ayın ~üçte biri ödenmemiş, ödenenler Banka'dan düşer."""
    rng = random.Random(f"{SEED}:expenses:{spec.slug}")  # noqa: S311  # nosec B311
    categories = {c.kind: c.id for c in await session.scalars(select(ExpenseCategory))}
    has_elevator = any(block.has_elevator for block in spec.blocks)
    bank = accounts[PaymentMethod.BANK_TRANSFER]
    for months_ago, month in enumerate(reversed(months)):
        for item in EXPENSES:
            if item.elevator_only and not has_elevator:
                continue
            if item.once_months_ago is not None and item.once_months_ago != months_ago:
                continue
            factor = Decimal(1) + Decimal(rng.randint(-5, 5)) / 100
            amount = round_money(spec.monthly_budget * item.share * factor)
            day = min(month.first_day.replace(day=rng.randint(3, 20)), today)
            unpaid = months_ago == 0 and rng.random() < UNPAID_SHARE_THIS_MONTH
            paid_on = min(day + timedelta(days=rng.randint(0, 5)), today)
            kind = (
                ExpenseCategoryKind.CAPITAL_IMPROVEMENT if item.capital
                else ExpenseCategoryKind.OPERATING
            )  # fmt: skip
            await expenses.create(
                session,
                expenses.NewExpense(
                    expense_category_id=categories[kind.value],
                    description=item.description,
                    amount=amount,
                    day=day,
                    vendor=item.vendor,
                    document_number=f"F{month.year}{month.month:02d}-{rng.randint(1000, 9999)}",
                    paid=not unpaid,
                    paid_on=None if unpaid else paid_on,
                    cash_account_id=None if unpaid else bank.id,
                ),
                today=today,
                created_by="Demo verisi",
            )


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
    cash_accounts: dict[PaymentMethod, CashAccount],
) -> None:
    """Her borç, ayına göre olasılıkla tamamen ödenir; tahsilat gerçek servisten geçer (FIFO)
    ve paranın girdiği hesaba yazılır: nakit Kasa'ya, havale Banka'ya."""
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
            cash_account_id=cash_accounts[method].id,
        )


async def _seed_operations(session: AsyncSession, site: Site, spec: SiteSpec, today: date) -> None:
    """Duyurular ve farklı durumdaki talepler — servislerden geçer (docs/10 §1.4)."""
    for item in ANNOUNCEMENTS:
        published_on = today - timedelta(days=item.days_ago)
        await announcements.publish(
            session,
            announcements.NewAnnouncement(
                title=item.title,
                body=item.body,
                importance=Importance(item.importance),
                audience=Audience.ALL_RESIDENTS,
                block_ids=(),
                channels=(),
                expires_on=None,
                is_pinned=item.is_pinned,
            ),
            published_by="Site Yönetimi",
            now=datetime.combine(published_on, time(10), BUSINESS_TZ),
            today=published_on,
        )
    # Süreli duyuruların bitişi sonradan yazılır: geçmiş tarihli yayın doğrulamaya takılmasın.
    for item in ANNOUNCEMENTS:
        if item.valid_days is not None:
            await session.execute(
                update(Announcement)
                .where(Announcement.title == item.title)
                .values(expires_on=today - timedelta(days=item.days_ago - item.valid_days))
            )

    await _seed_requests(session, site, today)


async def _seed_requests(session: AsyncSession, site: Site, today: date) -> None:
    """Talepler dokümandaki akışla (K18): Açık → Atandı → İşlemde → Beklemede → Çözüldü →
    (sakin onayıyla) Kapandı. Zamanlar açılıştan sonra ve sıralı; çözüldü talep sakin onayı
    beklesin diye dün çözülür (otomatik kapanış süresi dolmasın). Ayrıca hizmet tarifesi, bir
    ücretli hizmet teklifi, bir ana olay ve bir tadilat başvurusu (servis istekleri 49–52)."""
    rng = random.Random(f"{SEED}:requests:{site.slug}")  # noqa: S311  # nosec B311
    reporters = list(
        await session.execute(
            select(UnitParty.person_id, UnitParty.unit_id).where(
                UnitParty.role != PartyRole.PROXY.value
            )
        )
    )
    flow = [
        RequestStatus.ASSIGNED,
        RequestStatus.IN_PROGRESS,
        RequestStatus.WAITING,
        RequestStatus.RESOLVED,
        RequestStatus.CLOSED,
    ]
    created: dict[str, Request] = {}
    for index, demand in enumerate(REQUESTS):
        person_id, unit_id = rng.choice(reporters)
        common = demand.location is not None
        opened = datetime.combine(
            today - timedelta(days=len(REQUESTS) - index), time(9), BUSINESS_TZ
        )
        request = await requests.create(
            session,
            requests.NewRequest(
                title=demand.title,
                description=None,
                category=demand.category,
                priority=RequestPriority(demand.priority),
                unit_id=None if common else unit_id,
                location=demand.location,
                reported_by_person_id=person_id,
            ),
            actor="Demo verisi",
            now=opened,
        )
        created[demand.title] = request
        target = RequestStatus(demand.status)
        if target is RequestStatus.OPEN:
            continue
        await requests.assign(
            session, request, demand.assignee or "Teknik ekip", actor="Site Yönetimi",
            now=opened + timedelta(hours=1),
        )  # fmt: skip
        steps = flow[1 : flow.index(target) + 1]
        if (
            RequestStatus.RESOLVED in steps
            and RequestStatus.WAITING in steps
            and (target is not RequestStatus.WAITING)
        ):
            steps.remove(RequestStatus.WAITING)
        for step, status in enumerate(steps, start=1):
            at = opened + timedelta(hours=1 + 3 * step)
            if status is RequestStatus.RESOLVED and target is RequestStatus.RESOLVED:
                at = max(at, datetime.combine(today - timedelta(days=1), time(15), BUSINESS_TZ))
            if status is RequestStatus.CLOSED:
                await service_desk.confirm(
                    session, site, request, solved=True, score=4, comment="Hızlı çözüldü.",
                    person_id=person_id, actor="Sakin", now=at,
                )  # fmt: skip
                continue
            await requests.change_status(
                session, request, status, demand.resolution, actor="Site Yönetimi", now=at
            )
    morning = datetime.combine(today, time(8), BUSINESS_TZ)
    for name, unit, price in TARIFFS:
        session.add(
            ServiceTariff(
                name=name, name_key=" ".join(ascii_fold(name).split()), unit=unit, price=price,
                is_active=True,
            )
        )  # fmt: skip
    await session.flush()
    leak = created["Mutfak lavabosu sızdırıyor"]
    await paid_services.create_quote(
        session, leak,
        [("Batarya değişimi (malzeme)", Decimal(1), Decimal("650.00")),
         ("İşçilik", Decimal("1.5"), Decimal("400.00"))],
        "Mutfak bataryası değişecek.", actor="Teknik ekip", now=morning,
    )  # fmt: skip
    lift = created["Asansör kat arasında duruyor"]
    await service_desk.create_outage(
        session,
        service_desk.OutageData(
            "A blok asansör arızası", lift.category, lift.location,
            "A blok asansörü bakımda; firma parça bekliyor. Lütfen B blok asansörünü kullanın.",
            [lift.id],
        ),
        actor="Site Yönetimi", now=morning,
    )  # fmt: skip
    owner = await session.scalar(
        select(UnitParty)
        .where(UnitParty.role == PartyRole.OWNER.value)
        .order_by(UnitParty.unit_id)
        .limit(1)
    )
    if owner is not None:
        await permits.apply(
            session,
            permits.PermitData(
                owner.unit_id, PermitType.RENOVATION, today + timedelta(days=5),
                today + timedelta(days=12), "Yapı Usta Tadilat",
                "Banyo yenileme: fayans ve tesisat değişimi.",
            ),
            person_id=owner.person_id, actor="Sakin", today=today,
        )  # fmt: skip


async def _seed_communication(session: AsyncSession, site: Site, today: date) -> None:
    """ILT-01/06, RAP-02: okuma onaylı duyuru, acil bilgi kartı, geçen ayın şeffaflık raporu."""
    now = datetime.combine(today - timedelta(days=1), time(11), BUSINESS_TZ)
    await announcements.publish(
        session,
        announcements.NewAnnouncement(
            title="Asansör yenileme çalışması — okuma onayı",
            body="A blok asansörü 3 gün boyunca kullanılamayacak. Lütfen okuduğunuzu onaylayın.",
            importance=Importance.IMPORTANT,
            audience=Audience.ALL_RESIDENTS,
            block_ids=(),
            channels=(),
            expires_on=None,
            is_pinned=False,
            requires_ack=True,
            ack_hours=72,
            sms_fallback=True,
        ),
        published_by="Site Yönetimi",
        now=now,
        today=today,
    )
    blocks = list(await session.scalars(select(Block).order_by(Block.name).limit(2)))
    await comm_svc.publish_card(
        session,
        comm_svc.CardInput(
            muster_points=[
                {"name": "Otopark girişi", "directions": "Ana kapının karşısı, açık alan"},
                {"name": "Çocuk parkı", "directions": "B blok arkası"},
            ],
            phones=[
                {"label": "Acil çağrı", "number": "112"},
                {"label": "Doğal gaz acil", "number": "187"},
                {"label": "Site güvenliği", "number": "02125550000"},
            ],
            instructions={},
            block_notes=[
                {"block_id": str(b.id), "note": "Merdivenle inin; asansör kullanmayın."}
                for b in blocks[:1]
            ],
        ),
        by="Site Yönetimi",
    )
    last = today.replace(day=1) - timedelta(days=1)
    report = await transparency_svc.prepare(
        session,
        year=last.year,
        month=last.month,
        scope={"include_categories": True, "include_collection": True,
               "include_cash_balance": False},
        note="Yönetim kurulu tarafından onaylanan aylık özet.",
        by="Site Yönetimi",
        today=today,
    )  # fmt: skip
    await transparency_svc.submit(
        session,
        site,
        report,
        requester=approvals_svc.Person(uuid.UUID(int=0), "Site Yönetimi"),
        can_publish=True,
        today=today,
        now=now,
    )


async def _seed_gate(session: AsyncSession, spec: SiteSpec, today: date) -> None:
    """Güvenlik modülü açık sitelerde kargo ve ziyaretçi kayıtları (docs/10 §1.4)."""
    rng = random.Random(f"{SEED}:gate:{spec.slug}")  # noqa: S311  # nosec B311
    units = list(await session.scalars(select(Unit.id).order_by(Unit.id)))
    morning = datetime.combine(today, time(9), BUSINESS_TZ)
    if ModuleKey.PACKAGES in spec.extra_modules:
        for index, carrier in enumerate(
            ("Yurtiçi Kargo", "Aras Kargo", "MNG Kargo", "PTT Kargo", "Sürat Kargo")
        ):
            package = await security.receive_package(
                session, unit_id=rng.choice(units), person_id=None, carrier=carrier, note=None,
                now=morning - timedelta(hours=index * 7), received_by="Demo verisi",
            )  # fmt: skip
            if index >= 3:  # eskiler teslim edilmiş
                await security.deliver_package(
                    session, package, pickup_code=package.pickup_code, delivered_to="Daire sakini",
                    now=morning - timedelta(hours=index * 7 - 2), delivered_by="Demo verisi",
                )  # fmt: skip
    if ModuleKey.VISITORS in spec.extra_modules:
        guests = (
            ("Ahmet Demir", VisitorKind.GUEST, "34ABC123"),
            ("Elektrik ustası", VisitorKind.SERVICE, None),
            ("Zeynep Kurt", VisitorKind.GUEST, None),
            ("Boya ekibi", VisitorKind.CONTRACTOR, "06XYZ45"),
        )
        for index, (name, kind, plate) in enumerate(guests):
            visitor = await security.record_visitor(
                session,
                security.NewVisitor(unit_id=rng.choice(units), full_name=name, kind=kind,
                                    plate_number=plate),
                now=morning + timedelta(minutes=40 * index), today=today, recorded_by="Demo verisi",
            )  # fmt: skip
            if index % 2 == 0:
                await security.exit_(session, visitor, morning + timedelta(hours=2 + index))
        await security.record_visitor(
            session,
            security.NewVisitor(unit_id=rng.choice(units), full_name="Mobilya teslimatı",
                                kind=VisitorKind.CARGO, expected_on=today + timedelta(days=1)),
            now=morning, today=today, recorded_by="Demo verisi",
        )  # fmt: skip


async def _seed_security_setup(session: AsyncSession, spec: SiteSpec, today: date) -> None:
    """Kapılar, devriye noktaları ve tur planı, park yerleri ve tahsisleri (servis istekleri
    40, 46; K30) — ekranlar boş görünmesin."""
    if ModuleKey.VISITORS not in spec.extra_modules:
        return
    for name, kind in (("Ana kapı", "vehicle"), ("Yaya kapısı", "pedestrian")):
        await gatehouse.save_gate(session, None, gatehouse.GateInput(name, kind, [], True, True))
    points = []
    for name, area in (
        ("Ana kapı önü", "Giriş"),
        ("Otopark B girişi", "Otopark"),
        ("Havuz", "Sosyal alan"),
        ("Jeneratör odası", "Teknik"),
    ):
        form = check_point(name=name, location=None, area=area, tag="qr", taken=set())
        points.append(await patrol.save_point(session, None, form, is_active=True))
    plan = check_plan(
        name="Gece turu",
        point_ids=[str(p.id) for p in points],
        ordered=True,
        start_times=["22:00", "01:00", "04:00"],
        allowed_minutes=30,
        assignee="Güvenlik",
        active_points={str(p.id) for p in points},
    )
    await patrol.save_plan(session, None, plan, is_active=True)
    units = list(await session.scalars(select(Unit.id).order_by(Unit.id).limit(6)))
    for index in range(1, 9):
        kind = "disabled" if index == 1 else "ev" if index == 2 else "standard"
        space = await parking.save_space(
            session, None,
            check_space(code=f"P-{index:02d}", kind=kind, location="Kapalı otopark -1",
                        is_paid=False, note=None, taken=set()),
            is_active=True,
        )  # fmt: skip
        if index <= len(units):
            await parking.allocate(
                session, space, unit_id=units[index - 1], start=today.replace(day=1), end=None,
                note=None, actor="Demo verisi",
            )  # fmt: skip


async def _seed_resident(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    password_hash: str,
    store: FileStore,
    today: date,
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
            session.add(SiteMembership(user_id=user_id, person_id=person.id))
            session.add(
                SiteRoleAssignment(
                    user_id=user_id, role_key=RESIDENT_KEY, assigned_by_name="Demo verisi"
                )
            )
            linked = await session.get(Person, person.id)
            if linked is not None:
                linked.user_id = user_id
            unit_id = await session.scalar(
                select(UnitParty.unit_id).where(UnitParty.person_id == person.id).limit(1)
            )
            if unit_id is not None:  # sakinin bölümüne özel belge (servis isteği 78)
                await _upload_document(session, store, RESIDENT_DOCUMENT, today, unit_id=unit_id)


# --- Belge arşivi (RAP-03) ------------------------------------------------------------

_ASCII = str.maketrans("çğıöşüÇĞİÖŞÜ—", "cgiosuCGIOSU-")


def demo_pdf(title: str, version: int) -> bytes:
    """Tek sayfalık küçük ama geçerli PDF (xref tablosu doğru): demo belge içeriği."""
    lines = [title.translate(_ASCII), f"Surum {version} - demo belgesi, ornek icerik"]
    text = " ".join(
        f"BT /F1 {18 if i == 0 else 11} Tf 72 {760 - i * 28} Td ({line}) Tj ET"
        for i, line in enumerate(lines)
    ).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


async def _upload_document(
    session: AsyncSession,
    store: FileStore,
    spec: DocumentSpec,
    today: date,
    *,
    block_id: uuid.UUID | None = None,
    unit_id: uuid.UUID | None = None,
) -> None:
    scope, target = (
        ("unit", unit_id) if unit_id else ("block", block_id) if block_id else ("site", None)
    )
    for version in range(1, spec.versions + 1):
        await documents.upload(
            session,
            documents.Upload(
                title=spec.title,
                kind=spec.kind,
                scope=scope,
                scope_id=target,
                confidentiality=spec.confidentiality,
                valid_until=today + timedelta(days=spec.valid_days)
                if spec.valid_days is not None
                else None,
                retention_years=spec.retention_years,
                file_name=f"{spec.title.translate(_ASCII).lower().replace(' ', '-')}.pdf",
                data=demo_pdf(spec.title, version),
            ),
            uploader=documents.Uploader(None, "Site Yönetimi", can_confidential=True),
            store=store,
        )


async def _seed_documents(session: AsyncSession, store: FileStore, today: date) -> None:
    """Yönetim planı (2 sürüm), kurallar, süresi yaklaşan/dolan poliçe ve etiket, gizli belge."""
    first_block = await session.scalar(select(Block.id).order_by(Block.sort_order, Block.name))
    for spec in DOCUMENTS:
        await _upload_document(
            session, store, spec, today, block_id=first_block if spec.first_block else None
        )


async def _seed_staff(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    user_ids: dict[str, uuid.UUID],
    today: date,
) -> None:
    """İnsan kaynakları (servis istekleri 35–39): kartlar (güvenlik ve teknik hesaplara bağlı —
    K29), üç vardiya şablonu, geçen ve bu haftanın planı (yayında), izinler, kesinleşmiş günler."""
    now = datetime.combine(today, time(9), BUSINESS_TZ)
    monday = week_start(today)
    with site_scope(site_id):
        async with factory() as session, session.begin():
            cards = []
            for name, position, payroll, email in STAFF_CARDS:
                cards.append(
                    await staff_svc.create(
                        session,
                        staff_svc.StaffData(
                            full_name=name,
                            position=position,
                            employer=Employer.SITE.value,
                            contractor_name=None,
                            phone=None,
                            start_date=date(today.year - 1, 3, 1),
                            end_date=None,
                            shift=None,
                            payroll_no=payroll,
                            user_id=user_ids.get(email) if email else None,
                        ),
                        today=today,
                        now=now,
                    )
                )
            templates = [
                await shifts_svc.save_template(
                    session,
                    shifts_svc.TemplateData(name, start, end, rest, None, "Ana kapı", True),
                    item=None,
                )
                for name, start, end, rest in SHIFT_TEMPLATES
            ]
            # Dönüşümlü plan: kişi i, gün d → şablon (i + d) % 3; pazar boş
            for week in (monday - timedelta(days=7), monday):
                for offset in range(6):
                    day = week + timedelta(days=offset)
                    for index, card in enumerate(cards):
                        await shifts_svc.assign(
                            session,
                            staff_id=card.id,
                            work_date=day,
                            template_id=templates[(index + offset) % len(templates)].id,
                            actor_name="Demo verisi",
                        )
                await shifts_svc.publish(
                    session, week=week, acknowledge=True, actor_name="Demo verisi", now=now
                )
            manager = approvals_svc.Person(user_ids["yonetici@demo.local"], "Kerem Yıldırım")
            site = await session.get(Site, site_id)
            if site is None:  # pragma: no cover
                return
            approved = await leaves_svc.create(
                session,
                site,
                leaves_svc.LeaveData(
                    cards[2].id,
                    "annual",
                    monday + timedelta(days=9),
                    monday + timedelta(days=11),
                    None,
                ),
                source="manager",
                actor=manager,
                today=today,
                now=now,
            )
            approved.status = "approved"
            approved.decided_by_name = manager.name
            approved.decided_at = now
            await leaves_svc.create(
                session,
                site,
                leaves_svc.LeaveData(
                    cards[1].id, "excuse", monday + timedelta(days=15), monday + timedelta(days=15),
                    "Aile ziyareti",
                ),
                source="manager",
                actor=manager,
                today=today,
                now=now,
            )  # fmt: skip
            await session.flush()
            # Geçen haftanın ilk üç günü kesin (puantaj ekranı ve aktarım önizlemesi boş görünmesin)
            for offset in range(3):
                day = monday - timedelta(days=7 - offset)
                if day < today:
                    await timesheets_svc.finalize(
                        session, day, today=today, actor_name="Demo verisi"
                    )


async def _seed_facility(
    factory: async_sessionmaker[AsyncSession],
    site_id: uuid.UUID,
    user_ids: dict[str, uuid.UUID],
    store: FileStore,
    today: date,
) -> None:
    """Operasyon ekleri ve teknik varlık (servis istekleri 53–58; HIZ-06/07; K28): anahtarlar
    (biri dışarıda, biri iadesi gecikmiş), sakinin emanet anahtarı, hazır denetim şablonları ve bir
    denetim, poliçeler (biri 45 gün kala) ve hasar dosyası, sayaçlar ve iki dönem okuma, görevli
    görevleri (personel kartına bağlı), stok, teknik varlıklar (yaklaşan muayeneler) ve periyodik
    bakım planları (biri bugün iş emri açar)."""
    now = datetime.combine(today, time(9), BUSINESS_TZ)
    sink = PhotoSink(store)
    technician = user_ids.get("teknik@demo.local")
    with site_scope(site_id):
        async with factory() as session, session.begin():
            site = await session.get(Site, site_id)
            if site is None:  # pragma: no cover
                return
            await _seed_keys(session, today, now)
            # 55 — denetim: hazır şablonlar + bir denetim (talep açmadan)
            await checklists_svc.ensure_defaults(session)
            cleaning = next(
                t
                for t in await session.scalars(checklists_svc.templates_query())
                if t.name == "Temizlik kalitesi"
            )
            await checklists_svc.inspect(
                session,
                sink,
                template=cleaning,
                location="A Blok giriş",
                answers=[
                    checklists_svc.AnswerData("yes", None),
                    checklists_svc.AnswerData("no", None),
                    checklists_svc.AnswerData("yes", DEMO_PHOTO),  # fotoğraf zorunlu madde
                    checklists_svc.AnswerData("4", None),
                ],
                open_requests=False,
                by=checklists_svc.Inspector("Ergün Kılıç", technician),
            )
            await _seed_insurance(session, today)
            await _seed_meters(session, sink, today, technician)
            await _seed_tasks(session, sink, today)
            for name, label, minimum, quantity in (
                ("Ampul (LED 9W)", "adet", "10", "24"),
                ("Çamaşır suyu", "litre", "20", "12"),
            ):
                item = await inventory_svc.create_item(
                    session, name=name, unit_label=label, min_quantity=minimum, location="Depo"
                )
                await inventory_svc.move(
                    session, item, direction="in", quantity=quantity, note="Açılış", moved_by="Demo"
                )
            await _seed_technical_assets(session, site, today)
            await session.flush()


# Küçük, geçerli imzalı demo görseli (JPEG başlığı) — saha fotoğrafı örneği
DEMO_PHOTO = (
    "data:image/jpeg;base64,"
    + base64.b64encode(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + bytes(64)).decode()
)


async def _seed_keys(session: AsyncSession, today: date, now: datetime) -> None:
    keys = [
        await keys_svc.save_key(
            session, keys_svc.KeyData(kind, code, name, area, list(roles), True), None
        )
        for kind, code, name, area, roles in FACILITY_KEYS
    ]
    await keys_svc.give(
        session,
        keys[0],
        to_name="Selin Arslan",
        to_phone=None,
        role=None,
        due_at=now + timedelta(days=2),
        by="Recep Er",
        now=now,
    )
    session.add(  # iadesi gecikmiş teslim (listede kırmızı)
        KeyLoan(
            key_id=keys[1].id,
            to_name="Hasan Usta (bahçıvan)",
            to_phone=None,
            role="Görevli",
            due_at=now - timedelta(hours=20),
            given_by_name="Recep Er",
        )
    )
    member = await session.scalar(
        select(SiteMembership)
        .where(
            SiteMembership.user_id.in_(
                select(SiteRoleAssignment.user_id).where(
                    SiteRoleAssignment.role_key == RESIDENT_KEY
                )
            ),
            SiteMembership.person_id.is_not(None),
        )
        .limit(1)
    )
    if member is None or member.person_id is None:
        return
    unit_id = await session.scalar(
        select(UnitParty.unit_id).where(UnitParty.person_id == member.person_id).limit(1)
    )
    if unit_id is None:
        return
    deposit = await keys_svc.deposit(
        session,
        unit_id=unit_id,
        person_id=member.person_id,
        user_id=member.user_id,
        by="Demo sakin",
        authorized=[keys_svc.Authorized("Mert Yılmaz", "05321112233")],
        valid_until=today + timedelta(days=10),
        note="Tatildeyken çiçekler için",
        today=today,
    )
    await keys_svc.receive(session, deposit, by="Recep Er", now=now)


async def _seed_insurance(session: AsyncSession, today: date) -> None:
    policies = [
        await insurance_svc.save_policy(
            session,
            insurance_svc.PolicyData(
                kind,
                insurer,
                number,
                coverage,
                Decimal(premium),
                today + timedelta(days=start),
                today + timedelta(days=end),
                None,
                None,
            ),
            None,
        )
        for kind, insurer, number, coverage, premium, start, end in INSURANCE_POLICIES
    ]
    claim = await insurance_svc.open_claim(
        session,
        policy=policies[0],
        source="Talep: B Blok su kaçağı",
        description="B Blok bodrum su baskını — depo duvarı",
        incident_date=today - timedelta(days=12),
        claimed=Decimal("24000.00"),
        by="Kerem Yıldırım",
        today=today,
    )
    await insurance_svc.update_claim(
        session, claim, status="submitted", paid=None, paid_on=None, today=today
    )


async def _seed_meters(
    session: AsyncSession, sink: PhotoSink, today: date, technician: uuid.UUID | None
) -> None:
    """İlk 6 bölümde ısı pay ölçer + bahçe sayacı; iki geçmiş dönem okuma (bu ay okunmadı)."""
    units = list(await session.scalars(select(Unit.id).order_by(Unit.number).limit(6)))
    meters = [
        await meters_svc.save_meter(
            session,
            meters_svc.MeterData("heat", f"ISI-{i + 1:04d}", unit, None, None, "1", None, "active"),
            None,
        )
        for i, unit in enumerate(units)
    ]
    meters.append(
        await meters_svc.save_meter(
            session,
            meters_svc.MeterData(
                "common", "BS-0001", None, "Bahçe sulama", None, "1", None, "active"
            ),
            None,
        )
    )
    month = today.replace(day=1)
    reader = meters_svc.Reader("Ergün Kılıç", technician)
    for back, step in ((2, 0), (1, 1)):
        period = f"{add_months(month, -back):%Y-%m}"
        for index, meter in enumerate(meters):
            value = 1000 + index * 37 + step * (120 + index * 5)
            await meters_svc.read(
                session,
                sink,
                meter,
                meters_svc.ReadingData(period, str(value), None, None, None),
                by=reader,
            )


async def _seed_tasks(session: AsyncSession, sink: PhotoSink, today: date) -> None:
    """Görevli rutin görevleri; K29: personel kartına bağlı (teknik ve temizlik görevlisi)."""
    cards = {c.full_name: c.id for c in await session.scalars(select(StaffMember))}
    templates = []
    for name, freq, days, mday, start, end, place, who, card, photo in TASK_TEMPLATES:
        card_id = cards.get(STAFF_CARDS[card][0]) if card is not None else None
        templates.append(
            await tasks_svc.save_template(
                session,
                tasks_svc.TemplateData(
                    name,
                    None,
                    freq,
                    list(days),
                    mday,
                    time.fromisoformat(start),
                    time.fromisoformat(end),
                    place,
                    who,
                    card_id,
                    photo,
                    None,
                    True,
                ),
                None,
            )
        )
    await tasks_svc.complete(
        session,
        sink,
        templates[1],
        day=today - timedelta(days=1),
        status="done",
        reason=None,
        note="Holler ve merdivenler",
        photo=None,
        by=tasks_svc.Closer("Hülya Demir", None, cards.get("Hülya Demir")),
        today=today,
    )


async def _seed_technical_assets(session: AsyncSession, site: Site, today: date) -> None:
    """K28: mali demirbaştan ayrı teknik varlıklar; muayene takvimi (biri 7, biri 30 gün kala) ve
    bakım planları (geçmiş dönem yapıldı; hidroforun bakımı bugün → iş emri açılır)."""
    assets = {}
    for category, name, place, brand, serial, company, check, last in TECHNICAL_ASSETS:
        schedules = (
            [assets_svc.ScheduleData(check, None, None, today + timedelta(days=last), None)]
            if check is not None and last is not None
            else []
        )
        assets[name] = await assets_svc.create_asset(
            session,
            assets_svc.AssetData(
                category,
                name,
                place,
                brand,
                None,
                serial,
                date(today.year - 6, 4, 1),
                date(today.year + 1, 4, 1) if category == "camera" else None,
                None,
                company,
                "working",
                None,
            ),
            schedules,
            today=today,
        )
    for asset_name, title, unit, count, due, who in MAINTENANCE_PLANS:
        due_on = today + timedelta(days=due)
        plan = await assets_svc.save_plan(
            session,
            assets_svc.PlanData(
                assets[asset_name].id,
                title,
                None,
                unit,
                count,
                add_months(due_on, -count) if due > 0 else due_on,
                who,
                None,
                None,
                True,
            ),
            None,
        )
        if due > 0:  # geçen dönem yapıldı → geçmiş ve maliyet; sıradaki tarih ileri kayar
            run = await assets_svc.open_run(session, plan, requests_enabled=False, by="Demo")
            await assets_svc.finish_run(
                session,
                run,
                assets_svc.RunCompletion(
                    "done", plan.next_due_on, Decimal("2400.00"), "Rutin bakım yapıldı", False, None
                ),
                requests_enabled=False,
                by=who,
                today=today,
            )
    await assets_svc.generate_due(session, site, today=today)


def _person(first: str, last: str) -> Person:
    first_name, last_name = normalize_person_name(first, last)
    return Person(first_name=first_name, last_name=last_name)


# --- Yapı, kişiler ve kayıt (servis istekleri 19–26) ------------------------------------

COMMON_AREAS = (
    ("Açık havuz", CommonAreaKind.POOL, True),
    ("Toplantı salonu", CommonAreaKind.HALL, True),
    ("Kapalı otopark", CommonAreaKind.PARKING, False),
    ("Çocuk parkı", CommonAreaKind.PLAYGROUND, False),
)
VEHICLE_MODELS = ("Renault Clio", "Fiat Egea", "Toyota Corolla", "VW Golf", "Hyundai i20")
VEHICLE_COLORS = ("Beyaz", "Gri", "Siyah", "Kırmızı", "Lacivert")
PLATE_LETTERS = ("ABC", "KL", "TR", "MN", "YZ", "BD")


async def _seed_directory(session: AsyncSession, spec: SiteSpec, today: date) -> None:
    """Ortak alanlar, araçlar, hane üyeleri, davet ve özel alan örnekleri — ekranlar boş
    görünmesin (docs/10)."""
    rng = random.Random(f"{SEED}:directory:{spec.slug}")  # noqa: S311  # nosec B311
    session.add_all(
        CommonArea(name=name, kind=kind.value, is_reservable=reservable)
        for name, kind, reservable in COMMON_AREAS
    )
    occupied = list(
        await session.execute(
            select(UnitParty.unit_id, UnitParty.person_id)
            .where(UnitParty.role.in_([PartyRole.OWNER.value, PartyRole.TENANT.value]))
            .order_by(UnitParty.unit_id, UnitParty.role.desc())
        )
    )
    owners: dict[uuid.UUID, uuid.UUID] = {}
    for unit_id, person_id in occupied:
        owners.setdefault(unit_id, person_id)  # kiracı varsa kiracı (role desc: tenant > owner)
    unit_ids = sorted(owners)
    city = "34" if spec.city != "Ankara" else "06"
    for index, unit_id in enumerate(unit_ids):
        if rng.random() > 0.35:
            continue
        plate = f"{city}{rng.choice(PLATE_LETTERS)}{rng.randint(100, 9999)}"
        session.add(
            Vehicle(
                unit_id=unit_id, plate=plate, brand_model=rng.choice(VEHICLE_MODELS),
                color=rng.choice(VEHICLE_COLORS), kind=VehicleKind.PERMANENT.value,
                status=(VehicleStatus.PENDING if index % 17 == 0 else VehicleStatus.ACTIVE).value,
                owner_person_id=owners[unit_id], created_by_name="Demo verisi",
            )
        )  # fmt: skip
    if unit_ids:
        session.add(
            Vehicle(
                unit_id=unit_ids[0], plate=f"{city}MS{rng.randint(10, 99)}",
                brand_model="Misafir aracı", kind=VehicleKind.GUEST.value,
                valid_until=today + timedelta(days=5), owner_person_id=owners[unit_ids[0]],
                created_by_name="Demo verisi",
            )
        )  # fmt: skip
    now = datetime.combine(today, time(10), BUSINESS_TZ)
    for index, unit_id in enumerate(unit_ids[1:4]):
        phone = f"+90532{rng.randint(1000000, 9999999)}"
        invitation = Invitation(
            id=uuid.uuid7(), full_name=("Can Yılmaz", "Ece Demir", "Mert Kaya")[index],
            phone=phone, unit_id=unit_id, relation="household", status="sent",
            token_hash=token_hash(f"demo-{spec.slug}-{index}"), sent_by_name="Demo verisi",
            sent_at=now - timedelta(days=index), expires_at=now + timedelta(days=7 - index),
        )  # fmt: skip
        session.add(invitation)
        await session.flush()  # üye satırı davete bağlı (FK)
        session.add(
            HouseholdMember(
                unit_id=unit_id, invitation_id=invitation.id, full_name=invitation.full_name,
                phone=phone, invited_by_name="Demo verisi",
                can_reservations=index == 0, can_requests=index == 1,
            )
        )  # fmt: skip
    for index, unit_id in enumerate(unit_ids[5:7]):
        session.add(
            Invitation(
                full_name=("Selin Aydın", "Burak Şahin")[index],
                phone=f"+90533{rng.randint(1000000, 9999999)}", unit_id=unit_id,
                relation="tenant", status="sent" if index == 0 else "completed",
                token_hash=token_hash(f"demo-invite-{spec.slug}-{index}"),
                sent_by_name="Demo verisi", sent_at=now - timedelta(days=2 + index),
                expires_at=now + timedelta(days=5 - index),
                completed_at=now - timedelta(days=1) if index else None,
            )
        )  # fmt: skip
    session.add_all(
        (
            CustomField(
                entity="unit", label="Evcil hayvan", key="evcil_hayvan", type="select",
                options=["Yok", "Kedi", "Köpek", "Diğer"], kvkk_category="other",
                view_roles=["Yönetici", "Güvenlik"], edit_roles=["Yönetici"],
                resident_can_view=True, resident_can_edit=True, sort_order=1,
            ),
            CustomField(
                entity="unit", label="Depo no", key="depo_no", type="text",
                kvkk_category="other", view_roles=["Yönetici"], edit_roles=["Yönetici"],
                sort_order=2,
            ),
            CustomField(
                entity="person", label="İkinci telefon", key="ikinci_telefon", type="text",
                kvkk_category="contact", view_roles=["Yönetici"], edit_roles=["Yönetici"],
                sort_order=1,
            ),
        )
    )  # fmt: skip
    session.add(StructureSettings(kvkk_published=False))
    await session.flush()
