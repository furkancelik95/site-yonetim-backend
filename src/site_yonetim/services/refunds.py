"""İade — cari hesabın alacaklı bakiyesinin sakine geri ödenmesi (frontend servis isteği 03).
Açık site kapsamında; transaction'ı çağıran yönetir.

Tek transaction'da: `refunds` satırı, cari defterde **borç** hareketi (`source = refund`,
bakiyeyi sıfıra doğru çeker) ve kasa/bankada **çıkış** hareketi (`CashSource.REFUND`).

- Yalnız alacaklı bakiye iade edilir: `amount ≤ −bakiye`. Bakiye defterden, hesap kilitliyken
  hesaplanır — eşzamanlı iki iade aynı alacağı iki kez geri ödeyemez.
- İade borcu "açık borç" değildir: sonraki tahsilat ona mahsup edilmez (`payments._OPEN_DEBTS`).
- Kapalı hesaba da iade yapılabilir: taşınan sakinin alacağını kapatmanın yolu budur.
- Avansın mahsubu (açık karar K4) ne olursa olsun iade ayrı işlemdir. Değişmez; geri alınamaz
  (cari defter için ters kayıt ucu henüz yok).
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.cash import CashSource
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource, PeriodStatus
from site_yonetim.domain.money import EPSILON, ZERO, round_money
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import LedgerAccount, LedgerEntry, Period, Refund
from site_yonetim.services import cash, ledger


async def _ledger_balance(session: AsyncSession, account_id: uuid.UUID) -> Decimal:
    value = await session.scalar(
        select(func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return round_money(value if value is not None else ZERO)


async def record(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    amount: Decimal,
    day: date,
    cash_account_id: uuid.UUID,
    reason: str | None,
    today: date,
    recorded_by: str,
) -> Refund:
    reason = " ".join((reason or "").split())
    if not reason:
        raise FinanceRuleError("reason_required", "Gerekçe zorunlu.", field="reason")
    if amount <= ZERO or amount != round_money(amount):
        raise FinanceRuleError(
            "invalid_amount", "Tutar sıfırdan büyük olmalı (en fazla 2 ondalık).", field="amount"
        )
    if day > today:
        raise FinanceRuleError(
            "refund_in_future", "İade tarihi ileri bir tarih olamaz.", field="date"
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
            "period_closed", f"{day:%m/%Y} dönemi kapalı; bu tarihe iade yazılamaz.", conflict=True
        )
    cash_account = await cash.active_account(session, cash_account_id)

    await ledger.lock_accounts(session, [account.id])
    credit = -await _ledger_balance(session, account.id)
    if credit <= EPSILON:
        raise FinanceRuleError(
            "no_credit", "Bu hesabın alacak bakiyesi yok; iade yapılamaz.", conflict=True
        )
    if amount > credit:
        raise FinanceRuleError(
            "exceeds_credit",
            f"En fazla {format_money_tr(credit)} iade edilebilir.",
            field="amount",
        )
    refund = Refund(
        id=uuid.uuid7(),
        ledger_account_id=account.id,
        cash_account_id=cash_account.id,
        amount=amount,
        date=day,
        reason=reason,
        created_by_name=recorded_by,
    )
    session.add(refund)
    await session.flush()
    session.add(
        LedgerEntry(
            account_id=account.id,
            date=day,
            debit=amount,
            source=LedgerSource.REFUND.value,
            source_id=refund.id,
            description=f"İade — {reason}",
        )
    )
    await ledger.refresh_balances(session, [account.id])
    await cash.add_movement(
        session, cash_account, day=day, outflow=amount,
        description=f"İade — {account.reference_code}", source=CashSource.REFUND,
        source_id=refund.id, created_by=recorded_by,
    )  # fmt: skip
    return refund
