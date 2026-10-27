"""Yıllık bütçe ve bütçe–gerçekleşme — Yazılım Dokümanı FIN-17, §8.10 (B17, D-16).

Okuma `finance.read`, yazma `finance.budget.manage`. Bütçe = gider kategorisi × ay; taslak
işletme projesinden önerilebilir. Onaya gönderilen bütçe "Bütçe" onay kuralı varsa onay bekler
(202), yoksa yürürlüğe girer. Revizyon yeni sürümdür. Gerçekleşme gerçekleşen giderlerden.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import BudgetManage, FinanceRead, finance_error
from site_yonetim.api.v1.finance_ops_common import actor, approval_error, form_error
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import Budget, ExpenseCategory
from site_yonetim.services import annual_budgets as svc
from site_yonetim.services.approvals import ApprovalError

router = APIRouter(prefix="/sites/{slug}/budgets", tags=["bütçe"])
Paging = Annotated[PageParams, Depends()]
Status = Literal["draft", "approval_pending", "active", "rejected", "superseded"]


class AnnualBudgetOut(BaseModel):
    id: uuid.UUID
    fiscal_year: int
    version: int
    name: str
    status: Status
    income_target: Money | None = Field(description="aidat ve diğer gelir hedefi (isteğe bağlı)")
    total: Money = Field(description="yıllık gider bütçesi toplamı")
    budget_plan_id: uuid.UUID | None = Field(description="öneri kaynağı işletme projesi")
    approval_request_id: uuid.UUID | None
    created_by: str | None
    created_at: dt.datetime
    activated_at: dt.datetime | None


class BudgetCategoryLine(BaseModel):
    expense_category_id: uuid.UUID
    months: list[Money] = Field(description="Ocak–Aralık, 12 tutar")


class BudgetCategoryOut(BudgetCategoryLine):
    category_name: str
    total: Money


class AnnualBudgetDetail(AnnualBudgetOut):
    lines: list[BudgetCategoryOut]
    month_totals: list[Money] = Field(description="ay başına toplam (12)")


def _out(budget: Budget, total: Decimal | None) -> AnnualBudgetOut:
    return AnnualBudgetOut(
        id=budget.id,
        fiscal_year=budget.fiscal_year,
        version=budget.version,
        name=budget.name,
        status=budget.status,  # type: ignore[arg-type]
        income_target=budget.income_target,
        total=total or ZERO,
        budget_plan_id=budget.budget_plan_id,
        approval_request_id=budget.approval_request_id,
        created_by=budget.created_by,
        created_at=budget.created_at,
        activated_at=budget.activated_at,
    )


async def _names(ctx: SiteContext) -> dict[uuid.UUID, str]:
    return {
        c.id: c.name
        for c in await ctx.session.scalars(
            select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
        )
    }


async def _detail(ctx: SiteContext, budget: Budget) -> AnnualBudgetDetail:
    lines = await svc.lines_of(ctx.session, budget.id)
    names = await _names(ctx)
    rows = [
        BudgetCategoryOut(
            expense_category_id=cat,
            category_name=name,
            months=lines[cat],
            total=sum(lines[cat], ZERO),
        )
        for cat, name in names.items()
        if cat in lines
    ]
    month_totals = [sum((r.months[i] for r in rows), ZERO) for i in range(svc.MONTHS)]
    base = _out(budget, sum(month_totals, ZERO))
    return AnnualBudgetDetail(**base.model_dump(), lines=rows, month_totals=month_totals)


async def _budget(ctx: SiteContext, budget_id: uuid.UUID) -> Budget:
    budget = await svc.get(ctx.session, budget_id)
    if budget is None:
        raise NotFoundError("Bütçe bulunamadı.")
    return budget


@router.get("", summary="Bütçeler (yeniden eskiye)")
async def list_budgets(
    ctx: FinanceRead,
    paging: Paging,
    fiscal_year: Annotated[int | None, Query(description="mali yıl")] = None,
) -> Page[AnnualBudgetOut]:
    query = select(Budget)
    if fiscal_year is not None:
        query = query.where(Budget.fiscal_year == fiscal_year)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    budgets = list(
        await ctx.session.scalars(
            query.order_by(Budget.fiscal_year.desc(), Budget.version.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    totals = await svc.totals(ctx.session, [b.id for b in budgets])
    return Page(
        items=[_out(b, totals.get(b.id)) for b in budgets],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class BudgetCreateIn(BaseModel):
    fiscal_year: int
    name: str = Field(max_length=120)
    income_target: Money | None = None
    budget_plan_id: uuid.UUID | None = Field(
        default=None, description="işletme projesinden öneri: kalemler kategori × aya dağıtılır"
    )


@router.post("", status_code=HTTPStatus.CREATED, summary="Bütçe taslağı aç")
async def create_budget(
    body: BudgetCreateIn, ctx: BudgetManage, current: CurrentUserDep
) -> Written[AnnualBudgetDetail]:
    try:
        budget = await svc.create(
            ctx.session,
            fiscal_year=body.fiscal_year,
            name=body.name,
            income_target=body.income_target,
            budget_plan_id=body.budget_plan_id,
            actor=actor(current),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    data = await _detail(ctx, budget)
    await ctx.session.commit()
    return Written(
        data=data,
        message=f"{budget.fiscal_year} bütçesi (sürüm {budget.version}) taslak olarak açıldı.",
    )


@router.get("/{budget_id}", summary="Bütçe (kategori × ay)")
async def get_budget(budget_id: uuid.UUID, ctx: FinanceRead) -> AnnualBudgetDetail:
    return await _detail(ctx, await _budget(ctx, budget_id))


class BudgetUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    income_target: Money | None = None
    lines: list[BudgetCategoryLine] | None = Field(
        default=None, description="verilirse tüm satırların yerine geçer"
    )


@router.put("/{budget_id}", summary="Taslak bütçeyi düzenle")
async def update_budget(
    budget_id: uuid.UUID, body: BudgetUpdateIn, ctx: BudgetManage
) -> Written[AnnualBudgetDetail]:
    budget = await _budget(ctx, budget_id)
    lines = (
        {line.expense_category_id: list(line.months) for line in body.lines}
        if body.lines is not None
        else None
    )
    if body.lines is not None and len(lines or {}) != len(body.lines):
        raise form_error(FormFieldError({"lines": "Aynı kategori iki kez gönderilmiş."}))
    try:
        await svc.update(
            ctx.session, budget, name=body.name, income_target=body.income_target, lines=lines
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    data = await _detail(ctx, budget)
    await ctx.session.commit()
    return Written(data=data, message="Bütçe kaydedildi.")


@router.post(
    "/{budget_id}/submit",
    summary="Bütçeyi onaya gönder (kural yoksa hemen yürürlüğe girer)",
    responses={202: {"description": "Onaya gönderildi"}},
)
async def submit_budget(
    budget_id: uuid.UUID,
    ctx: BudgetManage,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[AnnualBudgetDetail]:
    budget = await _budget(ctx, budget_id)
    try:
        result = await svc.submit(
            ctx.session, ctx.site, budget, actor=actor(current), today=today, now=now
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    except ApprovalError as exc:
        raise approval_error(exc) from exc
    data = await _detail(ctx, budget)
    await ctx.session.commit()
    if result.request is not None:
        response.status_code = HTTPStatus.ACCEPTED
        message = (
            f"{budget.fiscal_year} bütçesi 'Bütçe' onayına gönderildi (talep "
            f"#{result.request.number})."
        )
    else:
        message = f"{budget.fiscal_year} bütçesi yürürlüğe girdi ({format_money_tr(data.total)})."
    return Written(data=data, message=message)


@router.post("/{budget_id}/revise", status_code=HTTPStatus.CREATED, summary="Revizyon (yeni sürüm)")
async def revise_budget(
    budget_id: uuid.UUID, ctx: BudgetManage, current: CurrentUserDep
) -> Written[AnnualBudgetDetail]:
    budget = await _budget(ctx, budget_id)
    try:
        copy = await svc.revise(ctx.session, budget, actor(current))
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    data = await _detail(ctx, copy)
    await ctx.session.commit()
    return Written(
        data=data,
        message=f"Sürüm {copy.version} taslak olarak açıldı; onaylanınca öncekinin yerine geçer.",
    )


class BudgetActualRowOut(BaseModel):
    expense_category_id: uuid.UUID
    category_name: str
    month_budget: Money
    month_actual: Money
    cumulative_budget: Money
    cumulative_actual: Money
    annual_budget: Money
    remaining: Money = Field(description="yıllık bütçe − kümülatif gerçekleşen")
    usage_percent: Decimal | None = Field(description="kümülatif gerçekleşen / yıllık bütçe (%)")
    deviation_percent: Decimal | None = Field(description="kümülatif sapma (%)")
    level: Literal["ok", "warn", "over"] = Field(description="%90 sarı (warn), %100 kırmızı")


class BudgetActualsOut(BaseModel):
    budget_id: uuid.UUID
    fiscal_year: int
    month: int
    warn_percent: int = Field(description="finans parametresi `budget_warn_percent`")
    rows: list[BudgetActualRowOut]
    totals: BudgetActualRowOut | None


@router.get("/{budget_id}/actuals", summary="Bütçe–gerçekleşme (aylık ve kümülatif)")
async def budget_actuals(
    budget_id: uuid.UUID,
    ctx: FinanceRead,
    today: TodayDep,
    month: Annotated[int | None, Query(ge=1, le=12, description="boşsa bu ay / yıl sonu")] = None,
) -> BudgetActualsOut:
    from site_yonetim.domain.charging.periods import YearMonth
    from site_yonetim.services import finance_params

    budget = await _budget(ctx, budget_id)
    if month is None:
        month = today.month if today.year == budget.fiscal_year else 12
    rows = await svc.report(ctx.session, budget, month)
    warn = int(
        (await finance_params.params_at(ctx.session, YearMonth(budget.fiscal_year, month)))[
            "budget_warn_percent"
        ]
    )

    def row_out(r: svc.ActualRow) -> BudgetActualRowOut:
        return BudgetActualRowOut(
            expense_category_id=r.category_id,
            category_name=r.category_name,
            month_budget=r.month_budget,
            month_actual=r.month_actual,
            cumulative_budget=r.cumulative_budget,
            cumulative_actual=r.cumulative_actual,
            annual_budget=r.annual_budget,
            remaining=r.annual_budget - r.cumulative_actual,
            usage_percent=r.usage_percent,
            deviation_percent=r.deviation_percent,
            level=r.level,  # type: ignore[arg-type]
        )

    total_row = None
    if rows:
        fields = (
            "month_budget",
            "month_actual",
            "cumulative_budget",
            "cumulative_actual",
            "annual_budget",
        )
        sums = {f: sum((getattr(r, f) for r in rows), ZERO) for f in fields}
        usage = svc.usage_percent(sums["cumulative_actual"], sums["annual_budget"])
        deviation = (
            svc.usage_percent(
                sums["cumulative_actual"] - sums["cumulative_budget"], sums["cumulative_budget"]
            )
            if sums["cumulative_budget"] > ZERO
            else None
        )
        total_row = row_out(
            svc.ActualRow(
                budget.id,
                "Toplam",
                sums["month_budget"],
                sums["month_actual"],
                sums["cumulative_budget"],
                sums["cumulative_actual"],
                sums["annual_budget"],
                usage,
                deviation,
                svc.level(usage, warn, sums["cumulative_actual"]),
            )
        )
    return BudgetActualsOut(
        budget_id=budget.id,
        fiscal_year=budget.fiscal_year,
        month=month,
        warn_percent=warn,
        rows=[row_out(r) for r in rows],
        totals=total_row,
    )
