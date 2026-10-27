"""Kesinleşmiş tahakkukta satır bazlı düzeltme (Yazılım Dokümanı FIN-05 "Kesinleşmiş tahakkukta
düzeltme"; D-10). Site kapsamında.

1. Muhasebe hatalı satırı seçer, yeni tutarı ve gerekçeyi girer.
2. "Tahakkuk düzeltme" onay kuralı varsa düzeltme onay bekler (202); yoksa hemen uygulanır.
3. Uygulama: borcun güncel defter hareketi **ters kaydedilir** ve düzeltilmiş toplamla **yeni borç**
   yazılır (aynı vade). Orijinal satır ve hareket değişmez; ters kayıt ve yeni kayıt ikisi de
   görünür. Ödenmiş tutar avansa döner ve yeni borca mahsup edilir (D-6).
4. Orijinal satırın dönemi kilitliyse ters ve yeni kayıt **açık döneme** (bugünün tarihiyle)
   "önceki döneme ait" işaretiyle yazılır; değilse orijinal tarihle aynı döneme.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import ChargeRunStatus, FinanceRuleError, LedgerSource
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Charge,
    ChargeAdjustment,
    ChargeLine,
    LedgerEntry,
    Site,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import charging, ledger, payments, period_lock, summary
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ

PENDING, APPLIED, REJECTED = "approval_pending", "applied", "rejected"
_REVERSAL = aliased(LedgerEntry)


async def get_line(session: AsyncSession, line_id: uuid.UUID) -> ChargeLine | None:
    line: ChargeLine | None = await session.scalar(
        select(ChargeLine).where(ChargeLine.id == line_id)
    )
    return line


async def current_line_amounts(
    session: AsyncSession, line_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Uygulanmış son düzeltmeye göre satırın güncel tutarı (düzeltilmemişse yok)."""
    if not line_ids:
        return {}
    out: dict[uuid.UUID, Decimal] = {}
    for adj in await session.scalars(
        select(ChargeAdjustment)
        .where(ChargeAdjustment.charge_line_id.in_(line_ids), ChargeAdjustment.status == APPLIED)
        .order_by(ChargeAdjustment.created_at)
    ):
        out[adj.charge_line_id] = adj.new_amount
    return out


async def list_for_charges(
    session: AsyncSession, charge_ids: list[uuid.UUID]
) -> list[ChargeAdjustment]:
    if not charge_ids:
        return []
    return list(
        await session.scalars(
            select(ChargeAdjustment)
            .where(ChargeAdjustment.charge_id.in_(charge_ids))
            .order_by(ChargeAdjustment.created_at)
        )
    )


async def _current_entry(session: AsyncSession, charge: Charge) -> LedgerEntry | None:
    """Borcun ters kaydı alınmamış güncel hareketi (orijinal ya da son düzeltmenin yenisi)."""
    entry: LedgerEntry | None = await session.scalar(
        select(LedgerEntry)
        .where(
            LedgerEntry.source == LedgerSource.CHARGE.value,
            LedgerEntry.source_id == charge.id,
            LedgerEntry.reversal_of_entry_id.is_(None),
            ~select(_REVERSAL.id)
            .where(_REVERSAL.reversal_of_entry_id == LedgerEntry.id)
            .correlate(LedgerEntry)
            .exists(),
        )
        .order_by(LedgerEntry.created_at.desc())
        .limit(1)
    )
    return entry


@dataclass(frozen=True, slots=True)
class Requested:
    adjustment: ChargeAdjustment
    request: ApprovalRequest | None


async def request(
    session: AsyncSession,
    site: Site,
    line: ChargeLine,
    *,
    new_amount: Decimal,
    reason: str,
    actor: Person,
    today: date,
    now: datetime,
) -> Requested:
    errors: dict[str, str] = {}
    reason = " ".join(reason.split())
    if not 3 <= len(reason) <= 500:
        errors["reason"] = "Gerekçe zorunlu (3–500 karakter)."
    if new_amount < ZERO or new_amount != round_money(new_amount):
        errors["new_amount"] = "Yeni tutar sıfır ya da daha büyük olmalı (en fazla 2 ondalık)."
    if errors:
        raise FormFieldError(errors)
    charge = await session.scalar(select(Charge).where(Charge.id == line.charge_id))
    run = await charging.get_run(session, charge.charge_run_id) if charge else None
    if charge is None or run is None or run.status != ChargeRunStatus.POSTED.value:
        raise FinanceRuleError(
            "charge_not_posted",
            "Yalnız kesinleşmiş (ters kaydı alınmamış) tahakkukun satırı düzeltilebilir.",
            conflict=True,
        )
    pending = await session.scalar(
        select(func.count()).where(
            ChargeAdjustment.charge_id == charge.id, ChargeAdjustment.status == PENDING
        )
    )
    if pending:
        raise FinanceRuleError(
            "adjustment_pending",
            "Bu borç için onay bekleyen bir düzeltme var; önce onu sonuçlandırın.",
            conflict=True,
        )
    current = (await current_line_amounts(session, [line.id])).get(line.id, line.amount)
    if new_amount == current:
        raise FormFieldError({"new_amount": "Yeni tutar mevcut tutarla aynı."})
    adjustment = ChargeAdjustment(
        id=uuid.uuid7(),
        charge_id=charge.id,
        charge_line_id=line.id,
        ledger_account_id=charge.ledger_account_id,
        old_amount=current,
        new_amount=new_amount,
        reason=reason,
        status=PENDING,
        requested_by_id=actor.user_id,
        requested_by=actor.name,
    )
    session.add(adjustment)
    await session.flush()
    difference = abs(new_amount - current)
    if (
        await approvals_svc.match_rule(
            session, ActionType.CHARGE_ADJUSTMENT, amount=difference, source=None, today=today
        )
        is None
    ):
        await apply(session, adjustment, today=today)
        return Requested(adjustment, None)
    period = await charging.run_period(session, run)
    approval = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CHARGE_ADJUSTMENT,
            title=f"Tahakkuk düzeltme — {period.name} · {line.description}: "
            f"{format_money_tr(current)} → {format_money_tr(new_amount)}",
            summary={
                "period": period.name,
                "line": line.description,
                "old_amount": str(current),
                "new_amount": str(new_amount),
                "reason": reason,
            },
            payload={"adjustment_id": str(adjustment.id)},
            amount=difference,
            subject_type="charge_adjustments",
            subject_id=adjustment.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    adjustment.approval_request_id = approval.id
    await session.flush()
    return Requested(adjustment, approval)


