"""Tahakkuk koşusu: önizle → kaydet → (gerekirse) ters kaydet — docs/04 §4, §7. Site kapsamında.

Önizleme hiçbir şey yazmaz. Kaydetme tek transaction'dır: koşu + borçlar + satırlar + borç
hareketleri + özet bakiye. Bir dönem için tek geçerli koşu: uygulama kontrol eder, kısmi
benzersiz indeks eşzamanlı ikinci kaydı veritabanında reddeder.

Motor girdisi sabit sayıda sorguyla kurulur (bölüm sayısından bağımsız — docs/08 §3).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.charging.engine import (
    AccountData,
    ChargeInput,
    ComponentData,
    ItemData,
    PartyData,
    Preview,
    RuleData,
    UnitData,
    build_preview,
)
from site_yonetim.domain.charging.periods import (
    DUE_AFTER_DAYS,
    YearMonth,
    already_posted_message,
    charge_description,
    default_dates,
    is_item_due,
    next_period,
    reversal_description,
)
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    ChargeRunStatus,
    FinanceRuleError,
    Frequency,
    LedgerSource,
    PayerRule,
    PeriodStatus,
    ScopeKind,
)
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.models import (
    AllocationComponent,
    AllocationRule,
    Block,
    BudgetItem,
    BudgetPlan,
    Charge,
    ChargeLine,
    ChargeRun,
    ChargeType,
    LedgerAccount,
    LedgerEntry,
    Period,
    Person,
    Unit,
    UnitParty,
    UnitType,
    UnitWeight,
)
from site_yonetim.services import ledger
from site_yonetim.services.budget import current_plan

BUSINESS_TZ = ZoneInfo("Europe/Istanbul")


def _valid_run() -> list[ColumnElement[bool]]:
    """Geçerli koşu: kaydedilmiş ve ters kaydı alınmamış (docs/04 §7.2)."""
    return [
        ChargeRun.status == ChargeRunStatus.POSTED.value,
        ChargeRun.reversal_of_run_id.is_(None),
    ]


async def last_valid_period(session: AsyncSession) -> YearMonth | None:
    row = (
        await session.execute(
            select(Period.year, Period.month)
            .join(
                ChargeRun,
                and_(ChargeRun.period_id == Period.id, ChargeRun.site_id == Period.site_id),
            )
            .where(*_valid_run())
            .order_by(Period.year.desc(), Period.month.desc())
            .limit(1)
        )
    ).first()
    return YearMonth(row.year, row.month) if row else None


# --- Motor girdisi ------------------------------------------------------------------


async def _units(session: AsyncSession) -> list[UnitData]:
    rows = await session.execute(
        select(Unit, Block.name, UnitType.weight)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .outerjoin(
            UnitType, and_(UnitType.id == Unit.unit_type_id, UnitType.site_id == Unit.site_id)
        )
        .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
    )
    return [
        UnitData(
            id=unit.id,
            name=unit.display_name(block_name),
            block_id=unit.block_id,
            unit_type_id=unit.unit_type_id,
            unit_type_weight=weight,
            gross_area=unit.gross_area,
            net_area=unit.net_area,
            land_share_numerator=unit.land_share_numerator,
            land_share_denominator=unit.land_share_denominator,
            usage=unit.usage,
            is_active=unit.is_active,
        )
        for unit, block_name, weight in rows
    ]


async def _parties(session: AsyncSession) -> list[PartyData]:
    rows = await session.execute(
        select(UnitParty, Person.first_name, Person.last_name)
        .join(Person, and_(Person.id == UnitParty.person_id, Person.site_id == UnitParty.site_id))
        .where(UnitParty.role.in_([PartyRole.OWNER.value, PartyRole.TENANT.value]))
    )
    return [
        PartyData(
            unit_id=party.unit_id,
            person_id=party.person_id,
            person_name=f"{first} {last}",
            role=PartyRole(party.role),
            start_date=party.start_date,
            end_date=party.end_date,
            share_percent=party.share_percent,
        )
        for party, first, last in rows
    ]


async def _accounts(session: AsyncSession) -> list[AccountData]:
    rows = await session.scalars(select(LedgerAccount))
    return [
        AccountData(
            a.id, a.unit_id, a.person_id, AccountKind(a.kind), a.reference_code, a.is_closed
        )
        for a in rows
    ]


async def _rules(session: AsyncSession, rule_ids: set[uuid.UUID]) -> dict[uuid.UUID, RuleData]:
    if not rule_ids:
        return {}
    rules = list(
        await session.scalars(select(AllocationRule).where(AllocationRule.id.in_(rule_ids)))
    )
    components: dict[uuid.UUID, list[ComponentData]] = defaultdict(list)
    for c in await session.scalars(
        select(AllocationComponent)
        .where(AllocationComponent.allocation_rule_id.in_(rule_ids))
        .order_by(AllocationComponent.sort_order)
    ):
        components[c.allocation_rule_id].append(
            ComponentData(AllocationKind(c.kind), c.percent, AreaBasis(c.area_basis))
        )
    weights: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = defaultdict(dict)
    for w in await session.scalars(
        select(UnitWeight).where(UnitWeight.allocation_rule_id.in_(rule_ids))
    ):
        weights[w.allocation_rule_id][w.unit_id] = w.weight
    return {
        r.id: RuleData(
            id=r.id,
            name=r.name,
            kind=AllocationKind(r.kind),
            area_basis=AreaBasis(r.area_basis),
            fixed_amount=r.fixed_amount,
            components=tuple(components[r.id]),
            unit_weights=weights[r.id],
        )
        for r in rules
    }


async def _charged_item_ids(session: AsyncSession, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Geçerli bir koşuda kesilmiş kalemler (tek seferlik kalem ikinci kez kesilmez)."""
    if not item_ids:
        return set()
    rows = await session.scalars(
        select(ChargeLine.budget_item_id)
        .join(Charge, and_(Charge.id == ChargeLine.charge_id, Charge.site_id == ChargeLine.site_id))
        .join(
            ChargeRun,
            and_(ChargeRun.id == Charge.charge_run_id, ChargeRun.site_id == Charge.site_id),
        )
        .where(ChargeLine.budget_item_id.in_(item_ids), *_valid_run())
        .distinct()
    )
    return set(rows)


