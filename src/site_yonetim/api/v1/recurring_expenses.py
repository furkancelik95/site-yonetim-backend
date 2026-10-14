"""Tekrarlanan gider — frontend servis isteği 07 (backend issue #36).

Okuma `expenses.read`, yazma `expenses.manage`; otomatik ödeme (`auto_pay`) açmak ayrıca
`finance.cash.manage` ister. Giderleri gece işi yazar (`cli run-recurring-expenses`).
Tanımlar denetim kaydında.
"""

import datetime as dt
import uuid
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import finance_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.models import RecurringExpense, RecurringExpenseRun
from site_yonetim.services import recurring_expenses as svc

router = APIRouter(prefix="/sites/{slug}/recurring-expenses", tags=["gider"])
ExpensesRead = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
ExpensesManage = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_MANAGE))]


class RecurringIn(BaseModel):
    description: str = Field(max_length=300)
    expense_category_id: uuid.UUID
    amount: Money
    vendor: str | None = Field(default=None, max_length=200)
    day_of_month: int = Field(description="1–28")
    auto_pay: bool = False
    cash_account_id: uuid.UUID | None = None
    is_active: bool = True


class RecurringPatch(BaseModel):
    description: str | None = Field(default=None, max_length=300)
    expense_category_id: uuid.UUID | None = None
    amount: Money | None = None
    vendor: str | None = Field(default=None, max_length=200)
    day_of_month: int | None = None
    auto_pay: bool | None = None
    cash_account_id: uuid.UUID | None = None
    is_active: bool | None = None


class RecurringOut(BaseModel):
    id: uuid.UUID
    description: str
    expense_category_id: uuid.UUID
    amount: Money
    vendor: str | None
    day_of_month: int
    is_active: bool
    auto_pay: bool
    cash_account_id: uuid.UUID | None
    next_run_on: dt.date | None = Field(description="durdurulmuşsa null; İstanbul tarihi")
    last_created_on: dt.date | None
    created_at: dt.datetime


def _out(
    item: RecurringExpense,
    today: dt.date,
    run: RecurringExpenseRun | None,
    last: dt.date | None,
) -> RecurringOut:
    return RecurringOut(
        id=item.id,
        description=item.description,
        expense_category_id=item.expense_category_id,
        amount=item.amount,
        vendor=item.vendor,
        day_of_month=item.day_of_month,
        is_active=item.is_active,
        auto_pay=item.auto_pay,
        cash_account_id=item.cash_account_id,
        next_run_on=svc.next_run(item, today, run),
        last_created_on=last,
        created_at=item.created_at,
    )


async def _one(ctx: SiteContext, item: RecurringExpense, today: dt.date) -> RecurringOut:
    await ctx.session.refresh(item, ["created_at"])
    runs = await svc.runs_this_month(ctx.session, today)
    last = (await svc.last_created(ctx.session)).get(item.id)
    return _out(item, today, runs.get(item.id), last)


def _needs_cash(ctx: SiteContext, auto_pay: bool) -> None:
    if auto_pay and not ctx.access.can(Permission.FINANCE_CASH_MANAGE):
        raise ForbiddenError("Otomatik ödeme için kasa yetkisi gerekiyor.")


async def _item(ctx: SiteContext, item_id: uuid.UUID) -> RecurringExpense:
    item = await svc.get(ctx.session, item_id)
    if item is None:
        raise NotFoundError("Tekrarlanan gider bulunamadı.")
    return item


@router.get("", summary="Tekrarlanan giderler")
async def list_recurring(ctx: ExpensesRead, today: TodayDep) -> list[RecurringOut]:
    items = await svc.listing(ctx.session)
    runs = await svc.runs_this_month(ctx.session, today)
    last = await svc.last_created(ctx.session)
    return [_out(i, today, runs.get(i.id), last.get(i.id)) for i in items]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Tekrarlanan gider tanımla")
async def create_recurring(
    body: RecurringIn, ctx: ExpensesManage, today: TodayDep
) -> Written[RecurringOut]:
    _needs_cash(ctx, body.auto_pay)
    try:
        item = await svc.create(ctx.session, svc.Definition(**dict(body)), today=today)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    out = await _one(ctx, item, today)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f'"{item.description}" her ayın {item.day_of_month}. günü kaydedilecek.',
    )


@router.patch("/{item_id}", summary="Güncelle ya da durdur (`is_active`)")
async def update_recurring(
    item_id: uuid.UUID, body: RecurringPatch, ctx: ExpensesManage, today: TodayDep
) -> Written[RecurringOut]:
    item = await _item(ctx, item_id)
    data = replace(svc.current(item), **{k: getattr(body, k) for k in body.model_fields_set})
    _needs_cash(ctx, data.auto_pay and not item.auto_pay)
    was_active = item.is_active
    try:
        await svc.update(ctx.session, item, data, today=today)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    out = await _one(ctx, item, today)
    await ctx.session.commit()
    if was_active and not item.is_active:
        message = f'"{item.description}" durduruldu.'
    elif not was_active and item.is_active:
        day = item.day_of_month
        message = f'"{item.description}" yeniden başlatıldı; her ayın {day}. günü kaydedilecek.'
    else:
        message = f'"{item.description}" güncellendi.'
    return Written(data=out, message=message)


@router.delete("/{item_id}", summary="Tanımı kaldır (oluşmuş giderler yerinde kalır)")
async def remove_recurring(
    item_id: uuid.UUID, ctx: ExpensesManage, today: TodayDep, now: NowDep
) -> Written[RecurringOut]:
    item = await _item(ctx, item_id)
    await svc.remove(ctx.session, item, now=now)
    out = await _one(ctx, item, today)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f'"{item.description}" kaldırıldı. Daha önce oluşturulan giderler yerinde kalır.',
    )
