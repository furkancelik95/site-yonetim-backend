"""Tahsilat — docs/04 §5. Açık site kapsamında; transaction'ı çağıran yönetir.

Tek transaction'da: tahsilat satırı, açık borçlara FIFO mahsup, cari hesaba tahsilatın tamamı
kadar alacak hareketi, özet bakiye ve — kasa/banka hesabı seçildiyse — kasaya giriş (adım 4).
Birlikte: borç kapanır **ve** para bir yere girer.

Hesap önce kilitlenir (`ledger.lock_accounts`): aynı hesaba eşzamanlı iki tahsilat aynı açık
borcu iki kez kapatamaz; ikincisi birincinin mahsuplarını görerek dağıtır.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.cash import CashSource
from site_yonetim.domain.charging.payments import (
    AllocationResult,
    OpenDebt,
    PaymentMethod,
    PaymentStatus,
    allocate,
    check_amount,
    payment_description,
)
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource, PeriodStatus
from site_yonetim.models import (
    AccountBalance,
    LedgerAccount,
    LedgerEntry,
    Payment,
    PaymentAllocation,
    Period,
)
from site_yonetim.services import cash, ledger, summary

# Açık borç = borç hareketi − yapılmış mahsuplar; ters kaydı alınmış borç açık değildir.
# İade (`refund`) borç tarafında yazılır ama alacağı geri ödemedir, kapatılacak borç değildir.
_OPEN_DEBTS = text(
    """
    SELECT e.id, COALESCE(e.due_date, e.date) AS due, e.source = 'late_fee' AS is_late_fee,
           e.debit - COALESCE(SUM(pa.amount), 0) AS open_amount
    FROM ledger_entries e
    LEFT JOIN payment_allocations pa ON pa.site_id = e.site_id AND pa.ledger_entry_id = e.id
    WHERE e.site_id = :site_id AND e.account_id = :account_id AND e.debit > 0
      AND e.source <> 'refund'
      AND NOT EXISTS (
          SELECT 1 FROM ledger_entries r
          WHERE r.site_id = e.site_id AND r.reversal_of_entry_id = e.id
      )
    GROUP BY e.id
    HAVING e.debit - COALESCE(SUM(pa.amount), 0) > 0.005
    ORDER BY due, e.created_at, e.id
    """
)


async def open_debts(session: AsyncSession, account_id: uuid.UUID) -> list[OpenDebt]:
    scope = current_scope()
    rows = await session.execute(
        _OPEN_DEBTS, {"site_id": scope.site_id if scope else None, "account_id": account_id}
    )
    return [
        OpenDebt(row.id, row.due, row.is_late_fee, sequence, row.open_amount)
        for sequence, row in enumerate(rows)
    ]


async def balance_of(session: AsyncSession, account_id: uuid.UUID) -> Decimal:
    value = await session.scalar(
        select(AccountBalance.balance).where(AccountBalance.account_id == account_id)
    )
    return value if value is not None else Decimal("0.00")


@dataclass(frozen=True, slots=True)
class RecordedPayment:
    payment: Payment
    result: AllocationResult
    balance: Decimal  # tahsilattan sonraki bakiye (negatif = avans)


def _clean(value: str | None) -> str | None:
    """Boşlukları sadeleştirir; uzunluk sınırı uç şemasında (reference ≤ 100, note ≤ 500)."""
    return " ".join(value.split()) or None if value is not None else None


async def record_payment(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    amount: Decimal,
    day: date,
    method: PaymentMethod,
    reference: str | None,
    note: str | None,
    today: date,
    recorded_by: str,
    cash_account_id: uuid.UUID | None = None,
) -> RecordedPayment:
    check_amount(amount)
    if day > today:
        raise FinanceRuleError(
            "payment_in_future", "Tahsilat tarihi ileri bir tarih olamaz.", field="date"
        )
    if account.is_closed:
        raise FinanceRuleError(
            "account_closed", "Bu cari hesap kapatılmış; tahsilat yazılamaz.", conflict=True
        )
    closed = await session.scalar(
        select(Period.id).where(
            Period.year == day.year,
            Period.month == day.month,
            Period.status == PeriodStatus.CLOSED.value,
        )
    )
    if closed is not None:
        raise FinanceRuleError(
            "period_closed",
            f"{day:%m/%Y} dönemi kapalı; bu tarihe tahsilat yazılamaz.",
            conflict=True,
        )
    cash_account = await cash.active_account(session, cash_account_id) if cash_account_id else None
    reference = _clean(reference) or account.reference_code
    note = _clean(note)

    await ledger.lock_accounts(session, [account.id])
    result = allocate(amount, await open_debts(session, account.id))
    payment = Payment(
        id=uuid.uuid7(),
        ledger_account_id=account.id,
        amount=amount,
        date=day,
        method=method.value,
        cash_account_id=cash_account.id if cash_account else None,
        reference=reference,
        note=note,
        status=PaymentStatus.CONFIRMED.value,
        created_by_name=recorded_by,
    )
    session.add(payment)
    await session.flush()
    session.add_all(
        PaymentAllocation(payment_id=payment.id, ledger_entry_id=a.entry_id, amount=a.amount)
        for a in result.allocations
    )
    session.add(
        LedgerEntry(
            account_id=account.id,
            date=day,
            credit=amount,
            source=LedgerSource.PAYMENT.value,
            source_id=payment.id,
            description=payment_description(day, method, result),
        )
    )
    await ledger.refresh_balances(session, [account.id])
    await summary.add_collected(session, day, amount)
    if cash_account is not None:
        await cash.add_movement(
            session, cash_account, day=day, inflow=amount,
            description=f"Tahsilat — {account.reference_code}", source=CashSource.PAYMENT,
            source_id=payment.id, reference=reference, created_by=recorded_by,
        )  # fmt: skip
    return RecordedPayment(payment, result, await balance_of(session, account.id))
