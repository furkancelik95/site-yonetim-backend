"""Hizmet sözleşmeleri — frontend servis isteği 16 (backend #44).

Çekirdek (modül yok). Okuma `expenses.read`, yazma `contracts.manage` (Yönetici, Muhasebe).
`days_left` ve `state` sunucuda sitenin bugününe (Europe/Istanbul) göre hesaplanır.
`amount`/`period` bugün geçerli bedeldir (bedel geçmişinden); ayrıntıda geçmiş de döner.
"""

import dataclasses
import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_ops_common import actor
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.contract_performance import AmountSource
from site_yonetim.domain.management import (
    ContractCategory,
    ContractPeriod,
    ContractState,
    contract_state,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Contract, ContractAmount
from site_yonetim.services import contracts as svc

router = APIRouter(prefix="/sites/{slug}/contracts", tags=["sözleşme"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
Manager = Annotated[SiteContext, Depends(require_permission(Permission.CONTRACTS_MANAGE))]
LIST_MAX = 500


class ContractIn(BaseModel):
    vendor: str | None = Field(default=None, max_length=400)
    subject: str | None = Field(default=None, max_length=600)
    category: str | None = Field(default=None, max_length=30)
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    amount: Money | None = None
    period: str | None = Field(default=None, max_length=20)
    notice_days: int | None = 30
    auto_renew: bool = False
    note: str | None = Field(default=None, max_length=4000)


class ContractPatch(BaseModel):
    vendor: str | None = Field(default=None, max_length=400)
    subject: str | None = Field(default=None, max_length=600)
    category: str | None = Field(default=None, max_length=30)
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    amount: Money | None = None
    period: str | None = Field(default=None, max_length=20)
    notice_days: int | None = None
    auto_renew: bool | None = None
    note: str | None = Field(default=None, max_length=4000)
    is_archived: bool | None = Field(default=None, description="true → arşive kaldır")


class ContractOut(BaseModel):
    id: uuid.UUID
    vendor: str
    subject: str
    category: ContractCategory
    start_date: dt.date
    end_date: dt.date
    amount: Money | None
    period: ContractPeriod | None
    notice_days: int
    auto_renew: bool
    note: str | None
    is_archived: bool
    days_left: int = Field(description="bugünden bitişe gün; geçmişse eksi")
    state: ContractState
    created_at: dt.datetime

    @classmethod
    def of(
        cls,
        item: Contract,
        today: dt.date,
        current: tuple[Decimal | None, str | None] | None = None,
    ) -> ContractOut:
        amount, period = current if current is not None else (item.amount, item.period)
        days_left, state = contract_state(
            end_date=item.end_date,
            notice_days=item.notice_days,
            archived=item.is_archived,
            today=today,
        )
        return cls(
            id=item.id,
            vendor=item.vendor,
            subject=item.subject,
            category=ContractCategory(item.category),
            start_date=item.start_date,
            end_date=item.end_date,
            amount=amount,
            period=ContractPeriod(period) if period else None,
            notice_days=item.notice_days,
            auto_renew=item.auto_renew,
            note=item.note,
            is_archived=item.is_archived,
            days_left=days_left,
            state=state,
            created_at=item.created_at,
        )


class ContractAmountOut(BaseModel):
    amount: Money | None
    period: ContractPeriod | None
    effective_from: dt.date
    source: AmountSource
    note: str | None
    created_by: str
    created_at: dt.datetime

    @classmethod
    def of(cls, row: ContractAmount) -> ContractAmountOut:
        return cls(
            amount=row.amount,
            period=ContractPeriod(row.period) if row.period else None,
            effective_from=row.effective_from,
            source=AmountSource(row.source),
            note=row.note,
            created_by=row.created_by,
            created_at=row.created_at,
        )


class ContractDetailOut(ContractOut):
    latest_amount: Money | None = Field(description="en son yazılan bedel (ileri tarihli olabilir)")
    amount_history: list[ContractAmountOut] = Field(description="bedel geçmişi, eskiden yeniye")


@router.get("", summary="Sözleşmeler (bitişe göre; dizi)")
async def list_contracts(
    ctx: Reader,
    today: TodayDep,
    archived: Annotated[bool, Query(description="true → arşiv")] = False,
) -> list[ContractOut]:
    rows = list(await ctx.session.scalars(svc.list_query(archived).limit(LIST_MAX)))
    current = await svc.current_amounts(ctx.session, [c.id for c in rows], today)
    return [ContractOut.of(c, today, current.get(c.id)) for c in rows]


@router.get("/{contract_id}", summary="Sözleşme ayrıntısı (bedel geçmişiyle)")
async def get_contract(contract_id: uuid.UUID, ctx: Reader, today: TodayDep) -> ContractDetailOut:
    item = await ctx.session.get(Contract, contract_id)
    if item is None:
        raise NotFoundError("Sözleşme bulunamadı.")
    history = await svc.amount_rows(ctx.session, item.id)
    base = ContractOut.of(item, today, svc.amount_at(history, today) if history else None)
    return ContractDetailOut(
        **base.model_dump(),
        latest_amount=item.amount,
        amount_history=[ContractAmountOut.of(r) for r in history],
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Sözleşme ekle")
async def create_contract(
    body: ContractIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[ContractOut]:
    fields = {k: getattr(body, k) for k in ContractIn.model_fields}
    try:
        item = await svc.create(ctx.session, svc.ContractData(**fields), actor=actor(current))
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.refresh(item, ["created_at"])
    out = ContractOut.of(item, today)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.vendor} sözleşmesi eklendi.")


@router.patch("/{contract_id}", summary="Sözleşmeyi düzenle ya da arşivle (kısmi)")
async def update_contract(
    contract_id: uuid.UUID,
    body: ContractPatch,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[ContractOut]:
    item = await svc.get(ctx.session, contract_id)
    if item is None:
        raise NotFoundError("Sözleşme bulunamadı.")
    was_archived = item.is_archived
    changes = {k: getattr(body, k) for k in body.model_fields_set if k != "is_archived"}
    try:
        await svc.update(
            ctx.session,
            item,
            dataclasses.replace(svc.current(item), **changes),
            archived=body.is_archived,
            actor=actor(current),
            today=today,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    history = await svc.amount_rows(ctx.session, item.id)
    out = ContractOut.of(item, today, svc.amount_at(history, today) if history else None)
    await ctx.session.commit()
    if item.is_archived and not was_archived:
        message = f"{item.vendor} sözleşmesi arşive kaldırıldı."
    elif was_archived and not item.is_archived:
        message = f"{item.vendor} sözleşmesi arşivden çıkarıldı."
    else:
        message = f"{item.vendor} sözleşmesi güncellendi."
    return Written(data=out, message=message)
