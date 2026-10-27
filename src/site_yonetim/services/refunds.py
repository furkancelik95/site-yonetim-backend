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
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.cash import CashSource
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.money import EPSILON, ZERO, round_money
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import CashAccount, LedgerAccount, LedgerEntry, Refund, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import cash, ledger, period_lock
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ


async def _ledger_balance(session: AsyncSession, account_id: uuid.UUID) -> Decimal:
    value = await session.scalar(
        select(func.sum(LedgerEntry.debit - LedgerEntry.credit)).where(
            LedgerEntry.account_id == account_id
        )
    )
    return round_money(value if value is not None else ZERO)


async def _validate(
    session: AsyncSession,
    account: LedgerAccount,
    *,
    amount: Decimal,
    day: date,
    cash_account_id: uuid.UUID,
    reason: str | None,
    today: date,
) -> tuple[str, CashAccount]:
    """Alan, dönem ve bakiye denetimi (hesabı kilitler); hiçbir şey yazmaz."""
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
    await period_lock.ensure_writable(session, day)
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
    return reason, cash_account


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
    reason, cash_account = await _validate(
        session, account, amount=amount, day=day, cash_account_id=cash_account_id,
        reason=reason, today=today,
    )  # fmt: skip
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


async def request_if_ruled(
    session: AsyncSession,
    site: Site,
    account: LedgerAccount,
    *,
    amount: Decimal,
    day: date,
    cash_account_id: uuid.UUID,
    reason: str,
    requester: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    """İade onay kuralı (K21) varsa talep açar (iade yazılmaz); yoksa None. Alan ve bakiye
    denetimi onay öncesi yapılır."""
    if (
        await approvals_svc.match_rule(
            session, ActionType.REFUND, amount=amount, source=None, today=today
        )
        is None
    ):
        return None
    await _validate(
        session, account, amount=amount, day=day, cash_account_id=cash_account_id,
        reason=reason, today=today,
    )  # fmt: skip
    return await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.REFUND,
            title=f"İade — {account.reference_code}, {format_money_tr(amount)}",
            summary={
                "reference_code": account.reference_code,
                "amount": f"{amount:.2f}",
                "date": day.isoformat(),
                "reason": " ".join(reason.split()),
            },
            payload={
                "ledger_account_id": str(account.id),
                "amount": str(amount),
                "date": day.isoformat(),
                "cash_account_id": str(cash_account_id),
                "reason": reason,
            },
            amount=amount,
            subject_type="ledger_accounts",
            subject_id=account.id,
        ),
        requester=requester,
        today=today,
        now=now,
    )


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    account = await session.get(LedgerAccount, uuid.UUID(payload["ledger_account_id"]))
    if account is None:
        raise ExecutionError("Cari hesap bulunamadı.")
    decided = (request.decided_at or datetime.now().astimezone()).astimezone(BUSINESS_TZ)
    try:
        await record(
            session,
            account,
            amount=Decimal(payload["amount"]),
            day=date.fromisoformat(payload["date"]),
            cash_account_id=uuid.UUID(payload["cash_account_id"]),
            reason=payload["reason"],
            today=decided.date(),
            recorded_by=request.requested_by_name,
        )
    except FinanceRuleError as exc:
        raise ExecutionError(f"İade artık yapılamıyor: {exc.message}") from exc


approvals_svc.register_executor(ActionType.REFUND, _execute)
