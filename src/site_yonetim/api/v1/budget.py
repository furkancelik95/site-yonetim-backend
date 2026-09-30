"""İşletme projesi (bütçe) ve finans seçim listeleri — docs/06 §2.6, docs/04 §3.

Okuma `finance.read`, değiştirme `finance.budget.manage`. Kalemler yalnız taslakta değişir;
tebliğ → 7 gün itiraz → kesinleşme. Kesinleşmiş proje değiştirilemez (409).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from site_yonetim.api.deps import SiteContext, TodayDep
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import BudgetManage, FinanceRead, finance_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.charging.periods import period_amount
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    BudgetStatus,
    ExpenseCategoryKind,
    FinanceRuleError,
    Frequency,
    PayerRule,
    ScopeKind,
)
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    AllocationComponent,
    AllocationRule,
    BudgetItem,
    BudgetPlan,
    ChargeType,
    UnitUsage,
)
from site_yonetim.services import budget as svc

router = APIRouter(prefix="/sites/{slug}", tags=["işletme projesi"])
Paging = Annotated[PageParams, Depends()]


# --- Seçim listeleri ---------------------------------------------------------------


class ChargeTypeOut(BaseModel):
    id: uuid.UUID
    name: str
    payer_rule: PayerRule = Field(description="occupant: oturan öder · owner: malik öder")
    legal_basis: str | None
    is_advance: bool

    @classmethod
    def of(cls, row: ChargeType) -> ChargeTypeOut:
        return cls(
            id=row.id,
            name=row.name,
            payer_rule=PayerRule(row.payer_rule),
            legal_basis=row.legal_basis,
            is_advance=row.is_advance,
        )


class ExpenseCategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: ExpenseCategoryKind


class ComponentOut(BaseModel):
    kind: AllocationKind
    area_basis: AreaBasis
    percent: str = Field(description="yüzde, metin (`70.00`)")


class AllocationRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: AllocationKind
    area_basis: AreaBasis
    fixed_amount: Money | None
    note: str | None
    components: list[ComponentOut]

    @classmethod
    def of(cls, rule: AllocationRule, components: list[AllocationComponent]) -> AllocationRuleOut:
        return cls(
            id=rule.id,
            name=rule.name,
            kind=AllocationKind(rule.kind),
            area_basis=AreaBasis(rule.area_basis),
            fixed_amount=rule.fixed_amount,
            note=rule.note,
            components=[
                ComponentOut(
                    kind=AllocationKind(c.kind),
                    area_basis=AreaBasis(c.area_basis),
                    percent=f"{c.percent:.2f}",
                )
                for c in components
            ],
        )


@router.get("/charge-types", summary="Tahakkuk tipleri")
async def list_charge_types(ctx: FinanceRead) -> list[ChargeTypeOut]:
    return [ChargeTypeOut.of(t) for t in await svc.charge_types(ctx.session)]


@router.get("/expense-categories", summary="Gider kategorileri")
async def list_expense_categories(ctx: FinanceRead) -> list[ExpenseCategoryOut]:
    rows = await svc.expense_categories(ctx.session)
    return [
        ExpenseCategoryOut(id=c.id, name=c.name, kind=ExpenseCategoryKind(c.kind)) for c in rows
    ]


@router.get("/allocation-rules", summary="Dağıtım kuralları")
async def list_allocation_rules(ctx: FinanceRead) -> list[AllocationRuleOut]:
    return [AllocationRuleOut.of(r, c) for r, c in await svc.allocation_rules(ctx.session)]


# --- İşletme projesi ------------------------------------------------------------------


class BudgetPlanOut(BaseModel):
    id: uuid.UUID
    fiscal_year: int
    name: str
    status: BudgetStatus
    notified_on: date | None
    objection_deadline: date | None = Field(description="tebliğ + 7 gün")
    finalized_on: date | None
    total_annual_amount: Money
    item_count: int

    @classmethod
    def of(cls, plan: BudgetPlan, total: tuple[Decimal, int] | None = None) -> BudgetPlanOut:
        amount, count = total or (ZERO, 0)
        return cls(
            id=plan.id,
            fiscal_year=plan.fiscal_year,
            name=plan.name,
            status=BudgetStatus(plan.status),
            notified_on=plan.notified_on,
            objection_deadline=plan.objection_deadline,
            finalized_on=plan.finalized_on,
            total_annual_amount=amount,
            item_count=count,
        )


class BudgetItemOut(BaseModel):
    id: uuid.UUID
    name: str
    expense_category_id: uuid.UUID
    charge_type_id: uuid.UUID
    allocation_rule_id: uuid.UUID
    annual_amount: Money
    period_amount: Money = Field(description="bir kesimdeki tutar (aylıkta yıllık/12)")
    frequency: Frequency
    scope_kind: ScopeKind
    scope_block_ids: list[uuid.UUID]
    scope_unit_type_ids: list[uuid.UUID]
    scope_usage: UnitUsage | None
    sort_order: int

    @classmethod
    def of(cls, item: BudgetItem) -> BudgetItemOut:
        frequency = Frequency(item.frequency)
        return cls(
            id=item.id,
            name=item.name,
            expense_category_id=item.expense_category_id,
            charge_type_id=item.charge_type_id,
            allocation_rule_id=item.allocation_rule_id,
            annual_amount=item.annual_amount,
            period_amount=period_amount(item.annual_amount, frequency),
            frequency=frequency,
            scope_kind=ScopeKind(item.scope_kind),
            scope_block_ids=item.scope_block_ids or [],
            scope_unit_type_ids=item.scope_unit_type_ids or [],
            scope_usage=UnitUsage(item.scope_usage) if item.scope_usage else None,
            sort_order=item.sort_order,
        )


class BudgetPlanDetail(BudgetPlanOut):
    items: list[BudgetItemOut]


async def _detail(ctx: SiteContext, plan: BudgetPlan) -> BudgetPlanDetail:
    items = await svc.plan_items(ctx.session, plan.id)
    total = sum((i.annual_amount for i in items), ZERO)
    return BudgetPlanDetail(
        **BudgetPlanOut.of(plan, (total, len(items))).model_dump(),
        items=[BudgetItemOut.of(i) for i in items],
    )


async def _plan(ctx: SiteContext, plan_id: uuid.UUID) -> BudgetPlan:
    plan = await svc.get_plan(ctx.session, plan_id)
    if plan is None:
        raise NotFoundError("İşletme projesi bulunamadı.")
    return plan


@router.get("/budget-plans", summary="İşletme projeleri")
async def list_plans(ctx: FinanceRead, paging: Paging) -> Page[BudgetPlanOut]:
    session = ctx.session
    total = await session.scalar(select(func.count()).select_from(BudgetPlan)) or 0
    plans = list(
        await session.scalars(
            select(BudgetPlan)
            .order_by(BudgetPlan.fiscal_year.desc(), BudgetPlan.created_at.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    totals = await svc.plan_totals(session, [p.id for p in plans])
    return Page(
        items=[BudgetPlanOut.of(p, totals.get(p.id)) for p in plans],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get(
    "/budget-plans/current",
    summary="Geçerli (kesinleşmiş) işletme projesi",
    responses={404: {"description": "Kesinleşmiş proje yok"}},
)
async def current_plan(ctx: FinanceRead) -> BudgetPlanDetail:
    plan = await svc.current_plan(ctx.session)
    if plan is None:
        raise NotFoundError("Kesinleşmiş işletme projesi yok.")
    return await _detail(ctx, plan)


@router.get("/budget-plans/{plan_id}", summary="İşletme projesi ayrıntısı")
async def get_plan(plan_id: uuid.UUID, ctx: FinanceRead) -> BudgetPlanDetail:
    return await _detail(ctx, await _plan(ctx, plan_id))


class BudgetPlanCreate(BaseModel):
    fiscal_year: int = Field(ge=2000, le=2100)
    name: str = Field(min_length=2, max_length=120)


@router.post("/budget-plans", status_code=status.HTTP_201_CREATED, summary="Taslak proje oluştur")
async def create_plan(ctx: BudgetManage, body: BudgetPlanCreate) -> Written[BudgetPlanDetail]:
    try:
        plan = await svc.create_plan(ctx.session, fiscal_year=body.fiscal_year, name=body.name)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _detail(ctx, plan),
        message=f"{plan.fiscal_year} işletme projesi taslağı oluşturuldu.",
    )


class BudgetItemIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    expense_category_id: uuid.UUID
    charge_type_id: uuid.UUID
    allocation_rule_id: uuid.UUID
    annual_amount: Money = Field(description="yıllık toplam")
    frequency: Frequency = Frequency.MONTHLY
    scope_kind: ScopeKind = ScopeKind.WHOLE_SITE
    scope_block_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    scope_unit_type_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    scope_usage: UnitUsage | None = None
    sort_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _non_negative(self) -> BudgetItemIn:
        if self.annual_amount < ZERO:
            raise ValueError("Yıllık tutar eksi olamaz.")
        return self

    def fields(self) -> svc.ItemFields:
        return svc.ItemFields(
            name=self.name,
            expense_category_id=self.expense_category_id,
            charge_type_id=self.charge_type_id,
            allocation_rule_id=self.allocation_rule_id,
            annual_amount=self.annual_amount,
            frequency=self.frequency,
            scope_kind=self.scope_kind,
            scope_block_ids=tuple(dict.fromkeys(self.scope_block_ids)),
            scope_unit_type_ids=tuple(dict.fromkeys(self.scope_unit_type_ids)),
            scope_usage=self.scope_usage,
            sort_order=self.sort_order,
        )


async def _item(ctx: SiteContext, plan: BudgetPlan, item_id: uuid.UUID) -> BudgetItem:
    item: BudgetItem | None = await ctx.session.scalar(
        select(BudgetItem).where(BudgetItem.id == item_id, BudgetItem.budget_plan_id == plan.id)
    )
    if item is None:
        raise NotFoundError("Kalem bulunamadı.")
    return item


@router.post(
    "/budget-plans/{plan_id}/items", status_code=status.HTTP_201_CREATED, summary="Kalem ekle"
)
async def add_item(
    plan_id: uuid.UUID, ctx: BudgetManage, body: BudgetItemIn
) -> Written[BudgetPlanDetail]:
    plan = await _plan(ctx, plan_id)
    try:
        item = await svc.add_item(ctx.session, plan, body.fields())
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _detail(ctx, plan),
        message=f"'{item.name}' kalemi eklendi ({format_money_tr(item.annual_amount)}/yıl).",
    )


@router.put("/budget-plans/{plan_id}/items/{item_id}", summary="Kalemi güncelle (taslakta)")
async def update_item(
    plan_id: uuid.UUID, item_id: uuid.UUID, ctx: BudgetManage, body: BudgetItemIn
) -> Written[BudgetPlanDetail]:
    plan = await _plan(ctx, plan_id)
    item = await _item(ctx, plan, item_id)
    try:
        await svc.update_item(ctx.session, plan, item, body.fields())
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(data=await _detail(ctx, plan), message=f"'{item.name}' kalemi güncellendi.")


@router.delete("/budget-plans/{plan_id}/items/{item_id}", summary="Kalemi kaldır (taslakta)")
async def remove_item(
    plan_id: uuid.UUID, item_id: uuid.UUID, ctx: BudgetManage
) -> Written[BudgetPlanDetail]:
    plan = await _plan(ctx, plan_id)
    item = await _item(ctx, plan, item_id)
    name = item.name
    try:
        await svc.remove_item(ctx.session, plan, item)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(data=await _detail(ctx, plan), message=f"'{name}' kalemi kaldırıldı.")


class NotifyIn(BaseModel):
    notified_on: date = Field(description="kat maliklerine tebliğ tarihi")


@router.post("/budget-plans/{plan_id}/notify", summary="Tebliğ edildi olarak işaretle")
async def notify_plan(
    plan_id: uuid.UUID, ctx: BudgetManage, body: NotifyIn, today: TodayDep
) -> Written[BudgetPlanDetail]:
    plan = await _plan(ctx, plan_id)
    try:
        await svc.notify(ctx.session, plan, body.notified_on, today)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    deadline = plan.objection_deadline
    return Written(
        data=await _detail(ctx, plan),
        message=f"Proje tebliğ edildi. İtiraz süresi {deadline:%d.%m.%Y} günü doluyor.",
    )


@router.post("/budget-plans/{plan_id}/finalize", summary="Kesinleştir")
async def finalize_plan(
    plan_id: uuid.UUID, ctx: BudgetManage, today: TodayDep
) -> Written[BudgetPlanDetail]:
    plan = await _plan(ctx, plan_id)
    try:
        previous = await svc.finalize(ctx.session, plan, today)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    message = f"{plan.fiscal_year} işletme projesi kesinleşti; tahakkuklar bu projeden kesilecek."
    if previous:
        message += f" Önceki {len(previous)} proje geçerliliğini yitirdi."
    return Written(data=await _detail(ctx, plan), message=message)
