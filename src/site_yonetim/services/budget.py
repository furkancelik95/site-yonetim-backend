"""İşletme projesi (bütçe) servisleri — docs/04 §3, docs/03 §6. Açık site kapsamında.

Kalemler yalnız taslakta değişir. Tebliğ → 7 gün itiraz → kesinleşme; yenisi kesinleşince
eskisi `superseded` olur. Kesinleşmiş proje değiştirilemez (İİK m.68 belgesi).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from site_yonetim.domain.charging.budget import check_finalize, check_notify, ensure_editable
from site_yonetim.domain.finance import BudgetStatus, FinanceRuleError, Frequency, ScopeKind
from site_yonetim.domain.money import ZERO
from site_yonetim.models import (
    AllocationComponent,
    AllocationRule,
    Block,
    BudgetItem,
    BudgetPlan,
    ChargeType,
    ExpenseCategory,
    UnitType,
    UnitUsage,
)

NAME_MIN, NAME_MAX = 2, 120


def _name(value: str, field: str = "name") -> str:
    text = " ".join(value.split())
    if not NAME_MIN <= len(text) <= NAME_MAX:
        raise FinanceRuleError(
            "invalid_name", f"Ad {NAME_MIN}–{NAME_MAX} karakter olmalı.", field=field
        )
    return text


async def get_plan(session: AsyncSession, plan_id: uuid.UUID) -> BudgetPlan | None:
    plan: BudgetPlan | None = await session.scalar(
        select(BudgetPlan).where(BudgetPlan.id == plan_id)
    )
    return plan


async def current_plan(session: AsyncSession) -> BudgetPlan | None:
    """Tahakkukun kaynağı: kesinleşmiş projelerden mali yılı en büyük olan (docs/04 §3)."""
    plan: BudgetPlan | None = await session.scalar(
        select(BudgetPlan)
        .where(BudgetPlan.status == BudgetStatus.FINALIZED.value)
        .order_by(BudgetPlan.fiscal_year.desc(), BudgetPlan.finalized_on.desc(), BudgetPlan.id)
        .limit(1)
    )
    return plan


async def plan_items(session: AsyncSession, plan_id: uuid.UUID) -> list[BudgetItem]:
    rows = await session.scalars(
        select(BudgetItem)
        .where(BudgetItem.budget_plan_id == plan_id)
        .order_by(BudgetItem.sort_order, BudgetItem.id)
    )
    return list(rows)


async def plan_totals(
    session: AsyncSession, plan_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, int]]:
    """Proje başına yıllık toplam ve kalem sayısı — veritabanında toplanır."""
    if not plan_ids:
        return {}
    rows = await session.execute(
        select(BudgetItem.budget_plan_id, func.sum(BudgetItem.annual_amount), func.count())
        .where(BudgetItem.budget_plan_id.in_(plan_ids))
        .group_by(BudgetItem.budget_plan_id)
    )
    return {plan_id: (total or ZERO, count) for plan_id, total, count in rows}


async def create_plan(session: AsyncSession, *, fiscal_year: int, name: str) -> BudgetPlan:
    plan = BudgetPlan(fiscal_year=fiscal_year, name=_name(name))
    session.add(plan)
    await session.flush()
    return plan


@dataclass(frozen=True, slots=True)
class ItemFields:
    name: str
    expense_category_id: uuid.UUID
    charge_type_id: uuid.UUID
    allocation_rule_id: uuid.UUID
    annual_amount: Decimal
    frequency: Frequency = Frequency.MONTHLY
    scope_kind: ScopeKind = ScopeKind.WHOLE_SITE
    scope_block_ids: tuple[uuid.UUID, ...] = ()
    scope_unit_type_ids: tuple[uuid.UUID, ...] = ()
    scope_usage: UnitUsage | None = None
    sort_order: int = 0


type IdColumn = InstrumentedAttribute[uuid.UUID]


async def _require(session: AsyncSession, column: IdColumn, item_id: uuid.UUID, field: str) -> None:
    # Başka sitenin kaydı kiracı filtresi yüzünden burada "yok"tur.
    if await session.scalar(select(column).where(column == item_id)) is None:
        raise FinanceRuleError(
            f"{field.removesuffix('_id')}_not_found", "Seçilen kayıt bulunamadı.", field=field
        )


async def _check_item(session: AsyncSession, fields: ItemFields) -> None:
    await _require(session, ExpenseCategory.id, fields.expense_category_id, "expense_category_id")
    await _require(session, ChargeType.id, fields.charge_type_id, "charge_type_id")
    await _require(session, AllocationRule.id, fields.allocation_rule_id, "allocation_rule_id")
    match fields.scope_kind:
        case ScopeKind.BLOCKS:
            await _require_all(session, Block.id, fields.scope_block_ids, "scope_block_ids", "blok")
        case ScopeKind.UNIT_TYPES:
            await _require_all(
                session,
                UnitType.id,
                fields.scope_unit_type_ids,
                "scope_unit_type_ids",
                "daire tipi",
            )
        case ScopeKind.USAGE:
            if fields.scope_usage is None:
                raise FinanceRuleError(
                    "scope_incomplete", "Kullanım kapsamı için kullanım türü seçin.",
                    field="scope_usage",
                )  # fmt: skip
        case ScopeKind.WHOLE_SITE:
            pass


async def _require_all(
    session: AsyncSession, column: IdColumn, ids: tuple[uuid.UUID, ...], field: str, label: str
) -> None:
    if not ids:
        raise FinanceRuleError(
            "scope_incomplete", f"Kapsam için en az bir {label} seçin.", field=field
        )
    found = set(await session.scalars(select(column).where(column.in_(ids))))
    if found != set(ids):
        raise FinanceRuleError(
            "scope_not_found", f"Seçilen {label} bu sitede bulunamadı.", field=field
        )


def _apply(item: BudgetItem, fields: ItemFields) -> None:
    item.name = _name(fields.name)
    item.expense_category_id = fields.expense_category_id
    item.charge_type_id = fields.charge_type_id
    item.allocation_rule_id = fields.allocation_rule_id
    item.annual_amount = fields.annual_amount
    item.frequency = fields.frequency.value
    item.scope_kind = fields.scope_kind.value
    blocks = fields.scope_kind is ScopeKind.BLOCKS
    types = fields.scope_kind is ScopeKind.UNIT_TYPES
    usage = fields.scope_kind is ScopeKind.USAGE
    item.scope_block_ids = list(fields.scope_block_ids) if blocks else None
    item.scope_unit_type_ids = list(fields.scope_unit_type_ids) if types else None
    item.scope_usage = fields.scope_usage.value if usage and fields.scope_usage else None
    item.sort_order = fields.sort_order


async def add_item(session: AsyncSession, plan: BudgetPlan, fields: ItemFields) -> BudgetItem:
    ensure_editable(BudgetStatus(plan.status))
    await _check_item(session, fields)
    item = BudgetItem(budget_plan_id=plan.id)
    _apply(item, fields)
    session.add(item)
    await session.flush()
    return item


async def update_item(
    session: AsyncSession, plan: BudgetPlan, item: BudgetItem, fields: ItemFields
) -> BudgetItem:
    ensure_editable(BudgetStatus(plan.status))
    await _check_item(session, fields)
    _apply(item, fields)
    await session.flush()
    return item


async def remove_item(session: AsyncSession, plan: BudgetPlan, item: BudgetItem) -> None:
    """Taslak kalemi kaldırır — henüz hiçbir tahakkuka dayanak olmamıştır."""
    ensure_editable(BudgetStatus(plan.status))
    await session.delete(item)
    await session.flush()


async def notify(session: AsyncSession, plan: BudgetPlan, notified_on: date, today: date) -> None:
    count = await session.scalar(
        select(func.count()).select_from(BudgetItem).where(BudgetItem.budget_plan_id == plan.id)
    )
    plan.objection_deadline = check_notify(
        BudgetStatus(plan.status), count or 0, notified_on, today
    )
    plan.status = BudgetStatus.NOTIFIED.value
    plan.notified_on = notified_on
    await session.flush()


async def finalize(session: AsyncSession, plan: BudgetPlan, today: date) -> list[BudgetPlan]:
    """Kesinleştirir; önceki kesinleşmiş projeler `superseded` olur. Onları döner."""
    check_finalize(BudgetStatus(plan.status), plan.objection_deadline, today)
    previous = list(
        await session.scalars(
            select(BudgetPlan)
            .where(BudgetPlan.status == BudgetStatus.FINALIZED.value, BudgetPlan.id != plan.id)
            .with_for_update()
        )
    )
    for old in previous:
        old.status = BudgetStatus.SUPERSEDED.value
    plan.status = BudgetStatus.FINALIZED.value
    plan.finalized_on = today
    await session.flush()
    return previous


# --- Seçim listeleri -------------------------------------------------------------------


async def charge_types(session: AsyncSession) -> list[ChargeType]:
    return list(
        await session.scalars(select(ChargeType).order_by(ChargeType.sort_order, ChargeType.name))
    )


async def expense_categories(session: AsyncSession) -> list[ExpenseCategory]:
    return list(
        await session.scalars(
            select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
        )
    )


async def allocation_rules(
    session: AsyncSession,
) -> list[tuple[AllocationRule, list[AllocationComponent]]]:
    rules = list(await session.scalars(select(AllocationRule).order_by(AllocationRule.name)))
    components = await session.scalars(
        select(AllocationComponent).order_by(AllocationComponent.sort_order)
    )
    by_rule: dict[uuid.UUID, list[AllocationComponent]] = {r.id: [] for r in rules}
    for component in components:
        by_rule.setdefault(component.allocation_rule_id, []).append(component)
    return [(rule, by_rule[rule.id]) for rule in rules]
