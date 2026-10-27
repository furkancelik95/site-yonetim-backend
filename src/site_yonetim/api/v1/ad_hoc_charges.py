"""Dönem dışı ücretler — Yazılım Dokümanı FIN-06, servis isteği 31.

Okuma `finance.read`; yazma `finance.charge.post`. `immediate` ücret cariye tek kalem borç olarak
yazılır; `next_period` ücret sonraki tahakkukta "ek kalemler" başlığıyla yazılır. İptal ters
kayıttır. "Dönem dışı ücret" onay kuralı varsa 202 (ücret onay bekler).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, TodayDep
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    ChargePost,
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import AdHocCharge, LedgerAccount, LedgerEntry
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import ad_hoc_charges as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}", tags=["dönem dışı ücretler"])
Paging = Annotated[PageParams, Depends()]
Source = Literal["reservation", "service", "renovation", "penalty", "other"]
Mode = Literal["immediate", "next_period"]
Status = Literal["approval_pending", "posted", "waiting_next_run", "cancelled", "rejected"]


class AdHocSourceSettingOut(BaseModel):
    mode: Mode
    due_days: int = Field(ge=0, le=60)


class AdHocSettingsOut(BaseModel):
    sources: dict[Source, AdHocSourceSettingOut]


@router.get("/settings/ad-hoc-charges", summary="Kaynak bazında yansıma ayarı")
async def get_settings(ctx: FinanceRead) -> AdHocSettingsOut:
    return AdHocSettingsOut.model_validate({"sources": await svc.settings(ctx.session)})


class AdHocSettingsIn(BaseModel):
    sources: dict[str, dict[str, object]]


@router.put("/settings/ad-hoc-charges", summary="Yansıma ayarını kaydet")
async def save_settings(body: AdHocSettingsIn, ctx: ChargePost) -> Written[AdHocSettingsOut]:
    try:
        saved = await svc.save_settings(ctx.session, body.sources)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=AdHocSettingsOut.model_validate({"sources": saved}),
        message="Dönem dışı ücret ayarları kaydedildi; yalnız bundan sonraki ücretleri etkiler.",
    )


class AdHocChargeOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    account_id: uuid.UUID
    account_kind: str
    reference_code: str
    person_name: str
    source: Source
    description: str
    amount: Money
    due_date: dt.date | None = Field(description="yazıldığı vade; bekleyende boşsa tahakkuk vadesi")
    mode: Mode
    status: Status
    approval_request_id: uuid.UUID | None
    charge_run_id: uuid.UUID | None
    created_by: str
    created_at: dt.datetime
    cancelled_at: dt.datetime | None
    cancel_reason: str | None


async def _outs(ctx: ChargePost | FinanceRead, rows: list[AdHocCharge]) -> list[AdHocChargeOut]:
    if not rows:
        return []
    accounts = {
        r.account.id: r
        for r in [
            accounts_svc.to_row(tuple(v))
            for v in await ctx.session.execute(
                accounts_svc.accounts_query().where(
                    LedgerAccount.id.in_({c.account_id for c in rows})
                )
            )
        ]
    }
    entry_ids = [c.ledger_entry_id for c in rows if c.ledger_entry_id]
    dues: dict[uuid.UUID, dt.date | None] = {}
    if entry_ids:
        for entry_id, due in await ctx.session.execute(
            select(LedgerEntry.id, LedgerEntry.due_date).where(LedgerEntry.id.in_(entry_ids))
        ):
            dues[entry_id] = due
    return [
        AdHocChargeOut(
            id=c.id,
            unit_id=c.unit_id,
            unit_name=accounts[c.account_id].unit_name,
            account_id=c.account_id,
            account_kind=c.account_kind,
            reference_code=accounts[c.account_id].account.reference_code,
            person_name=accounts[c.account_id].person_name,
            source=c.source,  # type: ignore[arg-type]
            description=c.description,
            amount=c.amount,
            due_date=dues.get(c.ledger_entry_id) if c.ledger_entry_id else c.due_date,
            mode=c.mode,  # type: ignore[arg-type]
            status=c.status,  # type: ignore[arg-type]
            approval_request_id=c.approval_request_id,
            charge_run_id=c.charge_run_id,
            created_by=c.created_by,
            created_at=c.created_at,
            cancelled_at=c.cancelled_at,
            cancel_reason=c.cancel_reason,
        )
        for c in rows
    ]


@router.get("/ad-hoc-charges", summary="Dönem dışı ücretler (sayfalı)")
async def list_charges(
    ctx: FinanceRead,
    paging: Paging,
    charge_status: Annotated[Status | None, Query(alias="status")] = None,
) -> Page[AdHocChargeOut]:
    query = select(AdHocCharge)
    if charge_status is not None:
        query = query.where(AdHocCharge.status == charge_status)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        await ctx.session.scalars(
            query.order_by(AdHocCharge.created_at.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    return Page(
        items=await _outs(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


class AdHocChargeIn(BaseModel):
    unit_id: uuid.UUID | None = None
    account_kind: str | None = Field(default=None, description="owner · occupant")
    source: str | None = None
    description: str | None = Field(default=None, max_length=200)
    amount: Money | None = None
    due_date: dt.date | None = None


@router.post(
    "/ad-hoc-charges",
    status_code=HTTPStatus.CREATED,
    summary="Dönem dışı ücret ekle (onay kuralı varsa 202)",
    responses={202: {"description": "Onaya gönderildi"}},
    response_model=Written[AdHocChargeOut],
)
async def create_charge(
    body: AdHocChargeIn,
    ctx: ChargePost,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[AdHocChargeOut] | JSONResponse:
    """422 `no_account` (bölümde seçilen türde açık hesap yok)."""
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    try:
        created = await svc.create(
            ctx.session,
            ctx.site,
            svc.NewCharge(
                body.unit_id,
                body.account_kind,
                body.source,
                body.description,
                body.amount,
                body.due_date,
            ),
            actor=actor(current),
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    charge = created.charge
    data = (await _outs(ctx, [charge]))[0]
    amount = format_money_tr(charge.amount)
    if created.request is not None:
        code = HTTPStatus.ACCEPTED
        message = (
            f"{data.unit_name} için {amount} ücret onaya gönderildi (talep "
            f"#{created.request.number}); onaylanınca cariye yansır."
        )
    else:
        code = HTTPStatus.CREATED
        message = (
            f"{data.unit_name} için {amount} ücret cariye yazıldı."
            if charge.status == svc.POSTED
            else f'{data.unit_name} için {amount} ücret bir sonraki aidat tahakkukunda "ek '
            'kalemler" başlığıyla yazılacak.'
        )
    written = Written(data=data, message=message)
    response.status_code = code
    await commit_with_key(ctx.session, idem, code, written)
    return written


class AdHocCancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/ad-hoc-charges/{charge_id}/cancel", summary="Ücreti iptal et (ters kayıt)")
async def cancel_charge(
    charge_id: uuid.UUID, body: AdHocCancelIn, ctx: ChargePost, today: TodayDep, now: NowDep
) -> Written[AdHocChargeOut]:
    charge = await svc.get(ctx.session, charge_id)
    if charge is None:
        raise NotFoundError("Ücret bulunamadı.")
    was_posted = charge.status == svc.POSTED
    try:
        await svc.cancel(ctx.session, charge, reason=body.reason, today=today, now=now)
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=(await _outs(ctx, [charge]))[0],
        message="Ücret ters kayıtla kapatıldı; orijinal satır ekstrede duruyor."
        if was_posted
        else "Bekleyen ücret iptal edildi; tahakkuka eklenmeyecek.",
    )
