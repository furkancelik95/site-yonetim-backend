"""Tahakkuk taslağı → "Tahakkuk" onayı → kesinleştirme (Yazılım Dokümanı FIN-05; PM K21, K25).

Dönem durum modeli: Taslak → Onay bekliyor → Onaylandı → Kesinleşti. Taslak, önizlemenin anlık
görüntüsünü saklar (`charge_run_drafts`); onaylanan taslak bu görüntüden kesinleşir — onaycının
gördüğü tutar deftere yazılan tutardır.

- "Tahakkuk" onay kuralı **varsa** gönderilen taslak onaya gider (202); onaylanınca tahakkuk
  tarihi geldiyse hemen, gelmediyse tahakkuk tarihinde (`finalize_due`, gece işi) kesinleşir.
- Kural **yoksa** gönderim bugünkü gibi doğrudan kesinleştirir.
- Reddedilen taslak yenilenip (yeniden hesaplanıp) yeniden gönderilebilir ya da iptal edilir.
- Bir dönemde en fazla bir açık taslak (kısmi benzersiz indeks); kesinleşmiş dönem için taslak
  açılamaz. Kesinleştirme tekrar güvenlidir: aynı dönem iki kez kesinleşemez (FIN-05 S2).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging import snapshot
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import OPEN_DRAFT_STATUSES, ChargeRunStatus, FinanceRuleError
from site_yonetim.domain.finance_params import ChargeSource
from site_yonetim.domain.text import format_date_tr
from site_yonetim.models import BudgetPlan, ChargeRun, ChargeRunDraft, Site
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import charging, period_lock
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ, PostedRun, PreparedRun

_OPEN = [s.value for s in OPEN_DRAFT_STATUSES]


async def open_draft_of(session: AsyncSession, period_id: uuid.UUID) -> ChargeRun | None:
    found: ChargeRun | None = await session.scalar(
        select(ChargeRun).where(ChargeRun.period_id == period_id, ChargeRun.status.in_(_OPEN))
    )
    return found


async def get_snapshot(session: AsyncSession, run: ChargeRun) -> ChargeRunDraft | None:
    found: ChargeRunDraft | None = await session.scalar(
        select(ChargeRunDraft).where(ChargeRunDraft.charge_run_id == run.id)
    )
    return found


async def prepared_of(session: AsyncSession, run: ChargeRun) -> PreparedRun:
    """Taslak koşunun saklanan önizlemesi (onaycının gördüğü)."""
    row = await get_snapshot(session, run)
    if row is None:
        raise FinanceRuleError("draft_missing", "Taslak önizlemesi bulunamadı.", conflict=True)
    preview, not_due = snapshot.load(row.snapshot)
    plan = (
        await session.scalar(select(BudgetPlan).where(BudgetPlan.id == run.budget_plan_id))
        if run.budget_plan_id
        else None
    )
    return PreparedRun(
        period=await charging.run_period(session, run),
        charge_date=run.charge_date,
        due_date=run.due_date,
        plan=plan,
        preview=preview,
        not_due_items=not_due,
        source=ChargeSource(run.source),
    )


async def create(
    session: AsyncSession,
    today: date,
    *,
    created_by: str,
    charge_date: date | None = None,
    due_date: date | None = None,
) -> tuple[ChargeRun, PreparedRun]:
    """Taslak: önizleme hesaplanır ve saklanır; deftere hiçbir şey yazılmaz."""
    prepared = await charging.prepare(session, today, charge_date=charge_date, due_date=due_date)
    await period_lock.ensure_writable(session, prepared.charge_date)
    period_row = await period_lock.get_or_create(session, prepared.period)
    existing = await charging.valid_run_of(session, period_row.id)
    if existing is not None:
        raise charging.already_posted(prepared.period, existing)
    if await open_draft_of(session, period_row.id) is not None:
        raise FinanceRuleError(
            "draft_exists",
            f"{prepared.period.name} dönemi için açık bir tahakkuk taslağı var; onu yenileyin ya "
            "da iptal edin.",
            conflict=True,
        )
    if not prepared.preview.charges:
        raise FinanceRuleError("nothing_to_charge", "Kesilecek tahakkuk yok.", conflict=True)
    run = ChargeRun(
        id=uuid.uuid7(),
        period_id=period_row.id,
        source=prepared.source.value,
        budget_plan_id=prepared.plan.id if prepared.plan else None,
        status=ChargeRunStatus.DRAFT.value,
        charge_date=prepared.charge_date,
        due_date=prepared.due_date,
        created_by=created_by,
    )
    session.add(run)
    await session.flush()
    session.add(
        ChargeRunDraft(
            charge_run_id=run.id,
            snapshot=snapshot.dump(prepared.preview, prepared.not_due_items),
        )
    )
    await session.flush()
    return run, prepared


async def refresh(session: AsyncSession, run: ChargeRun, today: date) -> PreparedRun:
    """Taslağı güncel veriyle yeniden hesaplar (taslak ya da reddedilmiş)."""
    if run.status not in (ChargeRunStatus.DRAFT.value, ChargeRunStatus.REJECTED.value):
        raise FinanceRuleError(
            "draft_not_editable",
            "Yalnız taslak ya da reddedilmiş tahakkuk yenilenebilir.",
            conflict=True,
        )
    prepared = await charging.prepare(
        session, today, charge_date=run.charge_date, due_date=run.due_date
    )
    row = await get_snapshot(session, run)
    data = snapshot.dump(prepared.preview, prepared.not_due_items)
    if row is None:
        session.add(ChargeRunDraft(charge_run_id=run.id, snapshot=data))
    else:
        row.snapshot = data
    run.source = prepared.source.value
    run.budget_plan_id = prepared.plan.id if prepared.plan else None
    run.status = ChargeRunStatus.DRAFT.value
    run.approval_request_id = None
    await session.flush()
    return prepared


async def cancel(session: AsyncSession, run: ChargeRun) -> None:
    if run.status not in (
        ChargeRunStatus.DRAFT.value,
        ChargeRunStatus.REJECTED.value,
        ChargeRunStatus.APPROVED.value,
    ):
        raise FinanceRuleError(
            "draft_not_cancellable",
            "Onay bekleyen taslak iptal edilemez; önce onay talebini geri çekin."
            if run.status == ChargeRunStatus.APPROVAL_PENDING.value
            else "Yalnız taslak tahakkuk iptal edilebilir.",
            conflict=True,
        )
    run.status = ChargeRunStatus.CANCELLED.value
    await session.flush()


async def rule_exists(session: AsyncSession, today: date, total: object = None) -> bool:
    from decimal import Decimal

    amount = total if isinstance(total, Decimal) else None
    return (
        await approvals_svc.match_rule(
            session, ActionType.CHARGE_RUN, amount=amount, source=None, today=today
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class Submitted:
    run: ChargeRun
    request: ApprovalRequest | None
    posted: PostedRun | None


async def submit(
    session: AsyncSession,
    site: Site,
    run: ChargeRun,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> Submitted:
    """Kural varsa onaya gönderir; yoksa hemen kesinleştirir (bugünkü davranış)."""
    if run.status not in (ChargeRunStatus.DRAFT.value, ChargeRunStatus.REJECTED.value):
        raise FinanceRuleError(
            "draft_not_submittable", "Yalnız taslak tahakkuk gönderilebilir.", conflict=True
        )
    prepared = await prepared_of(session, run)
    total = prepared.preview.total_amount
    if not await rule_exists(session, today, total):
        posted = await charging.post(session, prepared, posted_by=actor.name, now=now, run=run)
        return Submitted(run, None, posted)
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.CHARGE_RUN,
            title=f"Tahakkuk — {prepared.period.name} ({len(prepared.preview.charges)} borç)",
            summary={
                "period": prepared.period.name,
                "charge_date": prepared.charge_date.isoformat(),
                "due_date": prepared.due_date.isoformat(),
                "total_amount": str(total),
                "unit_count": prepared.preview.unit_count,
                "charge_count": len(prepared.preview.charges),
                "warning_count": len(prepared.preview.warnings),
                "source": prepared.source.value,
            },
            payload={"charge_run_id": str(run.id)},
            amount=total,
            subject_type="charge_runs",
            subject_id=run.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    run.status = ChargeRunStatus.APPROVAL_PENDING.value
    run.approval_request_id = request.id
    await session.flush()
    return Submitted(run, request, None)


async def finalize(session: AsyncSession, run: ChargeRun, *, now: datetime) -> PostedRun:
    """Onaylanmış taslağı saklanan görüntüden kesinleştirir."""
    if run.status != ChargeRunStatus.APPROVED.value:
        raise FinanceRuleError(
            "draft_not_approved", "Yalnız onaylanmış taslak kesinleştirilebilir.", conflict=True
        )
    prepared = await prepared_of(session, run)
    return await charging.post(
        session, prepared, posted_by=run.created_by or "Tahakkuk onayı", now=now, run=run
    )


async def finalize_due(session: AsyncSession, today: date, now: datetime) -> list[PostedRun]:
    """Tahakkuk tarihi gelmiş onaylı taslakları kesinleştirir (gece işi, FIN-05 adım 8)."""
    runs = list(
        await session.scalars(
            select(ChargeRun)
            .where(
                ChargeRun.status == ChargeRunStatus.APPROVED.value, ChargeRun.charge_date <= today
            )
            .order_by(ChargeRun.charge_date)
        )
    )
    posted = []
    for run in runs:
        posted.append(await finalize(session, run, now=now))
    return posted


async def _run_of(session: AsyncSession, request: ApprovalRequest) -> ChargeRun | None:
    return await charging.get_run(session, uuid.UUID((request.payload or {})["charge_run_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    run = await _run_of(session, request)
    if run is None or run.status != ChargeRunStatus.APPROVAL_PENDING.value:
        raise ExecutionError("Tahakkuk taslağı artık onay beklemiyor.")
    run.status = ChargeRunStatus.APPROVED.value
    await session.flush()
    decided = (request.decided_at or datetime.now(BUSINESS_TZ)).astimezone(BUSINESS_TZ)
    if run.charge_date <= decided.date():
        try:
            await finalize(session, run, now=decided)
        except FinanceRuleError as exc:
            raise ExecutionError(exc.message) from exc


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    run = await _run_of(session, request)
    if run is not None and run.status == ChargeRunStatus.APPROVAL_PENDING.value:
        run.status = ChargeRunStatus.REJECTED.value
        await session.flush()


approvals_svc.register_executor(ActionType.CHARGE_RUN, _execute)
approvals_svc.register_closer(ActionType.CHARGE_RUN, _close)


def status_message(run: ChargeRun, period: YearMonth) -> str:
    match ChargeRunStatus(run.status):
        case ChargeRunStatus.APPROVED:
            return (
                f"{period.name} tahakkuku onaylandı; {format_date_tr(run.charge_date)} günü "
                "kesinleşecek."
            )
        case ChargeRunStatus.APPROVAL_PENDING:
            return f"{period.name} tahakkuk taslağı onay bekliyor."
        case _:
            return f"{period.name} tahakkuk taslağı."
