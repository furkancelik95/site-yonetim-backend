"""Raporlar — docs/06 §2.9, docs/04 §12. İzin `finance.reports.read`.

- Gelir–gider (yıllık): gelir = fiilen tahsil edilen (onaylı tahsilat + elle kasa girişi),
  gider = gerçekleşen giderler; ay ay, kategori dökümü, işletme projesi karşılaştırması.
- Aidat tahsilat özeti: dönem başına kesilen borç, ona yapılan mahsuplar, tahsilat oranı.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Money
from site_yonetim.domain.access import Permission
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.reports import percent
from site_yonetim.domain.text import format_period_tr
from site_yonetim.services import exports
from site_yonetim.services import reports as svc

router = APIRouter(prefix="/sites/{slug}/reports", tags=["rapor"])
ReportsRead = Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_REPORTS_READ))]
Year = Annotated[int | None, Query(ge=2000, le=2100, description="boşsa bu yıl")]


class MonthOut(BaseModel):
    month: int
    period: str = Field(description="`09/2026`")
    income: Money
    expense: Money
    difference: Money


class CategoryOut(BaseModel):
    category_id: uuid.UUID
    name: str
    amount: Money
    count: int
    share: str = Field(description="pay %, metin (`25.00`)")


class BudgetLineOut(BaseModel):
    category_id: uuid.UUID
    name: str
    budgeted: Money
    actual: Money
    difference: Money = Field(description="bütçelenen − gerçekleşen")
    usage: str = Field(description="kullanım %, metin")
    is_over: bool


class PlanRef(BaseModel):
    id: uuid.UUID
    name: str
    status: str


class IncomeExpenseOut(BaseModel):
    year: int
    total_income: Money
    total_expense: Money
    difference: Money
    months: list[MonthOut] = Field(description="12 ay; veri olmayan aylar sıfır")
    categories: list[CategoryOut]
    budget_plan: PlanRef | None = Field(description="o yılın kesinleşmiş projesi; yoksa null")
    budget: list[BudgetLineOut]
    cash_balance: Money = Field(description="bugünkü toplam kasa/banka bakiyesi (aktif hesaplar)")
    years: list[int] = Field(description="seçilebilir yıllar")


async def _report(ctx: SiteContext, year: int | None, today: dt.date) -> svc.IncomeExpenseReport:
    return await svc.income_expense(ctx.session, year or today.year, today)


@router.get("/income-expense", summary="Yıllık gelir–gider raporu")
async def income_expense(ctx: ReportsRead, today: TodayDep, year: Year = None) -> IncomeExpenseOut:
    report = await _report(ctx, year, today)
    plan = report.budget_plan
    return IncomeExpenseOut(
        year=report.year,
        total_income=report.total_income,
        total_expense=report.total_expense,
        difference=report.total_income - report.total_expense,
        months=[
            MonthOut(
                month=m.month,
                period=format_period_tr(report.year, m.month),
                income=m.income,
                expense=m.expense,
                difference=m.difference,
            )
            for m in report.months
        ],
        categories=[
            CategoryOut(
                category_id=c.category_id,
                name=c.name,
                amount=c.amount,
                count=c.count,
                share=f"{c.share:.2f}",
            )
            for c in report.categories
        ],
        budget_plan=PlanRef(id=plan.id, name=plan.name, status=plan.status) if plan else None,
        budget=[
            BudgetLineOut(
                category_id=b.category_id,
                name=b.name,
                budgeted=b.budgeted,
                actual=b.actual,
                difference=b.difference,
                usage=f"{b.usage:.2f}",
                is_over=b.is_over,
            )
            for b in report.budget
        ],
        cash_balance=report.cash_balance,
        years=report.years,
    )


@router.get(
    "/income-expense/export.xlsx",
    summary="Gelir–gider raporu Excel (Özet, Kategori, İşletme Projesi)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX_MEDIA_TYPE: {}}}},
)
async def income_expense_export(ctx: ReportsRead, today: TodayDep, year: Year = None) -> Response:
    report = await _report(ctx, year, today)
    data = exports.report_xlsx(
        report.year,
        [(m.month, m.income, m.expense, m.difference) for m in report.months],
        [(c.name, c.amount, c.count, c.share) for c in report.categories],
        [(b.name, b.budgeted, b.actual, b.difference, b.usage, b.is_over) for b in report.budget],
    )
    return Response(
        data,
        media_type=exports.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="gelir-gider-{report.year}.xlsx"'},
    )


class CollectionOut(BaseModel):
    period: str
    year: int
    month: int
    charged: Money = Field(description="o dönem kesilen borç (geçerli koşu)")
    collected: Money = Field(description="o dönemin borçlarına yapılan mahsuplar")
    outstanding: Money
    rate: str = Field(description="tahsilat oranı %, metin")


class CollectionsOut(BaseModel):
    year: int
    periods: list[CollectionOut]
    total_charged: Money
    total_collected: Money
    rate: str


@router.get("/collections", summary="Aidat tahsilat özeti (dönem bazında)")
async def collections(ctx: ReportsRead, today: TodayDep, year: Year = None) -> CollectionsOut:
    chosen = year or today.year
    rows = await svc.collections(ctx.session, chosen)
    charged = sum((r.charged for r in rows), ZERO)
    collected = sum((r.collected for r in rows), ZERO)
    return CollectionsOut(
        year=chosen,
        periods=[
            CollectionOut(
                period=format_period_tr(r.year, r.month),
                year=r.year,
                month=r.month,
                charged=r.charged,
                collected=r.collected,
                outstanding=r.outstanding,
                rate=f"{r.rate:.2f}",
            )
            for r in rows
        ],
        total_charged=charged,
        total_collected=collected,
        rate=f"{percent(collected, charged):.2f}",
    )
