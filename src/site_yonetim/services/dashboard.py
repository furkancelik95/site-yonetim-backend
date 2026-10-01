"""Site panosu — docs/06 §2.4, docs/08 §2. Açık site kapsamında, salt okunur.

Sayılar özet tablolardan okunur (`site_finance_summary`, `account_balances`, `cash_balances`);
sorgu sayısı sabittir, veri büyüdükçe yavaşlamaz.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.operations import RequestPriority, RequestStatus
from site_yonetim.models import Announcement, Expense, Request, SiteFinanceSummary
from site_yonetim.services import accounts, cash
from site_yonetim.services.announcements import visible_query
from site_yonetim.services.expenses import realized

OPEN_STATUSES = (RequestStatus.OPEN, RequestStatus.IN_PROGRESS, RequestStatus.WAITING)
TOP_DEBTORS = 5
RECENT = 5


@dataclass(frozen=True, slots=True)
class FinanceTotals:
    charged: Decimal
    collected: Decimal
    month_charged: Decimal
    month_collected: Decimal


async def finance_totals(session: AsyncSession, today: date) -> FinanceTotals:
    this_month = (SiteFinanceSummary.year == today.year) & (SiteFinanceSummary.month == today.month)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(SiteFinanceSummary.charged), 0),
                func.coalesce(func.sum(SiteFinanceSummary.collected), 0),
                func.coalesce(func.sum(case((this_month, SiteFinanceSummary.charged), else_=0)), 0),
                func.coalesce(
                    func.sum(case((this_month, SiteFinanceSummary.collected), else_=0)), 0
                ),
            )
        )
    ).one()
    cent = Decimal("0.01")
    return FinanceTotals(*(Decimal(v).quantize(cent) for v in row))


async def top_debtors(session: AsyncSession) -> list[accounts.AccountRow]:
    rows = await session.execute(accounts.debtors_query(None).limit(TOP_DEBTORS))
    return [accounts.to_row(tuple(r)) for r in rows]


@dataclass(frozen=True, slots=True)
class RequestCounts:
    open: int
    in_progress: int
    waiting: int
    urgent: int

    @property
    def total(self) -> int:
        return self.open + self.in_progress + self.waiting


async def request_counts(session: AsyncSession) -> RequestCounts:
    is_open = Request.status.in_([s.value for s in OPEN_STATUSES])
    row = (
        await session.execute(
            select(
                func.count(case((Request.status == RequestStatus.OPEN.value, 1))),
                func.count(case((Request.status == RequestStatus.IN_PROGRESS.value, 1))),
                func.count(case((Request.status == RequestStatus.WAITING.value, 1))),
                func.count(case((is_open & (Request.priority == RequestPriority.URGENT.value), 1))),
            )
        )
    ).one()
    return RequestCounts(*row)


async def recent_announcements(
    session: AsyncSession, today: date, *, show_all: bool
) -> list[Announcement]:
    query = visible_query(show_all=show_all, person_id=None, today=today).limit(3)
    return list(await session.scalars(query))


async def recent_expenses(session: AsyncSession) -> list[Expense]:
    query = (
        select(Expense)
        .where(realized())
        .order_by(Expense.date.desc(), Expense.created_at.desc())
        .limit(RECENT)
    )
    return list(await session.scalars(query))


async def cash_total(session: AsyncSession) -> Decimal:
    return await cash.total_balance(session)
