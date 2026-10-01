"""Site panosu ve modül yönetimi — docs/06 §2.4, §2.13.

Pano `finance.read` ister; bölümler kullanıcının iznine ve sitenin modüllerine göre doldurulur
(izni ya da modülü yoksa o bölüm `null`). Kişi adları `people.read` yoksa `null`.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.payments import AccountOut
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import CORE_MODULES, ModuleKey, ModuleRuleError, statuses
from site_yonetim.domain.operations import Importance
from site_yonetim.models import SiteModule
from site_yonetim.services import dashboard as svc
from site_yonetim.services.accounts import debtor_summary, overdue_days
from site_yonetim.services.sites import plan_modules, set_module_enabled

router = APIRouter(prefix="/sites/{slug}", tags=["pano ve modüller"])
FinanceRead = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_READ))]
ModulesManage = Annotated[SiteContext, Depends(require_permission(Permission.MODULES_MANAGE))]


class FinanceOut(BaseModel):
    total_charged: Money = Field(description="kesilen tüm tahakkuk (geçerli koşular)")
    total_collected: Money = Field(description="onaylı tahsilatların toplamı")
    month_charged: Money = Field(description="bu ay kesilen")
    month_collected: Money = Field(description="bu ay tahsil edilen")
    open_balance: Money = Field(description="borçlu hesapların toplam bakiyesi")
    debtor_count: int
    over_30_days: Money
    over_60_days: Money
    average_debt: Money


class DebtorRow(AccountOut):
    overdue_days: int


class RequestsOut(BaseModel):
    open: int
    in_progress: int
    waiting: int
    urgent: int
    total: int


class AnnouncementRow(BaseModel):
    id: uuid.UUID
    title: str
    importance: Importance
    is_pinned: bool
    published_at: dt.datetime | None


class ExpenseRow(BaseModel):
    id: uuid.UUID
    date: dt.date
    description: str
    amount: Money
    is_paid: bool


class DashboardOut(BaseModel):
    finance: FinanceOut
    top_debtors: list[DebtorRow] = Field(description="bakiyesi en büyük 5 hesap")
    requests: RequestsOut | None = Field(description="talep modülü ve requests.read gerekir")
    announcements: list[AnnouncementRow] | None = Field(description="son 3 duyuru")
    expenses: list[ExpenseRow] | None = Field(description="son 5 gider (expenses.read)")
    cash_balance: Money | None = Field(description="toplam kasa/banka (finance.cash.read)")


@router.get("/dashboard", summary="Site panosu (tek istekte özet)")
async def dashboard(ctx: FinanceRead, today: TodayDep) -> DashboardOut:
    session = ctx.session
    can = ctx.access.can
    totals = await svc.finance_totals(session, today)
    debt = await debtor_summary(session, today)
    names = can(Permission.PEOPLE_READ)

    requests = None
    if ModuleKey.REQUESTS in ctx.modules and can(Permission.REQUESTS_READ):
        counts = await svc.request_counts(session)
        requests = RequestsOut(
            open=counts.open,
            in_progress=counts.in_progress,
            waiting=counts.waiting,
            urgent=counts.urgent,
            total=counts.total,
        )

    announcements = None
    if ModuleKey.ANNOUNCEMENTS in ctx.modules and can(Permission.ANNOUNCEMENTS_READ):
        rows = await svc.recent_announcements(
            session, today, show_all=can(Permission.ANNOUNCEMENTS_PUBLISH)
        )
        announcements = [
            AnnouncementRow(
                id=a.id,
                title=a.title,
                importance=Importance(a.importance),
                is_pinned=a.is_pinned,
                published_at=a.published_at,
            )
            for a in rows
        ]

    expenses = None
    if can(Permission.EXPENSES_READ):
        expenses = [
            ExpenseRow(
                id=e.id,
                date=e.date,
                description=e.description,
                amount=e.amount,
                is_paid=e.paid_on is not None,
            )
            for e in await svc.recent_expenses(session)
        ]

    return DashboardOut(
        finance=FinanceOut(
            total_charged=totals.charged,
            total_collected=totals.collected,
            month_charged=totals.month_charged,
            month_collected=totals.month_collected,
            open_balance=debt.total_balance,
            debtor_count=debt.debtor_count,
            over_30_days=debt.over_30_days,
            over_60_days=debt.over_60_days,
            average_debt=debt.average_balance,
        ),
        top_debtors=[
            DebtorRow(
                **AccountOut.of(r, names=names).model_dump(),
                overdue_days=overdue_days(r.oldest_open_due_date, today),
            )
            for r in await svc.top_debtors(session)
        ],
        requests=requests,
        announcements=announcements,
        expenses=expenses,
        cash_balance=await svc.cash_total(session) if can(Permission.FINANCE_CASH_READ) else None,
    )


# --- Modüller (docs/06 §2.13) ------------------------------------------------------------


class ModuleOut(BaseModel):
    key: ModuleKey
    enabled: bool
    in_plan: bool = Field(description="planda yoksa açılamaz")
    available: bool = Field(description="açık ve planda — menüde gösterilir")
    is_core: bool = Field(description="çekirdek (finans) kapatılamaz")


async def _modules(ctx: SiteContext) -> list[ModuleOut]:
    known = {m.value for m in ModuleKey}
    enabled = {
        ModuleKey(key)
        for key in await ctx.session.scalars(
            select(SiteModule.module_key).where(SiteModule.enabled)
        )
        if key in known
    }
    return [
        ModuleOut(
            key=s.key,
            enabled=s.enabled,
            in_plan=s.in_plan,
            available=s.available,
            is_core=s.key in CORE_MODULES,
        )
        for s in statuses(enabled, await plan_modules(ctx.session, ctx.site))
    ]


@router.get("/modules", summary="Modüller: açık mı, planda var mı")
async def list_modules(ctx: ModulesManage) -> list[ModuleOut]:
    return await _modules(ctx)


@router.post("/modules/{key}/toggle", summary="Modülü aç/kapa (kapatmak veriyi silmez)")
async def toggle_module(key: str, ctx: ModulesManage) -> Written[ModuleOut]:
    try:
        module = ModuleKey(key)
    except ValueError as exc:
        raise NotFoundError("Modül bulunamadı.") from exc
    current = next(m for m in await _modules(ctx) if m.key is module)
    try:
        await set_module_enabled(ctx.session, ctx.site, module, enable=not current.enabled)
    except ModuleRuleError as exc:
        raise ApiError(HTTPStatus.CONFLICT, exc.code, exc.message) from exc
    await ctx.session.commit()
    updated = next(m for m in await _modules(ctx) if m.key is module)
    state = "açıldı" if updated.enabled else "kapatıldı (veriler silinmedi)"
    return Written(data=updated, message=f"'{module.value}' modülü {state}.")
