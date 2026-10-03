"""Devir bakiye (cari hesap açılış bakiyesi) — frontend servis isteği 02. Açık site kapsamında;
transaction'ı çağıran yönetir.

Site sisteme geçerken önceki yönetimden kalan borç ya da alacak, hesap başına **bir kez**, defter
hareketi olarak yazılır (`source = opening`; kasadaki açılış hareketinin cari karşılığı).

- `debit` (sakin borçlu): vadesi devir tarihi olan borç — FIFO mahsuba ve gecikme gününe girer.
- `credit` (sakin alacaklı): avans. Avansın sonraki borca mahsubu açık karar (docs/12 K4):
  bakiye doğru, mahsup kaydı üretilmez — tahsilattaki avansla aynı davranış.
- Hesap başına bir kez: servis kontrolü + kısmi benzersiz indeks (`uq_ledger_entries_opening`).
- Tahakkuk/tahsilat değildir: pano özeti (`site_finance_summary`) değişmez, özet bakiye değişir.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.finance import FinanceRuleError, LedgerSource, PeriodStatus
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.models import LedgerAccount, LedgerEntry, Period
from site_yonetim.services import ledger

DESCRIPTION = "Devir bakiye"


class OpeningDirection(StrEnum):
    DEBIT = "debit"  # sakin borçlu
    CREDIT = "credit"  # sakin alacaklı (avans)


def description_of(note: str | None) -> str:
    """description_of("2025 yönetiminden devir") → "Devir bakiye — 2025 yönetiminden devir" """
    return f"{DESCRIPTION} — {note}" if note else DESCRIPTION


async def record(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    amount: Decimal,
    direction: OpeningDirection,
    day: date,
    note: str | None,
    today: date,
) -> LedgerEntry:
    if amount <= ZERO or amount != round_money(amount):
        raise FinanceRuleError(
            "invalid_amount", "Tutar sıfırdan büyük olmalı (en fazla 2 ondalık).", field="amount"
        )
    if day > today:
        raise FinanceRuleError(
            "opening_in_future", "Devir tarihi bugünden sonra olamaz.", field="date"
        )
    if account.is_closed:
        raise FinanceRuleError(
            "account_closed", "Bu cari hesap kapatılmış; devir bakiye yazılamaz.", conflict=True
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
            f"{day:%m/%Y} dönemi kapalı; bu tarihe devir bakiye yazılamaz.",
            conflict=True,
        )
    await ledger.lock_accounts(session, [account.id])
    exists = await session.scalar(
        select(LedgerEntry.id).where(
            LedgerEntry.account_id == account.id,
            LedgerEntry.source == LedgerSource.OPENING.value,
        )
    )
    if exists is not None:
        raise FinanceRuleError(
            "already_exists",
            "Bu hesaba devir bakiye daha önce girildi. Düzeltmek için ters kayıt kullanın.",
            conflict=True,
        )
    debit = direction is OpeningDirection.DEBIT
    entry = LedgerEntry(
        id=uuid.uuid7(),
        account_id=account.id,
        date=day,
        due_date=day if debit else None,
        debit=amount if debit else ZERO,
        credit=ZERO if debit else amount,
        source=LedgerSource.OPENING.value,
        description=description_of(note),
    )
    session.add(entry)
    await ledger.refresh_balances(session, [account.id])
    return entry