async def _items(
    session: AsyncSession, plan: BudgetPlan, period: YearMonth
) -> tuple[list[ItemData], list[str]]:
    """Bu dönem kesilecek kalemler ve takvim gereği bu dönem kesilmeyenlerin adları."""
    rows = list(
        await session.execute(
            select(BudgetItem, ChargeType.payer_rule)
            .outerjoin(
                ChargeType,
                and_(
                    ChargeType.id == BudgetItem.charge_type_id,
                    ChargeType.site_id == BudgetItem.site_id,
                ),
            )
            .where(BudgetItem.budget_plan_id == plan.id)
            .order_by(BudgetItem.sort_order, BudgetItem.id)
        )
    )
    rules = await _rules(session, {item.allocation_rule_id for item, _ in rows})
    one_time = [item.id for item, _ in rows if item.frequency == Frequency.ONE_TIME.value]
    charged = await _charged_item_ids(session, one_time)
    due: list[ItemData] = []
    skipped: list[str] = []
    for item, payer in rows:
        frequency = Frequency(item.frequency)
        if not is_item_due(
            frequency, period, fiscal_year=plan.fiscal_year, charged_before=item.id in charged
        ):
            skipped.append(item.name)
            continue
        due.append(
            ItemData(
                id=item.id,
                name=item.name,
                annual_amount=item.annual_amount,
                frequency=frequency,
                rule=rules.get(item.allocation_rule_id),
                payer_rule=PayerRule(payer) if payer else PayerRule.OCCUPANT,
                scope_kind=ScopeKind(item.scope_kind),
                scope_block_ids=frozenset(item.scope_block_ids or ()),
                scope_unit_type_ids=frozenset(item.scope_unit_type_ids or ()),
                scope_usage=item.scope_usage,
            )
        )
    return due, skipped


# --- Önizleme -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedRun:
    period: YearMonth
    charge_date: date
    due_date: date
    plan: BudgetPlan
    preview: Preview
    not_due_items: list[str]  # takvim gereği bu dönem kesilmeyen kalemler


