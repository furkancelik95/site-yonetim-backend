"""Ödeme planı — borç yapılandırma (FIN-12, §8.9; servis isteği 30). Açık site kapsamında.

Doküman modeli (deftere yazılır): plan yürürlüğe girince
1. yapılandırılan tutar kadar **alacak** hareketi (`source = payment_plan`) yazılır ve en eski
   açık kalemleri FIFO kapatır (`payment_plan_closures` — açık borç hesabı bunu düşer);
2. her taksit ayrı vadeli **borç** hareketidir (`payment_plan_installments`).
Bakiye değişmez; yalnız vadeler yeniden düzenlenir. Sonraki tahsilatlar FIFO ile taksitlere
mahsup edilir.

- Taksitler eşit; kuruş artığı ilk taksitlere (1.000,00 / 3 = 333,34 + 333,33 + 333,33).
- "Ödeme planı" onay kuralı varsa plan onay bekler (202), onaylanınca deftere yazılır.
- Durum taksitlerden hesaplanır: hepsi ödendi → tamamlandı; bir taksit vade + tolerans geçip
  ödenmediyse → bozuldu (yöneticiye bir kez bildirim; kalan taksitler normal kurallara döner).
- İptal (gerekçe zorunlu): ödenmemiş taksit tutarı kapanır, aynı tutar yapılandırılan en eski
  vadeyle yeniden borç olarak açılır — silme yok, bakiye değişmez.
- Gecikme tazminatı defterde henüz işlenmiyor; `plan_suspends_late_fee` o gelince uygulanır.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import ROLE_PERMISSIONS, Permission
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.payments import allocate
from site_yonetim.domain.debt_tracking import (
    InstallmentStatus,
    PlannedInstallment,
    PlanStatus,
    check_cancel_reason,
    check_plan,
    installment_status,
    plan_status,
    schedule,
)
from site_yonetim.domain.finance import FinanceRuleError, LedgerSource
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    LedgerAccount,
    LedgerEntry,
    PaymentAllocation,
    PaymentPlan,
    PaymentPlanClosure,
    PaymentPlanInstallment,
    Site,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import finance_params, ledger, payments, period_lock
from site_yonetim.services import members as members_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.charging import BUSINESS_TZ

ACTIVE, PENDING = PlanStatus.ACTIVE.value, PlanStatus.APPROVAL_PENDING.value


# --- Okuma --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallmentView:
    no: int
    due_date: date
    amount: Decimal
    paid: Decimal
    status: InstallmentStatus


async def _paid_by_entry(
    session: AsyncSession, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not entry_ids:
        return {}
    rows = await session.execute(
        select(PaymentAllocation.ledger_entry_id, func.sum(PaymentAllocation.amount))
        .where(PaymentAllocation.ledger_entry_id.in_(entry_ids))
        .group_by(PaymentAllocation.ledger_entry_id)
    )
    paid: dict[uuid.UUID, Decimal] = {}
    for entry_id, total in rows:
        paid[entry_id] = total
    return paid


async def installments(
    session: AsyncSession, plans: list[PaymentPlan], today: date
) -> dict[uuid.UUID, list[InstallmentView]]:
    """Plan → taksitler (ödenen = taksit borcuna yapılan tahsilat mahsupları)."""
    ids = [p.id for p in plans]
    stored: dict[uuid.UUID, list[PaymentPlanInstallment]] = defaultdict(list)
    if ids:
        for row in await session.scalars(
            select(PaymentPlanInstallment)
            .where(PaymentPlanInstallment.plan_id.in_(ids))
            .order_by(PaymentPlanInstallment.no)
        ):
            stored[row.plan_id].append(row)
    paid = await _paid_by_entry(
        session, [i.ledger_entry_id for rows in stored.values() for i in rows]
    )
    out: dict[uuid.UUID, list[InstallmentView]] = {}
    for plan in plans:
        if plan.id in stored:
            out[plan.id] = [
                InstallmentView(
                    i.no,
                    i.due_date,
                    i.amount,
                    paid.get(i.ledger_entry_id, ZERO),
                    installment_status(
                        i.amount,
                        paid.get(i.ledger_entry_id, ZERO),
                        i.due_date,
                        plan.tolerance_days,
                        today,
                    ),
                )
                for i in stored[plan.id]
            ]
        else:  # onay bekliyor / reddedildi: önizleme takvimi
            out[plan.id] = [
                InstallmentView(p.no, p.due_date, p.amount, ZERO, InstallmentStatus.OPEN)
                for p in schedule(plan.total, plan.installment_count, plan.first_due_date)
            ]
    return out


def derived_status(plan: PaymentPlan, views: list[InstallmentView]) -> PlanStatus:
    if plan.status != ACTIVE:
        return PlanStatus(plan.status)
    return plan_status([v.status for v in views])


async def plan_statuses(
    session: AsyncSession, today: date, *, account_id: uuid.UUID | None = None
) -> list[tuple[PaymentPlan, PlanStatus]]:
    query = select(PaymentPlan).order_by(PaymentPlan.created_at.desc())
    if account_id is not None:
        query = query.where(PaymentPlan.account_id == account_id)
    plans = list(await session.scalars(query))
    views = await installments(session, [p for p in plans if p.status == ACTIVE], today)
    return [(p, derived_status(p, views.get(p.id, []))) for p in plans]


async def get(
    session: AsyncSession, plan_id: uuid.UUID, *, lock: bool = False
) -> PaymentPlan | None:
    query = select(PaymentPlan).where(PaymentPlan.id == plan_id)
    found: PaymentPlan | None = await session.scalar(query.with_for_update() if lock else query)
    return found


# --- Oluşturma ve yürürlük -----------------------------------------------------------------


async def _open_total(session: AsyncSession, account_id: uuid.UUID) -> Decimal:
    debts = sum((d.open_amount for d in await payments.open_debts(session, account_id)), ZERO)
    return min(debts, await payments.balance_of(session, account_id))


async def _has_plan_in_force(session: AsyncSession, account_id: uuid.UUID, today: date) -> bool:
    for _plan, status in await plan_statuses(session, today, account_id=account_id):
        if status in (PlanStatus.APPROVAL_PENDING, PlanStatus.ACTIVE):
            return True
    return False


@dataclass(frozen=True, slots=True)
class Created:
    plan: PaymentPlan
    request: ApprovalRequest | None


async def create(
    session: AsyncSession,
    site: Site,
    account: LedgerAccount,
    *,
    amount: Decimal | None,
    installment_count: int | None,
    first_due_date: date | None,
    note: str | None,
    actor: Person,
    today: date,
    now: datetime,
) -> Created:
    if account.is_closed:
        raise FinanceRuleError(
            "account_closed", "Bu cari hesap kapatılmış; ödeme planı açılamaz.", conflict=True
        )
    await ledger.lock_accounts(session, [account.id])
    if await _has_plan_in_force(session, account.id, today):
        raise FinanceRuleError(
            "plan_exists",
            "Bu hesapta yürürlükte bir ödeme planı var; önce onu iptal edin.",
            conflict=True,
        )
    check_plan(
        amount, installment_count, first_due_date, await _open_total(session, account.id), today
    )
    if amount is None or installment_count is None or first_due_date is None:  # pragma: no cover
        raise FinanceRuleError("validation", "Plan bilgileri eksik.")  # check_plan zaten reddeder
    await period_lock.ensure_writable(session, today)
    params = await finance_params.current(session, today)
    needs_approval = (
        await approvals_svc.match_rule(
            session, ActionType.PAYMENT_PLAN, amount=amount, source=None, today=today
        )
        is not None
    )
    plan = PaymentPlan(
        id=uuid.uuid7(),
        account_id=account.id,
        total=amount,
        installment_count=installment_count,
        first_due_date=first_due_date,
        tolerance_days=int(params["plan_tolerance_days"]),
        note=" ".join((note or "").split())[:500] or None,
        status=PENDING,
        created_by_id=actor.user_id,
        created_by=actor.name,
    )
    session.add(plan)
    await session.flush()
    if not needs_approval:
        await activate(session, plan, account, today)
        return Created(plan, None)
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.PAYMENT_PLAN,
            title=f"Ödeme planı — {account.reference_code}, {installment_count} taksit, "
            f"{format_money_tr(amount)}",
            summary={
                "reference_code": account.reference_code,
                "total": f"{amount:.2f}",
                "installment_count": installment_count,
                "first_due_date": first_due_date.isoformat(),
            },
            payload={"plan_id": str(plan.id)},
            amount=amount,
            subject_type="payment_plans",
            subject_id=plan.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    plan.approval_request_id = request.id
    await session.flush()
    return Created(plan, request)


async def activate(
    session: AsyncSession, plan: PaymentPlan, account: LedgerAccount, day: date
) -> None:
    """Planı deftere yazar: açık kalemler kapanır, taksitler vadeli borç olarak açılır."""
    await period_lock.ensure_writable(session, day)
    await ledger.lock_accounts(session, [account.id])
    result = allocate(plan.total, await payments.open_debts(session, account.id))
    if result.unapplied > ZERO:
        raise FinanceRuleError(
            "plan_exceeds_debt",
            "Açık borç plan tutarından az; plan yürürlüğe giremez. Planı yeniden oluşturun.",
            conflict=True,
        )
    planned: list[PlannedInstallment] = schedule(
        plan.total, plan.installment_count, plan.first_due_date
    )
    session.add(
        LedgerEntry(
            account_id=account.id,
            date=day,
            credit=plan.total,
            source=LedgerSource.PAYMENT_PLAN.value,
            source_id=plan.id,
            description=f"Ödeme planına aktarıldı — {plan.installment_count} taksit",
        )
    )
    session.add_all(
        PaymentPlanClosure(
            plan_id=plan.id, ledger_entry_id=a.entry_id, kind="restructure", amount=a.amount
        )
        for a in result.allocations
    )
    for item in planned:
        entry = LedgerEntry(
            id=uuid.uuid7(),
            account_id=account.id,
            date=day,
            due_date=item.due_date,
            debit=item.amount,
            source=LedgerSource.PAYMENT_PLAN.value,
            source_id=plan.id,
            description=f"Ödeme planı taksiti {item.no}/{plan.installment_count}",
        )
        session.add(entry)
        session.add(
            PaymentPlanInstallment(
                plan_id=plan.id,
                no=item.no,
                due_date=item.due_date,
                amount=item.amount,
                ledger_entry_id=entry.id,
            )
        )
    plan.status = ACTIVE
    plan.activated_on = day
    await session.flush()
    await ledger.refresh_balances(session, [account.id])


# --- İptal --------------------------------------------------------------------------------


async def cancel(
    session: AsyncSession,
    plan: PaymentPlan,
    *,
    reason: str | None,
    actor: Person,
    today: date,
    now: datetime,
) -> PaymentPlan:
    text = check_cancel_reason(reason)
    if plan.status in (PlanStatus.CANCELLED.value, PlanStatus.REJECTED.value):
        raise FinanceRuleError("already_cancelled", "Plan zaten iptal edilmiş.", conflict=True)
    if plan.status == PENDING:
        raise FinanceRuleError(
            "plan_pending",
            "Onay bekleyen plan iptal edilmez; talebi onay kutusundan geri çekin.",
            conflict=True,
        )
    views = (await installments(session, [plan], today))[plan.id]
    if derived_status(plan, views) is PlanStatus.COMPLETED:
        raise FinanceRuleError(
            "plan_completed", "Tüm taksitleri ödenmiş plan iptal edilemez.", conflict=True
        )
    await period_lock.ensure_writable(session, today)
    await ledger.lock_accounts(session, [plan.account_id])
    rows = list(
        await session.scalars(
            select(PaymentPlanInstallment).where(PaymentPlanInstallment.plan_id == plan.id)
        )
    )
    open_by_entry = {
        d.entry_id: d.open_amount for d in await payments.open_debts(session, plan.account_id)
    }
    closures = [
        (r.ledger_entry_id, open_by_entry[r.ledger_entry_id])
        for r in rows
        if open_by_entry.get(r.ledger_entry_id, ZERO) > ZERO
    ]
    remaining = sum((amount for _, amount in closures), ZERO)
    if remaining > ZERO:
        oldest = await session.scalar(
            select(func.min(func.coalesce(LedgerEntry.due_date, LedgerEntry.date)))
            .join(PaymentPlanClosure, PaymentPlanClosure.ledger_entry_id == LedgerEntry.id)
            .where(PaymentPlanClosure.plan_id == plan.id, PaymentPlanClosure.kind == "restructure")
        )
        session.add(
            LedgerEntry(
                account_id=plan.account_id,
                date=today,
                credit=remaining,
                source=LedgerSource.PAYMENT_PLAN.value,
                source_id=plan.id,
                description="Ödeme planı iptali — ödenmemiş taksitler kapatıldı",
            )
        )
        session.add_all(
            PaymentPlanClosure(
                plan_id=plan.id, ledger_entry_id=entry_id, kind="cancel", amount=amount
            )
            for entry_id, amount in closures
        )
        session.add(
            LedgerEntry(
                account_id=plan.account_id,
                date=today,
                due_date=oldest or today,
                debit=remaining,
                source=LedgerSource.PAYMENT_PLAN.value,
                source_id=plan.id,
                description="Ödeme planı iptali — kalan borç ilk vadesiyle yeniden açıldı",
            )
        )
    plan.status = PlanStatus.CANCELLED.value
    plan.cancelled_at = now
    plan.cancel_reason = text
    plan.cancelled_by = actor.name
    await session.flush()
    await ledger.refresh_balances(session, [plan.account_id])
    return plan


# --- Onay ---------------------------------------------------------------------------------


async def _plan_of(session: AsyncSession, request: ApprovalRequest) -> PaymentPlan | None:
    return await session.get(PaymentPlan, uuid.UUID((request.payload or {})["plan_id"]))


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    plan = await _plan_of(session, request)
    if plan is None or plan.status != PENDING:
        raise ExecutionError("Ödeme planı artık onay beklemiyor.")
    account = await session.get(LedgerAccount, plan.account_id)
    if account is None or account.is_closed:
        raise ExecutionError("Cari hesap kapatılmış; plan yürürlüğe giremez.")
    decided = (request.decided_at or datetime.now().astimezone()).astimezone(BUSINESS_TZ)
    try:
        await activate(session, plan, account, decided.date())
    except FinanceRuleError as exc:
        raise ExecutionError(exc.message) from exc


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    plan = await _plan_of(session, request)
    if plan is not None and plan.status == PENDING:
        plan.status = PlanStatus.REJECTED.value
        await session.flush()


approvals_svc.register_executor(ActionType.PAYMENT_PLAN, _execute)
approvals_svc.register_closer(ActionType.PAYMENT_PLAN, _close)


# --- Uyum kontrolü işi ------------------------------------------------------------------


async def mark_broken(session: AsyncSession, site: Site, today: date, now: datetime) -> int:
    """Günlük iş: yeni bozulan planları işaretler ve finans yetkililerine bir kez bildirir."""
    newly = [
        plan
        for plan, status in await plan_statuses(session, today)
        if status is PlanStatus.BROKEN and plan.broken_at is None
    ]
    if not newly:
        return 0
    for plan in newly:
        plan.broken_at = now
    await session.flush()
    users = {
        m.user.id
        for m in await members_svc.list_members(session, site)
        if m.is_active and Permission.FINANCE_PAYMENT_RECORD in ROLE_PERMISSIONS[m.role]
    }
    for plan in newly:
        await notify_svc.notify(
            session,
            users,
            notify_svc.Message(
                category=NotificationCategory.FINANCIAL,
                kind="payment_plan.broken",
                title="Ödeme planı bozuldu",
                body="Bir taksit vade + tolerans gününde ödenmedi; kalan taksitler normal "
                "gecikme ve hatırlatma kurallarına döndü.",
                link_entity="payment_plans",
                link_id=plan.id,
            ),
            dedupe_key=f"payment-plan-broken:{plan.id}",
        )
    return len(newly)
