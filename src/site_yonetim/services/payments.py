"""Tahsilat — docs/04 §5. Açık site kapsamında; transaction'ı çağıran yönetir.

Tek transaction'da: tahsilat satırı, açık borçlara FIFO mahsup, cari hesaba tahsilatın tamamı
kadar alacak hareketi, özet bakiye ve — kasa/banka hesabı seçildiyse — kasaya giriş (adım 4).
Birlikte: borç kapanır **ve** para bir yere girer.

Hesap önce kilitlenir (`ledger.lock_accounts`): aynı hesaba eşzamanlı iki tahsilat aynı açık
borcu iki kez kapatamaz; ikincisi birincinin mahsuplarını görerek dağıtır.

- Gecikme tazminatı açıksa (K2) tahsilattan önce o güne kadar biriken tazminat yazılır
  (docs/04 §6 "ödeme anında"); sonra tahsilat mahsup sırasına göre dağıtılır.
- Mahsup sırası finans parametresidir (`allocation_order`, K24): aynı vadede önce gecikme
  (varsayılan, TBK m.100) ya da önce ana borç.
- Avansın sonraki borca mahsubu (D-6, §8.9): borç yazan işlem `apply_advances` çağırır;
  kullanılmamış tahsilat tutarı yeni borca `payment_allocations` (mahsup kaydı) olarak düşer.
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
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.models import (
    AccountBalance,
    LedgerAccount,
    LedgerEntry,
    Payment,
    PaymentAllocation,
)
from site_yonetim.services import cash, ledger, period_lock, summary

# Açık borç = borç hareketi − yapılmış mahsuplar − ödeme planının kapattığı tutar; ters kaydı
# alınmış borç açık değildir. İade (`refund`) borç tarafında yazılır ama alacağı geri ödemedir,
# kapatılacak borç değildir.
_OPEN_DEBTS = text(
    """
    SELECT e.id, COALESCE(e.due_date, e.date) AS due, e.source = 'late_fee' AS is_late_fee,
           e.debit - COALESCE(pa.total, 0) - COALESCE(pc.total, 0) AS open_amount
    FROM ledger_entries e
    LEFT JOIN (
        SELECT ledger_entry_id, SUM(amount) AS total FROM payment_allocations
        WHERE site_id = :site_id AND ledger_entry_id IN (
            SELECT id FROM ledger_entries WHERE site_id = :site_id AND account_id = :account_id
        )
        GROUP BY ledger_entry_id
    ) pa ON pa.ledger_entry_id = e.id
    LEFT JOIN (
        SELECT ledger_entry_id, SUM(amount) AS total FROM payment_plan_closures
        WHERE site_id = :site_id AND ledger_entry_id IN (
            SELECT id FROM ledger_entries WHERE site_id = :site_id AND account_id = :account_id
        )
        GROUP BY ledger_entry_id
    ) pc ON pc.ledger_entry_id = e.id
    WHERE e.site_id = :site_id AND e.account_id = :account_id AND e.debit > 0
      AND e.source <> 'refund'
      AND NOT EXISTS (
          SELECT 1 FROM ledger_entries r
          WHERE r.site_id = e.site_id AND r.reversal_of_entry_id = e.id
      )
      AND e.debit - COALESCE(pa.total, 0) - COALESCE(pc.total, 0) > 0.005
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
    await period_lock.ensure_writable(session, day)
    cash_account = await cash.active_account(session, cash_account_id) if cash_account_id else None
    reference = _clean(reference) or account.reference_code
    note = _clean(note)

    await ledger.lock_accounts(session, [account.id])
    from site_yonetim.services import late_fees

    await late_fees.accrue_at_payment(session, account.id, day)
    result = allocate(
        amount,
        await open_debts(session, account.id),
        principal_first=await principal_first(session, day),
    )
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


async def principal_first(session: AsyncSession, day: date) -> bool:
    """K24 mahsup sırası (finans parametresi `allocation_order`)."""
    from site_yonetim.domain.charging.periods import YearMonth
    from site_yonetim.services import finance_params

    params = await finance_params.params_at(session, YearMonth.of(day))
    return bool(params["allocation_order"] == "principal_first")


# Kullanılmamış tahsilat tutarı: tahsilat − ters kaydı alınmamış borçlara yapılmış mahsuplar.
# Ters kaydı alınan borca yapılmış mahsup serbest kalır (borç düzeltilince ödeme avansa döner).
_FREE_PAYMENTS = text(
    """
    SELECT p.id, p.amount - COALESCE(SUM(pa.amount) FILTER (WHERE r.id IS NULL), 0) AS free
    FROM payments p
    LEFT JOIN payment_allocations pa ON pa.site_id = p.site_id AND pa.payment_id = p.id
    LEFT JOIN ledger_entries r
        ON r.site_id = pa.site_id AND r.reversal_of_entry_id = pa.ledger_entry_id
    WHERE p.site_id = :site_id AND p.ledger_account_id = :account_id AND p.status = 'confirmed'
    GROUP BY p.id, p.amount, p.date, p.created_at
    HAVING p.amount - COALESCE(SUM(pa.amount) FILTER (WHERE r.id IS NULL), 0) > 0.005
    ORDER BY p.date, p.created_at, p.id
    """
)
_LEDGER_BALANCE = text(
    "SELECT COALESCE(SUM(debit - credit), 0) FROM ledger_entries "
    "WHERE site_id = :site_id AND account_id = :account_id"
)


async def apply_advances(session: AsyncSession, account_ids: list[uuid.UUID]) -> int:
    """Avansı yeni borca mahsup eder (D-6; docs/04 §5 K4, §8.9). Hesaplar kilitli olmalı.

    Kullanılabilir avans = min(kullanılmamış tahsilatlar, açık borç − bakiye): iade edilmiş ya da
    başka alacakla kapanmış avans yeniden kullanılmaz. Mahsup sırası tahsilattaki gibidir; her
    parça en eski kullanılmamış tahsilattan düşülür. Yazılan mahsup kaydı sayısını döner.
    """
    scope = current_scope()
    site_id = scope.site_id if scope else None
    written = 0
    for account_id in sorted(set(account_ids)):
        params = {"site_id": site_id, "account_id": account_id}
        free = [(row.id, row.free) for row in await session.execute(_FREE_PAYMENTS, params)]
        if not free:
            continue
        debts = await open_debts(session, account_id)
        balance = await session.scalar(_LEDGER_BALANCE, params) or Decimal(0)
        open_total = sum((d.open_amount for d in debts), Decimal(0))
        available = min(sum((f for _, f in free), Decimal(0)), open_total - balance)
        if available <= Decimal("0.005"):
            continue
        last = max(debts, key=lambda d: d.due_date).due_date if debts else None
        result = allocate(
            available,
            debts,
            principal_first=await principal_first(session, last) if last else False,
        )
        queue = list(free)
        for allocation in result.allocations:
            remaining = allocation.amount
            while remaining > 0 and queue:
                payment_id, left = queue[0]
                part = min(left, remaining)
                session.add(
                    PaymentAllocation(
                        payment_id=payment_id, ledger_entry_id=allocation.entry_id, amount=part
                    )
                )
                written += 1
                remaining -= part
                if left - part <= 0:
                    queue.pop(0)
                else:
                    queue[0] = (payment_id, left - part)
    if written:
        await session.flush()
    return written
