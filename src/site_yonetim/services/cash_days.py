"""Kasa gün sonu sayımı (FIN-11, servis isteği 28). Açık site kapsamında.

- `system_balance` = o günün sonundaki ekstre bakiyesi; `difference = counted − system_balance`.
- Fark varsa gerekçe zorunlu (≥ 10 karakter); "Kasa farkı" onay kuralı varsa gün onay bekler
  (202) ve onaylanınca kapanır. Onay bekleyen ya da kapanan güne kasa hareketi yazılamaz
  (`cash.ensure_day_writable`); ret edilen sayımdan sonra gün yeniden sayılabilir.
- Yalnız kasa (`kind = cash`) hesabında; gelecek gün kapatılamaz, aynı gün iki kez kapanmaz.
- Açık (hareketli, kapanmamış) kasa günü dönem kapanışını engeller (`open_days_in`).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.cash import CashAccountKind
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_date_tr, format_money_tr
from site_yonetim.models import CashAccount, CashDay, CashMovement, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import cash, period_lock
from site_yonetim.services.approvals import ExecutionError, Person

OPEN_WINDOW_DAYS = 60
CLOSED_SHOWN = 90
REASON_MIN = 10
CLOSED, PENDING, REJECTED = "closed", "approval_pending", "rejected"


@dataclass(frozen=True, slots=True)
class DayOverview:
    kind: str
    open_days: list[date]
    closed: list[CashDay]


async def overview(session: AsyncSession, account: CashAccount, today: date) -> DayOverview:
    """Son 60 günde hareketi olup sayılmamış günler (eskiden yeniye) ve son sayımlar."""
    counted = select(CashDay.date).where(
        CashDay.cash_account_id == account.id, CashDay.status != REJECTED
    )
    open_days = list(
        await session.scalars(
            select(CashMovement.date)
            .where(
                CashMovement.cash_account_id == account.id,
                CashMovement.date >= today - timedelta(days=OPEN_WINDOW_DAYS),
                CashMovement.date <= today,
                CashMovement.date.not_in(counted),
            )
            .distinct()
            .order_by(CashMovement.date)
        )
    )
    closed = list(
        await session.scalars(
            select(CashDay)
            .where(CashDay.cash_account_id == account.id)
            .order_by(CashDay.date.desc(), CashDay.created_at.desc())
            .limit(CLOSED_SHOWN)
        )
    )
    return DayOverview(account.kind, open_days if account.kind == "cash" else [], closed)


async def open_days_in(session: AsyncSession, first: date, last: date) -> list[tuple[str, date]]:
    """Aktif kasa hesaplarında aralıktaki hareketli ama sayılmamış günler (hesap adı, gün)."""
    counted = select(CashDay.id).where(
        CashDay.cash_account_id == CashMovement.cash_account_id,
        CashDay.date == CashMovement.date,
        CashDay.status == CLOSED,
    )
    rows = await session.execute(
        select(CashAccount.name, CashMovement.date)
        .join(CashAccount, CashAccount.id == CashMovement.cash_account_id)
        .where(
            CashAccount.kind == CashAccountKind.CASH.value,
            CashAccount.is_active,
            CashMovement.date >= first,
            CashMovement.date <= last,
            ~counted.correlate(CashMovement).exists(),
        )
        .distinct()
        .order_by(CashMovement.date, CashAccount.name)
    )
    return [(name, day) for name, day in rows]


async def balance_at(session: AsyncSession, account_id: uuid.UUID, day: date) -> Decimal:
    value = await session.scalar(
        select(func.coalesce(func.sum(CashMovement.inflow - CashMovement.outflow), 0)).where(
            CashMovement.cash_account_id == account_id, CashMovement.date <= day
        )
    )
    return Decimal(value or 0).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class Closed:
    day: CashDay
    request: ApprovalRequest | None


async def close_day(
    session: AsyncSession,
    site: Site,
    account: CashAccount,
    *,
    day: date,
    counted: Decimal,
    reason: str | None,
    actor: Person,
    today: date,
    now: datetime,
) -> Closed:
    if account.kind != CashAccountKind.CASH.value:
        raise FinanceRuleError(
            "not_cash",
            "Gün sonu sayımı yalnız kasa hesaplarında yapılır; banka hesabı ekstreyle mutabık "
            "tutulur.",
            conflict=True,
        )
    if day > today:
        raise FinanceRuleError("validation", "Gelecek gün kapatılamaz.", field="date")
    if counted < ZERO:
        raise FinanceRuleError("validation", "Sayılan tutar eksi olamaz.", field="counted")
    await period_lock.ensure_writable(session, day)
    await cash.lock_accounts(session, [account.id])
    existing = await session.scalar(
        select(CashDay.id).where(
            CashDay.cash_account_id == account.id, CashDay.date == day, CashDay.status != REJECTED
        )
    )
    if existing is not None:
        raise FinanceRuleError(
            "day_closed",
            "Bu gün zaten kapatılmış; hata ertesi gün düzeltme fişiyle giderilir.",
            conflict=True,
        )
    system = await balance_at(session, account.id, day)
    difference = counted - system
    text = " ".join((reason or "").split())[:500] or None
    if difference != ZERO and (text is None or len(text) < REASON_MIN):
        side = "eksik" if difference < ZERO else "fazla"
        raise FinanceRuleError(
            "difference_reason",
            f"Sayım sistem bakiyesinden {format_money_tr(abs(difference))} {side}. "
            "Farkın gerekçesini yazın (en az 10 karakter).",
            field="reason",
        )
    needs_approval = difference != ZERO and (
        await approvals_svc.match_rule(
            session, ActionType.CASH_DIFFERENCE, amount=abs(difference), source=None, today=today
        )
        is not None
    )
    row = CashDay(
        id=uuid.uuid7(),
        cash_account_id=account.id,
        date=day,
        system_balance=system,
        counted=counted,
        difference=difference,
        reason=text,
        status=PENDING if needs_approval else CLOSED,
        closed_by_id=actor.user_id,
        closed_by=actor.name,
        closed_at=None if needs_approval else now,
    )
    session.add(row)
    await session.flush()
    request = None
    if needs_approval:
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.CASH_DIFFERENCE,
                title=f"Kasa farkı — {account.name}, {format_date_tr(day)}",
                summary={
                    "cash_account": account.name,
                    "date": day.isoformat(),
                    "system_balance": f"{system:.2f}",
                    "counted": f"{counted:.2f}",
                    "difference": f"{difference:.2f}",
                    "reason": text,
                },
                payload={"cash_day_id": str(row.id)},
                amount=abs(difference),
                subject_type="cash_days",
                subject_id=row.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        row.approval_request_id = request.id
        await session.flush()
    return Closed(row, request)


async def _row(session: AsyncSession, request: ApprovalRequest) -> CashDay | None:
    return await session.get(CashDay, uuid.UUID((request.payload or {})["cash_day_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    row = await _row(session, request)
    if row is None or row.status != PENDING:
        raise ExecutionError("Kasa günü artık onay beklemiyor.")
    row.status = CLOSED
    row.closed_at = request.decided_at
    await session.flush()


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    row = await _row(session, request)
    if row is not None and row.status == PENDING:
        row.status = REJECTED
        await session.flush()


approvals_svc.register_executor(ActionType.CASH_DIFFERENCE, _execute)
approvals_svc.register_closer(ActionType.CASH_DIFFERENCE, _close)