async def prepare(
    session: AsyncSession,
    today: date,
    *,
    charge_date: date | None = None,
    due_date: date | None = None,
) -> PreparedRun:
    """Önizleme; `charge_date` verilmezse sıradaki dönemin 1'i (docs/04 §7.2)."""
    plan = await current_plan(session)
    if plan is None:
        raise FinanceRuleError(
            "no_finalized_budget",
            "Tahakkuk kesmek için önce işletme projesi kesinleşmeli.",
            conflict=True,
        )
    if charge_date is None:
        period = next_period(await last_valid_period(session), today)
        charge_date, default_due = default_dates(period)
    else:
        period = YearMonth.of(charge_date)
        default_due = charge_date + timedelta(days=DUE_AFTER_DAYS)
    due_date = due_date or default_due
    if due_date < charge_date:
        raise FinanceRuleError(
            "due_before_charge", "Vade tarihi tahakkuk tarihinden önce olamaz.", field="due_date"
        )
    items, not_due = await _items(session, plan, period)
    data = ChargeInput(
        charge_date=charge_date,
        due_date=due_date,
        items=items,
        units=await _units(session),
        parties=await _parties(session),
        accounts=await _accounts(session),
    )
    return PreparedRun(period, charge_date, due_date, plan, build_preview(data), not_due)


# --- Kaydetme -----------------------------------------------------------------------


async def _period_row(session: AsyncSession, period: YearMonth) -> Period:
    row: Period | None = await session.scalar(
        select(Period).where(Period.year == period.year, Period.month == period.month)
    )
    if row is None:  # dönem yoksa oluşturulur (docs/04 §7.1)
        row = Period(year=period.year, month=period.month)
        session.add(row)
        await session.flush()
    if row.status == PeriodStatus.CLOSED.value:
        raise FinanceRuleError(
            "period_closed", f"{period.name} dönemi kapalı; kayıt yazılamaz.", conflict=True
        )
    return row


async def valid_run_of(session: AsyncSession, period_id: uuid.UUID) -> ChargeRun | None:
    run: ChargeRun | None = await session.scalar(
        select(ChargeRun).where(ChargeRun.period_id == period_id, *_valid_run())
    )
    return run


def already_posted(period: YearMonth, run: ChargeRun) -> FinanceRuleError:
    posted = (run.posted_at or run.created_at).astimezone(BUSINESS_TZ)
    return FinanceRuleError(
        "period_already_charged",
        already_posted_message(period, posted.date(), posted.strftime("%H:%M")),
        conflict=True,
    )


@dataclass(frozen=True, slots=True)
class PostedRun:
    run: ChargeRun
    period: YearMonth
    charge_count: int
    unit_count: int
    total_amount: Decimal


async def post(
    session: AsyncSession, prepared: PreparedRun, *, posted_by: str, now: datetime
) -> PostedRun:
    """Önizlemeyi kalıcı kayda çevirir (docs/04 §7.1). Transaction'ı çağıran yönetir."""
    period_row = await _period_row(session, prepared.period)
    existing = await valid_run_of(session, period_row.id)
    if existing is not None:
        raise already_posted(prepared.period, existing)
    preview = prepared.preview
    if not preview.charges:
        raise FinanceRuleError("nothing_to_charge", "Kesilecek tahakkuk yok.", conflict=True)

    await ledger.lock_accounts(session, [c.ledger_account_id for c in preview.charges])
    run = ChargeRun(
        id=uuid.uuid7(),
        period_id=period_row.id,
        budget_plan_id=prepared.plan.id,
        status=ChargeRunStatus.POSTED.value,
        charge_date=prepared.charge_date,
        due_date=prepared.due_date,
        posted_at=now,
        posted_by=posted_by,
    )
    session.add(run)
    await session.flush()
    for preview_charge in preview.charges:
        charge = Charge(
            id=uuid.uuid7(),
            charge_run_id=run.id,
            unit_id=preview_charge.unit_id,
            ledger_account_id=preview_charge.ledger_account_id,
            amount=preview_charge.amount,
        )
        session.add(charge)
        session.add_all(
            ChargeLine(
                charge_id=charge.id,
                budget_item_id=line.budget_item_id,
                description=line.description,
                amount=line.amount,
                allocation_kind=line.allocation_kind.value,
                weight=line.weight,
                weight_total=line.weight_total,
                source_amount=line.source_amount,
                explanation=line.explanation,
            )
            for line in preview_charge.lines
        )
        amount = preview_charge.amount
        session.add(
            LedgerEntry(
                account_id=preview_charge.ledger_account_id,
                date=prepared.charge_date,
                # Eksi tutarlı (iade) kalem alacak olarak yazılır; alacağın vadesi olmaz.
                due_date=prepared.due_date if amount > ZERO else None,
                debit=amount if amount > ZERO else ZERO,
                credit=-amount if amount < ZERO else ZERO,
                source=LedgerSource.CHARGE.value,
                source_id=charge.id,
                description=charge_description(
                    prepared.period, [line.description for line in preview_charge.lines]
                ),
            )
        )
    await ledger.refresh_balances(session, [c.ledger_account_id for c in preview.charges])
    return PostedRun(
        run, prepared.period, len(preview.charges), preview.unit_count, preview.total_amount
    )