async def apply(session: AsyncSession, adjustment: ChargeAdjustment, *, today: date) -> None:
    charge = await session.scalar(select(Charge).where(Charge.id == adjustment.charge_id))
    line = await get_line(session, adjustment.charge_line_id)
    if charge is None or line is None:  # pragma: no cover - FK
        raise FinanceRuleError("charge_not_found", "Tahakkuk satırı bulunamadı.")
    run = await charging.get_run(session, charge.charge_run_id)
    if run is None or run.status != ChargeRunStatus.POSTED.value:
        raise FinanceRuleError(
            "charge_not_posted",
            "Tahakkuk ters kayıtla iptal edilmiş; satır düzeltmesi uygulanamaz.",
            conflict=True,
        )
    period = await charging.run_period(session, run)
    await ledger.lock_accounts(session, [charge.ledger_account_id])
    entry = await _current_entry(session, charge)
    if entry is None:
        raise FinanceRuleError(
            "charge_entry_missing", "Borcun geçerli defter hareketi bulunamadı.", conflict=True
        )
    current = (await current_line_amounts(session, [line.id])).get(line.id, line.amount)
    old_total = entry.debit - entry.credit
    new_total = old_total - current + adjustment.new_amount
    # Orijinal dönem kilitliyse açık döneme "önceki döneme ait" (FIN-05 adım 5–6).
    day, prior = entry.date, False
    try:
        await period_lock.ensure_writable(session, entry.date, original=True)
    except FinanceRuleError:
        await period_lock.ensure_writable(session, today)
        day, prior = today, True
    suffix = f" (önceki döneme ait — {period.name})" if prior else ""
    reversal = LedgerEntry(
        id=uuid.uuid7(),
        account_id=charge.ledger_account_id,
        date=day,
        due_date=None,
        debit=entry.credit,
        credit=entry.debit,
        source=LedgerSource.CHARGE.value,
        source_id=charge.id,
        description=f"DÜZELTME — {period.name} tahakkuku, '{line.description}' satırı ters "
        f"kaydı{suffix}",
        reversal_of_entry_id=entry.id,
    )
    session.add(reversal)
    new_entry = None
    if new_total != ZERO:
        new_entry = LedgerEntry(
            id=uuid.uuid7(),
            account_id=charge.ledger_account_id,
            date=day,
            due_date=entry.due_date if new_total > ZERO else None,
            debit=new_total if new_total > ZERO else ZERO,
            credit=-new_total if new_total < ZERO else ZERO,
            source=LedgerSource.CHARGE.value,
            source_id=charge.id,
            description=f"DÜZELTME — {period.name} tahakkuku, '{line.description}' "
            f"{format_money_tr(current)} → {format_money_tr(adjustment.new_amount)}{suffix}",
        )
        session.add(new_entry)
    await session.flush()
    adjustment.old_amount = current
    adjustment.status = APPLIED
    adjustment.prior_period = prior
    adjustment.applied_on = day
    adjustment.reversal_entry_id = reversal.id
    adjustment.new_entry_id = new_entry.id if new_entry else None
    await ledger.refresh_balances(session, [charge.ledger_account_id])
    target = period if not prior else YearMonth.of(day)
    await summary.add(session, year=target.year, month=target.month, charged=new_total - old_total)
    await payments.apply_advances(session, [charge.ledger_account_id])
    await session.flush()


async def _adjustment_of(
    session: AsyncSession, request: ApprovalRequest
) -> ChargeAdjustment | None:
    return await session.get(ChargeAdjustment, uuid.UUID((request.payload or {})["adjustment_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    adjustment = await _adjustment_of(session, request)
    if adjustment is None or adjustment.status != PENDING:
        raise ExecutionError("Düzeltme artık onay beklemiyor.")
    decided = (request.decided_at or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    try:
        await apply(session, adjustment, today=decided.date())
    except FinanceRuleError as exc:
        raise ExecutionError(exc.message) from exc


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    adjustment = await _adjustment_of(session, request)
    if adjustment is not None and adjustment.status == PENDING:
        adjustment.status = REJECTED
        await session.flush()


approvals_svc.register_executor(ActionType.CHARGE_ADJUSTMENT, _execute)
approvals_svc.register_closer(ActionType.CHARGE_ADJUSTMENT, _close)
