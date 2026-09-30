"""Cari hesap okumaları: hesap listesi, ekstre, borçlular, tahsilat ayrıntısı (makbuz verisi).

Hepsi açık site kapsamında; toplama ve yürüyen bakiye veritabanında, sayfa başına sabit sorgu
(docs/08 §3). Bakiyeler özet tablodan okunur (docs/08 §2).
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import ChargeRunStatus
from site_yonetim.domain.money import EPSILON, ZERO
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import (
    AccountBalance,
    Block,
    Charge,
    ChargeLine,
    ChargeRun,
    LedgerAccount,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    Period,
    Person,
    Unit,
)


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True)
class AccountRow:
    account: LedgerAccount
    unit_name: str
    person_name: str
    balance: Decimal
    oldest_open_due_date: date | None


def _unit_name(block: str | None, number: str) -> str:
    return f"{block}-{number}" if block else number


def accounts_query() -> Select[LedgerAccount, str, str, str, str, Decimal | None, date | None]:
    return (
        select(
            LedgerAccount,
            Unit.number,
            Block.name,
            Person.first_name,
            Person.last_name,
            AccountBalance.balance,
            AccountBalance.oldest_open_due_date,
        )
        .join(Unit, and_(Unit.id == LedgerAccount.unit_id, Unit.site_id == LedgerAccount.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .join(
            Person,
            and_(Person.id == LedgerAccount.person_id, Person.site_id == LedgerAccount.site_id),
        )
        .outerjoin(
            AccountBalance,
            and_(
                AccountBalance.account_id == LedgerAccount.id,
                AccountBalance.site_id == LedgerAccount.site_id,
            ),
        )
    )


def search(query: Select[*tuple[Any, ...]], q: str | None) -> Select[*tuple[Any, ...]]:
    """Referans kodu (`A12-M`), bölüm (`A-12`, `A12`) ya da kişi adı ile arama."""
    if not q:
        return query
    term = " ".join(q.split())
    compact = term.replace("-", "").replace(" ", "")
    return query.where(
        or_(
            LedgerAccount.reference_code.ilike(_like(term)),
            func.concat(Block.name, Unit.number).ilike(_like(compact)),
            Person.search_name.like(_like(tr_lower(term))),
        )
    )


def to_row(values: tuple[Any, ...]) -> AccountRow:
    account, number, block, first, last, balance, oldest = values
    return AccountRow(
        account=account,
        unit_name=_unit_name(block, number),
        person_name=f"{first} {last}",
        balance=balance if balance is not None else Decimal("0.00"),
        oldest_open_due_date=oldest,
    )


async def account_row(session: AsyncSession, account_id: uuid.UUID) -> AccountRow | None:
    row = (await session.execute(accounts_query().where(LedgerAccount.id == account_id))).first()
    return to_row(tuple(row)) if row else None


# --- Ekstre -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementLine:
    entry: LedgerEntry
    running_balance: Decimal


async def statement(
    session: AsyncSession, account_id: uuid.UUID, *, offset: int, limit: int
) -> tuple[list[StatementLine], int]:
    """Hareketler en yeni üstte; her satırda o satırdan sonraki bakiye (yürüyen bakiye).

    İlk satırın yürüyen bakiyesi = güncel bakiye. Hesap SQL pencere fonksiyonuyla yapılır.
    """
    running = (
        func.sum(LedgerEntry.debit - LedgerEntry.credit)
        .over(order_by=(LedgerEntry.date, LedgerEntry.created_at, LedgerEntry.id))
        .label("running")
    )
    inner = (
        select(LedgerEntry.id.label("entry_id"), running)
        .where(LedgerEntry.account_id == account_id)
        .subquery()
    )
    total = (
        await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.account_id == account_id)
        )
        or 0
    )
    rows = await session.execute(
        select(LedgerEntry, inner.c.running)
        .join(inner, inner.c.entry_id == LedgerEntry.id)
        .order_by(LedgerEntry.date.desc(), LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [StatementLine(entry, value) for entry, value in rows], total


@dataclass(frozen=True, slots=True)
class LastCharge:
    period: YearMonth
    charge: Charge
    lines: list[ChargeLine]


async def last_charge(session: AsyncSession, account_id: uuid.UUID) -> LastCharge | None:
    """Hesabın son geçerli tahakkuku ve kalem dökümü — "bu tutar nasıl hesaplandı"."""
    row = (
        await session.execute(
            select(Charge, Period.year, Period.month)
            .join(
                ChargeRun,
                and_(ChargeRun.id == Charge.charge_run_id, ChargeRun.site_id == Charge.site_id),
            )
            .join(
                Period, and_(Period.id == ChargeRun.period_id, Period.site_id == ChargeRun.site_id)
            )
            .where(
                Charge.ledger_account_id == account_id,
                ChargeRun.status == ChargeRunStatus.POSTED.value,
                ChargeRun.reversal_of_run_id.is_(None),
            )
            .order_by(Period.year.desc(), Period.month.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    charge, year, month = row
    lines = list(
        await session.scalars(
            select(ChargeLine).where(ChargeLine.charge_id == charge.id).order_by(ChargeLine.id)
        )
    )
    return LastCharge(YearMonth(year, month), charge, lines)


# --- Borçlular (docs/04 §13) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DebtorSummary:
    total_balance: Decimal
    debtor_count: int
    over_30_days: Decimal
    over_30_count: int
    over_60_days: Decimal
    over_60_count: int
    average_balance: Decimal


def debtors_query(q: str | None) -> Select[*tuple[Any, ...]]:
    query = accounts_query().where(AccountBalance.balance > EPSILON)
    return search(query, q).order_by(AccountBalance.balance.desc(), LedgerAccount.reference_code)


async def debtor_summary(session: AsyncSession, today: date) -> DebtorSummary:
    over_30 = AccountBalance.oldest_open_due_date < today - timedelta(days=30)
    over_60 = AccountBalance.oldest_open_due_date < today - timedelta(days=60)
    zero = literal(ZERO)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(AccountBalance.balance), zero),
                func.count(),
                func.coalesce(func.sum(case((over_30, AccountBalance.balance), else_=zero)), zero),
                func.count(case((over_30, 1))),
                func.coalesce(func.sum(case((over_60, AccountBalance.balance), else_=zero)), zero),
                func.count(case((over_60, 1))),
            ).where(AccountBalance.balance > EPSILON)
        )
    ).one()
    total, count, sum30, count30, sum60, count60 = row
    average = (total / count).quantize(Decimal("0.01")) if count else Decimal("0.00")
    return DebtorSummary(total, count, sum30, count30, sum60, count60, average)


def overdue_days(oldest: date | None, today: date) -> int:
    return max(0, (today - oldest).days) if oldest else 0


# --- Tahsilat ayrıntısı -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllocationLine:
    ledger_entry_id: uuid.UUID
    description: str
    due_date: date | None
    amount: Decimal


async def payment_allocations(session: AsyncSession, payment_id: uuid.UUID) -> list[AllocationLine]:
    rows = await session.execute(
        select(PaymentAllocation.amount, LedgerEntry)
        .join(
            LedgerEntry,
            and_(
                LedgerEntry.id == PaymentAllocation.ledger_entry_id,
                LedgerEntry.site_id == PaymentAllocation.site_id,
            ),
        )
        .where(PaymentAllocation.payment_id == payment_id)
        .order_by(func.coalesce(LedgerEntry.due_date, LedgerEntry.date), LedgerEntry.id)
    )
    return [AllocationLine(e.id, e.description, e.due_date or e.date, amount) for amount, e in rows]


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    payment: Payment | None = await session.scalar(select(Payment).where(Payment.id == payment_id))
    return payment


def payments_query(
    *, account_id: uuid.UUID | None, date_from: date | None, date_to: date | None
) -> Select[Payment]:
    query = select(Payment)
    if account_id is not None:
        query = query.where(Payment.ledger_account_id == account_id)
    if date_from is not None:
        query = query.where(Payment.date >= date_from)
    if date_to is not None:
        query = query.where(Payment.date <= date_to)
    return query.order_by(Payment.date.desc(), Payment.created_at.desc(), Payment.id.desc())
