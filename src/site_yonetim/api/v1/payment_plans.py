"""Ödeme planı — Yazılım Dokümanı FIN-12, FIN-15, §8.9; servis isteği 30.

Okuma `finance.read`; oluşturma/iptal `finance.payment.record` (para yazar: `Idempotency-Key`).
"Ödeme planı" onay kuralı varsa 202 (plan onay bekler, onaylanınca deftere yazılır). Sakin ucu
(`/resident/payment-plans`) yalnız oturum sahibinin hesaplarını döner.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    FinanceRead,
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.api.v1.resident import Resident, my_accounts
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.debt_tracking import PlanStatus
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import LedgerAccount, PaymentPlan
from site_yonetim.services import accounts as accounts_svc
from site_yonetim.services import payment_plans as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}", tags=["borç takibi"])
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]
Paging = Annotated[PageParams, Depends()]


class PlanInstallmentOut(BaseModel):
    no: int
    due_date: dt.date
    amount: Money
    paid: Money
    status: Literal["paid", "partial", "open", "overdue"]


class PaymentPlanOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    person_name: str
    reference_code: str
    total: Money
    installment_count: int
    first_due_date: dt.date
    tolerance_days: int
    note: str | None
    status: PlanStatus
    paid_total: Money
    remaining: Money
    installments: list[PlanInstallmentOut]
    approval_request_id: uuid.UUID | None
    created_by: str
    created_at: dt.datetime
    activated_on: dt.date | None
    cancelled_at: dt.datetime | None
    cancel_reason: str | None


async def _outs(ctx: SiteContext, plans: list[PaymentPlan], today: dt.date) -> list[PaymentPlanOut]:
    views = await svc.installments(ctx.session, plans, today)
    rows = {
        r.account.id: r
        for r in [
            accounts_svc.to_row(tuple(v))
            for v in await ctx.session.execute(
                accounts_svc.accounts_query().where(
                    LedgerAccount.id.in_({p.account_id for p in plans})
                )
            )
        ]
    }
    out: list[PaymentPlanOut] = []
    for plan in plans:
        items = views[plan.id]
        row = rows[plan.account_id]
        paid = sum((i.paid for i in items), ZERO)
        out.append(
            PaymentPlanOut(
                id=plan.id,
                account_id=plan.account_id,
                unit_id=row.account.unit_id,
                unit_name=row.unit_name,
                person_name=row.person_name,
                reference_code=row.account.reference_code,
                total=plan.total,
                installment_count=plan.installment_count,
                first_due_date=plan.first_due_date,
                tolerance_days=plan.tolerance_days,
                note=plan.note,
                status=svc.derived_status(plan, items),
                paid_total=paid,
                remaining=plan.total - paid,
                installments=[
                    PlanInstallmentOut(
                        no=i.no,
                        due_date=i.due_date,
                        amount=i.amount,
                        paid=i.paid,
                        status=i.status.value,
                    )
                    for i in items
                ],
                approval_request_id=plan.approval_request_id,
                created_by=plan.created_by,
                created_at=plan.created_at,
                activated_on=plan.activated_on,
                cancelled_at=plan.cancelled_at,
                cancel_reason=plan.cancel_reason,
            )
        )
    return out


class PaymentPlanCountsOut(BaseModel):
    active: int
    broken: int
    completed: int
    cancelled: int
    approval_pending: int
    remaining_total: Money = Field(description="yürürlükteki ve bozulan planlarda kalan tutar")


class PaymentPlanListOut(Page[PaymentPlanOut]):
    counts: PaymentPlanCountsOut


@router.get("/payment-plans", summary="Ödeme planları (sayfalı, durum süzgeçli)")
async def list_plans(
    ctx: FinanceRead,
    paging: Paging,
    today: TodayDep,
    plan_status: Annotated[PlanStatus | None, Query(alias="status")] = None,
) -> PaymentPlanListOut:
    pairs = await svc.plan_statuses(ctx.session, today)
    counts = dict.fromkeys(PlanStatus, 0)
    for _plan, s in pairs:
        counts[s] += 1
    chosen = [p for p, s in pairs if plan_status is None or s is plan_status]
    page = chosen[paging.offset : paging.offset + paging.page_size]
    in_force = [p for p, s in pairs if s in (PlanStatus.ACTIVE, PlanStatus.BROKEN)]
    in_force_out = await _outs(ctx, in_force, today) if in_force else []
    return PaymentPlanListOut(
        items=await _outs(ctx, page, today) if page else [],
        page=paging.page,
        page_size=paging.page_size,
        total=len(chosen),
        counts=PaymentPlanCountsOut(
            active=counts[PlanStatus.ACTIVE],
            broken=counts[PlanStatus.BROKEN],
            completed=counts[PlanStatus.COMPLETED],
            cancelled=counts[PlanStatus.CANCELLED],
            approval_pending=counts[PlanStatus.APPROVAL_PENDING],
            remaining_total=sum((o.remaining for o in in_force_out), ZERO),
        ),
    )


async def _account(ctx: SiteContext, account_id: uuid.UUID) -> LedgerAccount:
    row = await accounts_svc.account_row(ctx.session, account_id)
    if row is None:
        raise NotFoundError("Hesap bulunamadı.")
    return row.account


@router.get("/accounts/{account_id}/payment-plans", summary="Hesabın ödeme planları")
async def account_plans(
    account_id: uuid.UUID, ctx: FinanceRead, today: TodayDep
) -> list[PaymentPlanOut]:
    await _account(ctx, account_id)
    plans = list(
        await ctx.session.scalars(
            select(PaymentPlan)
            .where(PaymentPlan.account_id == account_id)
            .order_by(PaymentPlan.created_at.desc())
        )
    )
    return await _outs(ctx, plans, today) if plans else []


class PaymentPlanIn(BaseModel):
    amount: Money | None = None
    installment_count: int | None = None
    first_due_date: dt.date | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/accounts/{account_id}/payment-plans",
    status_code=status.HTTP_201_CREATED,
    summary="Ödeme planı oluştur (onay kuralı varsa 202)",
    responses={202: {"description": "Onaya gönderildi"}},
    response_model=Written[PaymentPlanOut],
)
async def create_plan(
    account_id: uuid.UUID,
    body: PaymentPlanIn,
    ctx: PaymentRecord,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[PaymentPlanOut] | JSONResponse:
    """409 `plan_exists`; 422 `fields.amount|installment_count|first_due_date`."""
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    account = await _account(ctx, account_id)
    try:
        created = await svc.create(
            ctx.session,
            ctx.site,
            account,
            amount=body.amount,
            installment_count=body.installment_count,
            first_due_date=body.first_due_date,
            note=body.note,
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
    data = (await _outs(ctx, [created.plan], today))[0]
    code = HTTPStatus.ACCEPTED if created.request is not None else HTTPStatus.CREATED
    if created.request is not None:
        message = (
            f"Ödeme planı onaya gönderildi (talep #{created.request.number}); onaylanınca "
            "taksitler deftere yazılır."
        )
    else:
        message = (
            f"{created.plan.installment_count} taksitli ödeme planı oluşturuldu "
            f"({format_money_tr(created.plan.total)}). Yapılandırılan kalemler kapandı, taksitler "
            f"vadeli borç olarak açıldı; {created.plan.tolerance_days} günden fazla geciken "
            "taksit planı bozar."
        )
    written = Written(data=data, message=message)
    response.status_code = code
    await commit_with_key(ctx.session, idem, code, written)
    return written


class PaymentPlanCancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/payment-plans/{plan_id}/cancel", summary="Ödeme planını iptal et")
async def cancel_plan(
    plan_id: uuid.UUID,
    body: PaymentPlanCancelIn,
    ctx: PaymentRecord,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
) -> Written[PaymentPlanOut]:
    plan = await svc.get(ctx.session, plan_id, lock=True)
    if plan is None:
        raise NotFoundError("Ödeme planı bulunamadı.")
    try:
        await svc.cancel(
            ctx.session, plan, reason=body.reason, actor=actor(current), today=today, now=now
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=(await _outs(ctx, [plan], today))[0],
        message="Ödeme planı iptal edildi; ödenmemiş tutar ilk vadesiyle yeniden borç olarak "
        "açıldı ve normal hatırlatma kurallarına döndü.",
    )


# --- Sakin ------------------------------------------------------------------------------


class ResidentPaymentPlanOut(BaseModel):
    id: uuid.UUID
    unit_name: str
    reference_code: str
    total: Money
    installment_count: int
    paid_total: Money
    status: PlanStatus
    installments: list[PlanInstallmentOut]


@router.get("/resident/payment-plans", summary="Sakin: ödeme planım", tags=["sakin"])
async def resident_plans(ctx: Resident, today: TodayDep) -> list[ResidentPaymentPlanOut]:
    """Yalnız oturum sahibinin hesapları; onay bekleyen/reddedilen plan gösterilmez."""
    accounts = {a.account.id: a for a in await my_accounts(ctx)}
    if not accounts:
        return []
    plans = list(
        await ctx.session.scalars(
            select(PaymentPlan)
            .where(
                PaymentPlan.account_id.in_(accounts),
                PaymentPlan.status.in_(["active", "cancelled"]),
            )
            .order_by(PaymentPlan.created_at.desc())
        )
    )
    views = await svc.installments(ctx.session, plans, today)
    return [
        ResidentPaymentPlanOut(
            id=p.id,
            unit_name=accounts[p.account_id].unit_name,
            reference_code=accounts[p.account_id].account.reference_code,
            total=p.total,
            installment_count=p.installment_count,
            paid_total=sum((i.paid for i in views[p.id]), ZERO),
            status=svc.derived_status(p, views[p.id]),
            installments=[
                PlanInstallmentOut(
                    no=i.no,
                    due_date=i.due_date,
                    amount=i.amount,
                    paid=i.paid,
                    status=i.status.value,
                )
                for i in views[p.id]
            ],
        )
        for p in plans
    ]