# --- Ters kayıt ---------------------------------------------------------------------


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> ChargeRun | None:
    run: ChargeRun | None = await session.scalar(select(ChargeRun).where(ChargeRun.id == run_id))
    return run


async def run_period(session: AsyncSession, run: ChargeRun) -> YearMonth:
    row = (
        await session.execute(select(Period.year, Period.month).where(Period.id == run.period_id))
    ).one()
    return YearMonth(row.year, row.month)


async def reverse(
    session: AsyncSession,
    run: ChargeRun,
    *,
    reason: str,
    today: date,
    reversed_by: str,
    now: datetime,
) -> ChargeRun:
    """Koşuyu iptal eder (docs/04 §7.3): orijinal satırlar yerinde kalır, karşı hareket yazılır."""
    locked: ChargeRun | None = await session.scalar(
        select(ChargeRun).where(ChargeRun.id == run.id).with_for_update()
    )
    if locked is None:  # pragma: no cover - aynı transaction'da az önce okundu
        raise FinanceRuleError("run_not_found", "Tahakkuk koşusu bulunamadı.")
    already = await session.scalar(select(exists().where(ChargeRun.reversal_of_run_id == run.id)))
    if already or locked.status == ChargeRunStatus.REVERSED.value:
        raise FinanceRuleError(
            "run_already_reversed", "Bu koşu zaten ters kayıtla iptal edilmiş.", conflict=True
        )
    if locked.status != ChargeRunStatus.POSTED.value or locked.reversal_of_run_id is not None:
        raise FinanceRuleError(
            "run_not_reversible",
            "Yalnızca kaydedilmiş koşular ters kayıtla iptal edilebilir.",
            conflict=True,
        )
    period = await run_period(session, locked)
    entries = list(
        await session.scalars(
            select(LedgerEntry)
            .join(
                Charge,
                and_(Charge.id == LedgerEntry.source_id, Charge.site_id == LedgerEntry.site_id),
            )
            .where(
                Charge.charge_run_id == locked.id,
                LedgerEntry.source == LedgerSource.CHARGE.value,
                LedgerEntry.reversal_of_entry_id.is_(None),
            )
        )
    )
    await ledger.lock_accounts(session, [e.account_id for e in entries])
    reversal = ChargeRun(
        id=uuid.uuid7(),
        period_id=locked.period_id,
        budget_plan_id=locked.budget_plan_id,
        status=ChargeRunStatus.POSTED.value,
        charge_date=today,
        due_date=locked.due_date,
        posted_at=now,
        posted_by=reversed_by,
        reversal_of_run_id=locked.id,
        reason=reason,
    )
    session.add(reversal)
    description = reversal_description(period)
    session.add_all(
        LedgerEntry(
            account_id=entry.account_id,
            date=today,
            due_date=None,
            debit=entry.credit,
            credit=entry.debit,
            source=LedgerSource.CHARGE.value,
            source_id=entry.source_id,
            description=description,
            reversal_of_entry_id=entry.id,
        )
        for entry in entries
    )
    locked.status = ChargeRunStatus.REVERSED.value
    await session.flush()
    await ledger.refresh_balances(session, [e.account_id for e in entries])
    return reversal
