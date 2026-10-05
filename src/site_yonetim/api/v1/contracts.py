"""Hizmet sözleşmeleri — frontend servis isteği 16 (backend #44).

Çekirdek (modül yok). Okuma `expenses.read`, yazma `contracts.manage` (Yönetici, Muhasebe).
`days_left` ve `state` sunucuda sitenin bugününe (Europe/Istanbul) göre hesaplanır.
"""

import dataclasses
import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.management import (
    ContractCategory,
    ContractPeriod,
    ContractState,
    contract_state,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Contract
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
    def of(cls, item: Contract, today: dt.date) -> ContractOut:
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
            amount=item.amount,
            period=ContractPeriod(item.period) if item.period else None,
            notice_days=item.notice_days,
            auto_renew=item.auto_renew,
            note=item.note,
            is_archived=item.is_archived,
            days_left=days_left,
            state=state,
            created_at=item.created_at,
        )


@router.get("", summary="Sözleşmeler (bitişe göre; dizi)")
async def list_contracts(
    ctx: Reader,
    today: TodayDep,
    archived: Annotated[bool, Query(description="true → arşiv")] = False,
) -> list[ContractOut]:
    rows = await ctx.session.scalars(svc.list_query(archived).limit(LIST_MAX))
    return [ContractOut.of(c, today) for c in rows]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Sözleşme ekle")
async def create_contract(body: ContractIn, ctx: Manager, today: TodayDep) -> Written[ContractOut]:
    fields = {k: getattr(body, k) for k in ContractIn.model_fields}
    try:
        item = await svc.create(ctx.session, svc.ContractData(**fields))
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.refresh(item, ["created_at"])
    out = ContractOut.of(item, today)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.vendor} sözleşmesi eklendi.")


@router.patch("/{contract_id}", summary="Sözleşmeyi düzenle ya da arşivle (kısmi)")
async def update_contract(
    contract_id: uuid.UUID, body: ContractPatch, ctx: Manager, today: TodayDep
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
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = ContractOut.of(item, today)
    await ctx.session.commit()
    if item.is_archived and not was_archived:
        message = f"{item.vendor} sözleşmesi arşive kaldırıldı."
    elif was_archived and not item.is_archived:
        message = f"{item.vendor} sözleşmesi arşivden çıkarıldı."
    else:
        message = f"{item.vendor} sözleşmesi güncellendi."
    return Written(data=out, message=message)
