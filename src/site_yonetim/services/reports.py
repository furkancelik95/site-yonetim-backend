"""Gelir–gider raporu ve aidat tahsilat özeti — docs/04 §12. Açık site kapsamında, salt okunur.

Toplamalar veritabanında (docs/08 §3.2); sorgu sayısı veri miktarından bağımsız.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, and_, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.cash import CashSource
from site_yonetim.domain.charging.payments import PaymentStatus
from site_yonetim.domain.finance import BudgetStatus, ChargeRunStatus, LedgerSource
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.reports import (
    BudgetLine,
    CategoryAmount,
    CategoryShare,
    CollectionRow,
    MonthRow,
    budget_comparison,
    category_shares,
    months,
)
from site_yonetim.models import (
    BudgetItem,
    BudgetPlan,
    CashMovement,
    Charge,
    ChargeRun,
    Expense,
    ExpenseCategory,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    Period,
)
from site_yonetim.services import cash
from site_yonetim.services.expenses import realized


def _by_month(rows: Iterable[Any]) -> dict[int, Decimal]:
    return {int(month): Decimal(total) for month, total in rows}


async def _income(session: AsyncSession, year: int) -> dict[int, Decimal]:
    """Onaylı tahsilat + elle kasa girişi (ters kaydı geliri düşürür)."""
    totals: dict[int, Decimal] = defaultdict(lambda: ZERO)
    payments = await session.execute(
        select(extract("month", Payment.date), func.sum(Payment.amount))
        .where(
            extract("year", Payment.date) == year,
            Payment.status == PaymentStatus.CONFIRMED.value,
        )
        .group_by(extract("month", Payment.date))
    )
    for month, amount in _by_month(payments).items():
        totals[month] += amount
    inflows = await session.execute(
        select(extract("month", CashMovement.date), func.sum(CashMovement.inflow))
        .where(
            extract("year", CashMovement.date) == year,
            CashMovement.source == CashSource.MANUAL.value,
            CashMovement.reversal_of_id.is_(None),
            CashMovement.inflow > 0,
        )
        .group_by(extract("month", CashMovement.date))
    )
    for month, amount in _by_month(inflows).items():
        totals[month] += amount
    reversed_income = await session.execute(
        select(extract("month", CashMovement.date), func.sum(CashMovement.outflow))
        .where(
            extract("year", CashMovement.date) == year,
            CashMovement.source == CashSource.MANUAL.value,
            CashMovement.reversal_of_id.is_not(None),
            CashMovement.outflow > 0,
        )
        .group_by(extract("month", CashMovement.date))
    )
    for month, amount in _by_month(reversed_income).items():
        totals[month] -= amount
    return dict(totals)


async def _expenses_by_month(session: AsyncSession, year: int) -> dict[int, Decimal]:
    rows = await session.execute(
        select(extract("month", Expense.date), func.sum(Expense.amount))
        .where(extract("year", Expense.date) == year, realized())
        .group_by(extract("month", Expense.date))
    )
    return _by_month(rows)


async def _categories(session: AsyncSession, year: int) -> list[CategoryAmount]:
    rows = await session.execute(
        select(ExpenseCategory.id, ExpenseCategory.name, func.sum(Expense.amount), func.count())
        .join(
            ExpenseCategory,
            and_(
                ExpenseCategory.id == Expense.expense_category_id,
                ExpenseCategory.site_id == Expense.site_id,
            ),
        )
        .where(extract("year", Expense.date) == year, realized())
        .group_by(ExpenseCategory.id, ExpenseCategory.name)
    )
    return [CategoryAmount(cid, name, Decimal(total), count) for cid, name, total, count in rows]


async def _year_plan(session: AsyncSession, year: int) -> BudgetPlan | None:
    """O yılın projesi: kesinleşmiş, yoksa yerini yenisine bırakmış olan (taslak sayılmaz)."""
    plan: BudgetPlan | None = await session.scalar(
        select(BudgetPlan)
        .where(
            BudgetPlan.fiscal_year == year,
            BudgetPlan.status.in_([BudgetStatus.FINALIZED.value, BudgetStatus.SUPERSEDED.value]),
        )
        .order_by(
            (BudgetPlan.status == BudgetStatus.FINALIZED.value).desc(),
            BudgetPlan.finalized_on.desc(),
        )
        .limit(1)
    )
    return plan


@dataclass(frozen=True, slots=True)
class IncomeExpenseReport:
    year: int
    months: list[MonthRow]
    categories: list[CategoryShare]
    budget_plan: BudgetPlan | None
    budget: list[BudgetLine]
    cash_balance: Decimal
    years: list[int]

    @property
    def total_income(self) -> Decimal:
        return sum((m.income for m in self.months), ZERO)

    @property
    def total_expense(self) -> Decimal:
        return sum((m.expense for m in self.months), ZERO)


async def available_years(session: AsyncSession, today: date) -> list[int]:
    """Gider ya da tahsilat olan yıllar + bu yıl, yeniden eskiye."""
    years = {today.year}
    for column in (Expense.date, Payment.date):
        years |= {int(y) for y in await session.scalars(select(extract("year", column)).distinct())}
    return sorted(years, reverse=True)


async def income_expense(session: AsyncSession, year: int, today: date) -> IncomeExpenseReport:
    categories = await _categories(session, year)
    plan = await _year_plan(session, year)
    budget: list[BudgetLine] = []
    if plan is not None:
        budgeted = {
            cid: Decimal(total)
            for cid, total in await session.execute(
                select(BudgetItem.expense_category_id, func.sum(BudgetItem.annual_amount))
                .where(BudgetItem.budget_plan_id == plan.id)
                .group_by(BudgetItem.expense_category_id)
            )
        }
        names = dict(
            (await session.execute(select(ExpenseCategory.id, ExpenseCategory.name))).all()
        )
        budget = budget_comparison(budgeted, {c.category_id: c.amount for c in categories}, names)
    return IncomeExpenseReport(
        year=year,
        months=months(await _income(session, year), await _expenses_by_month(session, year)),
        categories=category_shares(categories),
        budget_plan=plan,
        budget=budget,
        cash_balance=await cash.total_balance(session),
        years=await available_years(session, today),
    )


# --- Aidat tahsilat özeti -----------------------------------------------------------


def _valid_runs() -> list[ColumnElement[bool]]:
    return [
        ChargeRun.status == ChargeRunStatus.POSTED.value,
        ChargeRun.reversal_of_run_id.is_(None),
    ]


async def collections(session: AsyncSession, year: int) -> list[CollectionRow]:
    """Her dönem: geçerli koşunun kestiği borç ve o borçlara yapılan mahsuplar (oran %)."""
    run_join = and_(ChargeRun.id == Charge.charge_run_id, ChargeRun.site_id == Charge.site_id)
    period_join = and_(Period.id == ChargeRun.period_id, Period.site_id == ChargeRun.site_id)
    charged = await session.execute(
        select(Period.month, func.sum(Charge.amount))
        .join(ChargeRun, run_join)
        .join(Period, period_join)
        .where(Period.year == year, *_valid_runs())
        .group_by(Period.month)
    )
    collected = await session.execute(
        select(Period.month, func.sum(PaymentAllocation.amount))
        .join(
            LedgerEntry,
            and_(
                LedgerEntry.id == PaymentAllocation.ledger_entry_id,
                LedgerEntry.site_id == PaymentAllocation.site_id,
            ),
        )
        .join(
            Charge,
            and_(
                Charge.id == LedgerEntry.source_id,
                Charge.site_id == LedgerEntry.site_id,
                LedgerEntry.source == LedgerSource.CHARGE.value,
            ),
        )
        .join(ChargeRun, run_join)
        .join(Period, period_join)
        .where(Period.year == year, *_valid_runs())
        .group_by(Period.month)
    )
    paid = {int(month): Decimal(total) for month, total in collected}
    return [
        CollectionRow(year, int(month), Decimal(total), paid.get(int(month), ZERO))
        for month, total in sorted(charged, key=lambda row: row[0])
    ]
