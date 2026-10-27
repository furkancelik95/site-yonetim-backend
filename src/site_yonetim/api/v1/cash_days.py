"""Kasa gün sonu sayımı — Yazılım Dokümanı FIN-11, servis isteği 28.

Okuma `finance.cash.read`; kapatma `finance.cash.manage`. Fark varsa gerekçe zorunlu (422
`difference_reason`); "Kasa farkı" onay kuralı varsa 202 (gün onay bekler). Kapanan güne kasa
hareketi 422 `cash_day_closed`.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import finance_error
from site_yonetim.api.v1.finance_ops_common import actor, approval_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.text import format_date_tr, format_money_tr
from site_yonetim.models import CashDay
from site_yonetim.services import cash as cash_svc
from site_yonetim.services import cash_days as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/cash-accounts/{account_id}/days", tags=["kasa ve banka"])
CashRead = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_CASH_READ))]
CashManage = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_CASH_MANAGE))]


class CashDayOut(BaseModel):
    id: uuid.UUID
    date: dt.date
    system_balance: Money
    counted: Money
    difference: Money = Field(description="sayım − sistem (eksi: eksik)")
    reason: str | None
    status: Literal["closed", "approval_pending", "rejected"]
    approval_request_id: uuid.UUID | None
    closed_by: str
    closed_at: dt.datetime | None


def _out(row: CashDay) -> CashDayOut:
    return CashDayOut(
        id=row.id,
        date=row.date,
        system_balance=row.system_balance,
        counted=row.counted,
        difference=row.difference,
        reason=row.reason,
        status=row.status,  # type: ignore[arg-type]
        approval_request_id=row.approval_request_id,
        closed_by=row.closed_by,
        closed_at=row.closed_at,
    )


class CashDaysOut(BaseModel):
    kind: str = Field(description="cash · bank (bankada sayım yok)")
    open_days: list[dt.date] = Field(description="son 60 günde hareketli, sayılmamış günler")
    closed: list[CashDayOut] = Field(description="son 90 sayım, yeniden eskiye")


@router.get("", summary="Kasa günleri: açık günler ve sayımlar")
async def list_days(account_id: uuid.UUID, ctx: CashRead, today: TodayDep) -> CashDaysOut:
    account = await cash_svc.get_account(ctx.session, account_id)
    if account is None:
        raise NotFoundError("Kasa/banka hesabı bulunamadı.")
    view = await svc.overview(ctx.session, account, today)
    return CashDaysOut(
        kind=view.kind, open_days=view.open_days, closed=[_out(r) for r in view.closed]
    )


class CashDayCloseIn(BaseModel):
    date: dt.date
    counted: Money
    reason: str | None = Field(default=None, max_length=500, description="fark varsa ≥ 10 karakter")


@router.post(
    "/close",
    status_code=HTTPStatus.CREATED,
    summary="Gün sonu sayımıyla kapat",
    responses={202: {"description": "Fark onaya gönderildi"}},
)
async def close_day(
    account_id: uuid.UUID,
    body: CashDayCloseIn,
    ctx: CashManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[CashDayOut]:
    """409 `day_closed`, `not_cash`; 422 `difference_reason`."""
    account = await cash_svc.get_account(ctx.session, account_id)
    if account is None:
        raise NotFoundError("Kasa/banka hesabı bulunamadı.")
    try:
        result = await svc.close_day(
            ctx.session,
            ctx.site,
            account,
            day=body.date,
            counted=body.counted,
            reason=body.reason,
            actor=actor(current),
            today=today,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    await ctx.session.commit()
    row = result.day
    day = format_date_tr(row.date)
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        message = (
            f"{day} kasa farkı ({format_money_tr(row.difference)}) onaya gönderildi "
            f"(talep #{result.request.number}); onaylanınca gün kapanır."
        )
    elif row.difference == 0:
        message = f"{day} kasa günü kapandı; sayım sistemle aynı."
    else:
        message = (
            f"{day} kasa günü {format_money_tr(abs(row.difference))} farkla gerekçeli kapandı."
        )
    return Written(data=_out(row), message=message)
