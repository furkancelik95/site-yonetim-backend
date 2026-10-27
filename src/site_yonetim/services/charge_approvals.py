"""Tahakkuk kaydı ve tahakkuk ters kaydının onay kuralına bağlanması (PM kararı K21).

Kural (`charge_run` / `charge_run_reversal`) yoksa uçlar bugünkü gibi hemen yazar. Kural varsa
talep açılır, defter değişmez; onaylanınca yürütücü **onay günündeki** durumla `charging.post` /
`charging.reverse` çağırır (dönem kilidi ve "dönem zaten kesilmiş" denetimleri o anda yeniden
çalışır; olmazsa "onaylandı – yürütülemedi"). Ret ve geri çekmede kayıt yazılmamış kalır.

`services/charging.py` onay modülünü içe aktaramaz (onay servisi `charging.BUSINESS_TZ`
kullanır); bu yüzden bağlama ayrı modülde.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType
from site_yonetim.domain.finance import ChargeRunStatus, FinanceRuleError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import ChargeRun, Period, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import charging, period_lock
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ


async def _open_of(session: AsyncSession, action: ActionType) -> list[ApprovalRequest]:
    return list(
        await session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.action_type == action.value,
                ApprovalRequest.status.in_([s.value for s in OPEN_STATUSES]),
            )
        )
    )


def _already_open() -> FinanceRuleError:
    return FinanceRuleError(
        "approval_pending",
        "Bu işlem için onay talebi zaten açık; talep sonuçlanana kadar yeni talep açılamaz.",
        conflict=True,
    )


# --- Tahakkuk kaydı -----------------------------------------------------------------------


async def request_post_if_ruled(
    session: AsyncSession,
    site: Site,
    prepared: charging.PreparedRun,
    *,
    requester: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    amount = prepared.preview.total_amount
    if (
        await approvals_svc.match_rule(
            session, ActionType.CHARGE_RUN, amount=amount, source=None, today=today
        )
        is None
    ):
        return None
    period_key = f"{prepared.period.year}-{prepared.period.month:02d}"
    if any(
        (r.payload or {}).get("period") == period_key
        for r in await _open_of(session, ActionType.CHARGE_RUN)
    ):
        raise _already_open()
    # Kaydetme denetimleri (dönem kesilmiş mi, kesilecek var mı) onay açılmadan görünsün.
    if not prepared.preview.charges:
        raise FinanceRuleError("nothing_to_charge", "Kesilecek tahakkuk yok.", conflict=True)
    await period_lock.ensure_writable(session, prepared.charge_date)
    period_id = await session.scalar(
        select(Period.id).where(
            Period.year == prepared.period.year, Period.month == prepared.period.month
        )
    )
    existing = await charging.valid_run_of(session, period_id) if period_id else None
    if existing is not None:
        raise charging.already_posted(prepared.period, existing)
    return await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CHARGE_RUN,
            title=f"Tahakkuk — {prepared.period.name}, {prepared.preview.unit_count} bölüm, "
            f"{format_money_tr(amount)}",
            summary={
                "period": period_key,
                "unit_count": prepared.preview.unit_count,
                "total": f"{amount:.2f}",
                "charge_date": prepared.charge_date.isoformat(),
                "due_date": prepared.due_date.isoformat(),
            },
            payload={
                "period": period_key,
                "charge_date": prepared.charge_date.isoformat(),
                "due_date": prepared.due_date.isoformat(),
            },
            amount=Decimal(amount),
            subject_type="charge_runs",
        ),
        requester=requester,
        today=today,
        now=now,
    )


async def _execute_post(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    decided = request.decided_at or datetime.now().astimezone()
    try:
        prepared = await charging.prepare(
            session,
            decided.astimezone(BUSINESS_TZ).date(),
            charge_date=date.fromisoformat(payload["charge_date"]),
            due_date=date.fromisoformat(payload["due_date"]),
        )
        await charging.post(session, prepared, posted_by=request.requested_by_name, now=decided)
    except FinanceRuleError as exc:
        raise ExecutionError(f"Tahakkuk artık kesilemiyor: {exc.message}") from exc


# --- Tahakkuk ters kaydı ------------------------------------------------------------------


async def request_reversal_if_ruled(
    session: AsyncSession,
    site: Site,
    run: ChargeRun,
    *,
    reason: str,
    total: Decimal,
    requester: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    if (
        await approvals_svc.match_rule(
            session, ActionType.CHARGE_RUN_REVERSAL, amount=total, source=None, today=today
        )
        is None
    ):
        return None
    if run.status != ChargeRunStatus.POSTED.value or run.reversal_of_run_id is not None:
        raise FinanceRuleError(
            "run_not_reversible",
            "Yalnızca kaydedilmiş koşular ters kayıtla iptal edilebilir.",
            conflict=True,
        )
    if any(
        (r.payload or {}).get("run_id") == str(run.id)
        for r in await _open_of(session, ActionType.CHARGE_RUN_REVERSAL)
    ):
        raise _already_open()
    period = await charging.run_period(session, run)
    return await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CHARGE_RUN_REVERSAL,
            title=f"Tahakkuk iptali — {period.name}, {format_money_tr(total)}",
            summary={"period": period.name, "total": f"{total:.2f}", "reason": reason},
            payload={"run_id": str(run.id), "reason": reason},
            amount=total,
            subject_type="charge_runs",
            subject_id=run.id,
        ),
        requester=requester,
        today=today,
        now=now,
    )


async def _execute_reversal(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    run = await charging.get_run(session, uuid.UUID(payload["run_id"]))
    if run is None:
        raise ExecutionError("Tahakkuk koşusu bulunamadı.")
    decided = request.decided_at or datetime.now().astimezone()
    try:
        await charging.reverse(
            session,
            run,
            reason=payload["reason"],
            today=decided.astimezone(BUSINESS_TZ).date(),
            reversed_by=request.requested_by_name,
            now=decided,
        )
    except FinanceRuleError as exc:
        raise ExecutionError(f"Tahakkuk artık iptal edilemiyor: {exc.message}") from exc


approvals_svc.register_executor(ActionType.CHARGE_RUN, _execute_post)
approvals_svc.register_executor(ActionType.CHARGE_RUN_REVERSAL, _execute_reversal)
