"""Yıllık bütçe ve bütçe–gerçekleşme (Yazılım Dokümanı FIN-17, §8.10; B17, D-16). Site kapsamında.

İşletme projesi (KMK m.37, `budget_plans`) aidatın kaynağıdır; **bütçe** ise yılın gider kategorisi
× ay planıdır ve gerçekleşme ondan izlenir (doküman: "bütçeden aidat türetme Faz 2"). Bağ:

- Bütçe taslağı işletme projesinden önerilebilir: her kalemin yıllık tutarı kategorisine,
  `distribute` ile 12 aya bölünerek yazılır (toplam korunur, D-5).
- Bütçe gelir hedefi, aidat kuralı deneme hesabında karşılaştırılır (FIN-02).

Yaşam döngüsü: taslak → onay bekliyor ("Bütçe" onay kuralı varsa) → yürürlükte → revizyon (yeni
sürüm, aynı onay). Yürürlüğe giren sürüm aynı yılın öncekini `superseded` yapar. İşletme projesi
kesinleştirme de aynı "Bütçe" onay türünden geçer (D-16).

Gerçekleşme: gerçekleşen giderler (§9.4: geri alınmamış, düzeltme kaydı olmayan), belge tarihinin
ayına göre. Uyarı: kategorinin yıllık bütçesinin %`budget_warn_percent`'ine (varsayılan %90) ve
%100'üne ulaşınca muhasebe ve site yöneticisine bildirim — kategori ve eşik başına bir kez.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import ROLE_PERMISSIONS, Permission
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.charging.periods import YearMonth
from site_yonetim.domain.finance import BudgetStatus, FinanceRuleError, Frequency
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.money import ZERO, distribute, round_money
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Budget,
    BudgetAlert,
    BudgetItem,
    BudgetLine,
    BudgetPlan,
    Expense,
    ExpenseCategory,
    Site,
)
from site_yonetim.models.approvals import ApprovalRequest
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import finance_params
from site_yonetim.services import members as members_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.approvals import ExecutionError, Person

DRAFT, PENDING, ACTIVE, REJECTED, SUPERSEDED = (
    "draft",
    "approval_pending",
    "active",
    "rejected",
    "superseded",
)
MONTHS = 12
AMOUNT_MAX = Decimal("1000000000")


async def get(session: AsyncSession, budget_id: uuid.UUID) -> Budget | None:
    found: Budget | None = await session.scalar(select(Budget).where(Budget.id == budget_id))
    return found


async def active_for(session: AsyncSession, year: int) -> Budget | None:
    found: Budget | None = await session.scalar(
        select(Budget).where(Budget.fiscal_year == year, Budget.status == ACTIVE)
    )
    return found


async def income_target(session: AsyncSession, year: int) -> Decimal | None:
    budget = await active_for(session, year)
    return budget.income_target if budget else None


async def lines_of(session: AsyncSession, budget_id: uuid.UUID) -> dict[uuid.UUID, list[Decimal]]:
    """Kategori → 12 aylık tutar."""
    out: dict[uuid.UUID, list[Decimal]] = {}
    for line in await session.scalars(select(BudgetLine).where(BudgetLine.budget_id == budget_id)):
        out.setdefault(line.expense_category_id, [ZERO] * MONTHS)[line.month - 1] = line.amount
    return out


async def totals(session: AsyncSession, budget_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    if not budget_ids:
        return {}
    rows = await session.execute(
        select(BudgetLine.budget_id, func.sum(BudgetLine.amount))
        .where(BudgetLine.budget_id.in_(budget_ids))
        .group_by(BudgetLine.budget_id)
    )
    out: dict[uuid.UUID, Decimal] = {}
    for budget_id, total in rows:
        out[budget_id] = total
    return out


def _check_name(name: str) -> str:
    text = " ".join(name.split())
    if not 2 <= len(text) <= 120:
        raise FormFieldError({"name": "Ad 2–120 karakter olmalı."})
    return text


def _check_target(value: Decimal | None) -> None:
    if value is not None and (value < ZERO or value != round_money(value) or value > AMOUNT_MAX):
        raise FormFieldError({"income_target": "Gelir hedefi sıfır ya da daha büyük olmalı."})


async def _plan_lines(session: AsyncSession, plan: BudgetPlan) -> dict[uuid.UUID, list[Decimal]]:
    """İşletme projesi önerisi: kalemin yıllık tutarı kategorisine, takvimine göre aylara.

    Aylık kalem 12 aya, 3 aylık kalem çeyrek başlarına `distribute` ile; yıllık ve tek seferlik
    kalem Ocak'a. Toplam her zaman kalemin yıllık tutarıdır."""
    out: dict[uuid.UUID, list[Decimal]] = defaultdict(lambda: [ZERO] * MONTHS)
    for item in await session.scalars(
        select(BudgetItem).where(BudgetItem.budget_plan_id == plan.id)
    ):
        months = out[item.expense_category_id]
        frequency = Frequency(item.frequency)
        if frequency is Frequency.MONTHLY:
            for index, part in enumerate(distribute(item.annual_amount, [Decimal(1)] * MONTHS)):
                months[index] += part
        elif frequency is Frequency.QUARTERLY:
            for quarter, part in enumerate(distribute(item.annual_amount, [Decimal(1)] * 4)):
                months[quarter * 3] += part
        else:
            months[0] += item.annual_amount
    return dict(out)


async def _replace_lines(
    session: AsyncSession, budget: Budget, lines: dict[uuid.UUID, list[Decimal]]
) -> None:
    for old in await session.scalars(select(BudgetLine).where(BudgetLine.budget_id == budget.id)):
        await session.delete(old)
    await session.flush()
    session.add_all(
        BudgetLine(budget_id=budget.id, expense_category_id=cat, month=index + 1, amount=amount)
        for cat, months in lines.items()
        for index, amount in enumerate(months)
        if amount > ZERO
    )
    await session.flush()


async def create(
    session: AsyncSession,
    *,
    fiscal_year: int,
    name: str,
    income_target: Decimal | None,
    budget_plan_id: uuid.UUID | None,
    actor: Person,
) -> Budget:
    if not 2000 <= fiscal_year <= 2100:
        raise FormFieldError({"fiscal_year": "Mali yıl 2000–2100 arası olmalı."})
    _check_target(income_target)
    plan = None
    if budget_plan_id is not None:
        plan = await session.scalar(select(BudgetPlan).where(BudgetPlan.id == budget_plan_id))
        if plan is None:
            raise FormFieldError({"budget_plan_id": "İşletme projesi bulunamadı."})
    last = await session.scalar(
        select(func.max(Budget.version)).where(Budget.fiscal_year == fiscal_year)
    )
    budget = Budget(
        id=uuid.uuid7(),
        fiscal_year=fiscal_year,
        version=(last or 0) + 1,
        name=_check_name(name),
        status=DRAFT,
        income_target=income_target,
        budget_plan_id=plan.id if plan else None,
        created_by=actor.name,
    )
    session.add(budget)
    await session.flush()
    if plan is not None:
        await _replace_lines(session, budget, await _plan_lines(session, plan))
    return budget


def _ensure_draft(budget: Budget) -> None:
    if budget.status not in (DRAFT, REJECTED):
        raise FinanceRuleError(
            "budget_not_editable",
            "Yalnız taslak bütçe değiştirilebilir; yürürlükteki bütçe için revizyon açın.",
            conflict=True,
        )


async def update(
    session: AsyncSession,
    budget: Budget,
    *,
    name: str | None,
    income_target: Decimal | None,
    lines: dict[uuid.UUID, list[Decimal]] | None,
) -> None:
    _ensure_draft(budget)
    if name is not None:
        budget.name = _check_name(name)
    _check_target(income_target)
    budget.income_target = income_target
    if lines is not None:
        errors: dict[str, str] = {}
        if any(len(m) != MONTHS for m in lines.values()):
            errors["lines"] = "Her kategori için 12 aylık tutar gönderin."
        elif any(
            a < ZERO or a != round_money(a) or a > AMOUNT_MAX for m in lines.values() for a in m
        ):
            errors["lines"] = "Tutarlar sıfır ya da daha büyük olmalı (en fazla 2 ondalık)."
        elif lines:
            found = await session.scalar(
                select(func.count()).where(ExpenseCategory.id.in_(list(lines)))
            )
            if (found or 0) != len(lines):
                errors["lines"] = "Gider kategorilerinden biri bulunamadı."
        if errors:
            raise FormFieldError(errors)
        await _replace_lines(session, budget, lines)
    budget.status = DRAFT
    await session.flush()


async def revise(session: AsyncSession, budget: Budget, actor: Person) -> Budget:
    """Yürürlükteki bütçenin yeni sürümü (taslak, satırlar kopyalanır)."""
    if budget.status != ACTIVE:
        raise FinanceRuleError(
            "budget_not_active", "Yalnız yürürlükteki bütçe revize edilebilir.", conflict=True
        )
    open_draft = await session.scalar(
        select(func.count()).where(
            Budget.fiscal_year == budget.fiscal_year, Budget.status.in_((DRAFT, PENDING))
        )
    )
    if open_draft:
        raise FinanceRuleError(
            "budget_draft_exists",
            f"{budget.fiscal_year} için taslak ya da onay bekleyen bir bütçe sürümü zaten var.",
            conflict=True,
        )
    copy = await create(
        session,
        fiscal_year=budget.fiscal_year,
        name=budget.name,
        income_target=budget.income_target,
        budget_plan_id=None,
        actor=actor,
    )
    await _replace_lines(session, copy, await lines_of(session, budget.id))
    return copy


async def _activate(session: AsyncSession, budget: Budget, now: datetime) -> None:
    for old in await session.scalars(
        select(Budget).where(
            Budget.fiscal_year == budget.fiscal_year,
            Budget.status == ACTIVE,
            Budget.id != budget.id,
        )
    ):
        old.status = SUPERSEDED
    await session.flush()
    budget.status = ACTIVE
    budget.activated_at = now
    await session.flush()


@dataclass(frozen=True, slots=True)
class Submitted:
    budget: Budget
    request: ApprovalRequest | None


async def submit(
    session: AsyncSession, site: Site, budget: Budget, *, actor: Person, today: date, now: datetime
) -> Submitted:
    _ensure_draft(budget)
    total = (await totals(session, [budget.id])).get(budget.id, ZERO)
    if total <= ZERO:
        raise FinanceRuleError(
            "budget_empty", "Satırı olmayan bütçe onaya gönderilemez.", conflict=True
        )
    if (
        await approvals_svc.match_rule(
            session, ActionType.BUDGET, amount=total, source="budget", today=today
        )
        is None
    ):
        await _activate(session, budget, now)
        return Submitted(budget, None)
    budget.status = PENDING
    await session.flush()
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.BUDGET,
            title=f"{budget.fiscal_year} bütçesi — sürüm {budget.version}",
            summary={
                "fiscal_year": budget.fiscal_year,
                "version": budget.version,
                "total": str(total),
                "income_target": str(budget.income_target) if budget.income_target else None,
            },
            payload={"kind": "budget", "budget_id": str(budget.id)},
            amount=total,
            source="budget",
            subject_type="budgets",
            subject_id=budget.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    budget.approval_request_id = request.id
    await session.flush()
    return Submitted(budget, request)


# --- İşletme projesi kesinleştirme onayı (D-16) -----------------------------------------


async def request_plan_finalize(
    session: AsyncSession,
    site: Site,
    plan: BudgetPlan,
    *,
    actor: Person,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    """ "Bütçe" onay kuralı varsa kesinleştirme onaya gider (None: kural yok, doğrudan)."""
    from site_yonetim.domain.charging.budget import check_finalize

    check_finalize(BudgetStatus(plan.status), plan.objection_deadline, today)
    if plan.finalize_request_id is not None:
        request = await session.get(ApprovalRequest, plan.finalize_request_id)
        if request is not None and request.status in ("pending", "revision_requested"):
            raise FinanceRuleError(
                "finalize_pending",
                "Bu işletme projesinin kesinleştirmesi zaten onay bekliyor.",
                conflict=True,
            )
    total = await session.scalar(
        select(func.coalesce(func.sum(BudgetItem.annual_amount), 0)).where(
            BudgetItem.budget_plan_id == plan.id
        )
    )
    if (
        await approvals_svc.match_rule(
            session, ActionType.BUDGET, amount=total, source="budget_plan", today=today
        )
        is None
    ):
        return None
    request = await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.BUDGET,
            title=f"{plan.fiscal_year} işletme projesi kesinleştirme — {plan.name}",
            summary={"fiscal_year": plan.fiscal_year, "total": str(total)},
            payload={"kind": "budget_plan", "plan_id": str(plan.id)},
            amount=total,
            source="budget_plan",
            subject_type="budget_plans",
            subject_id=plan.id,
        ),
        requester=actor,
        today=today,
        now=now,
    )
    plan.finalize_request_id = request.id
    await session.flush()
    return request


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    from site_yonetim.services import budget as plans_svc
    from site_yonetim.services.charging import BUSINESS_TZ

    payload = request.payload or {}
    decided = request.decided_at or datetime.now(BUSINESS_TZ)
    if payload.get("kind") == "budget_plan":
        plan = await plans_svc.get_plan(session, uuid.UUID(payload["plan_id"]))
        if plan is None:
            raise ExecutionError("İşletme projesi bulunamadı.")
        try:
            await plans_svc.finalize(session, plan, decided.astimezone(BUSINESS_TZ).date())
        except FinanceRuleError as exc:
            raise ExecutionError(exc.message) from exc
        return
    budget = await get(session, uuid.UUID(payload["budget_id"]))
    if budget is None or budget.status != PENDING:
        raise ExecutionError("Bütçe artık onay beklemiyor.")
    await _activate(session, budget, decided)


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    if payload.get("kind") == "budget":
        budget = await get(session, uuid.UUID(payload["budget_id"]))
        if budget is not None and budget.status == PENDING:
            budget.status = REJECTED
            await session.flush()


approvals_svc.register_executor(ActionType.BUDGET, _execute)
approvals_svc.register_closer(ActionType.BUDGET, _close)


# --- Gerçekleşme --------------------------------------------------------------------------


async def actuals(session: AsyncSession, year: int) -> dict[uuid.UUID, list[Decimal]]:
    """Kategori → 12 aylık gerçekleşen gider (§9.4), veritabanında toplanır."""
    month = extract("month", Expense.date)
    rows = await session.execute(
        select(Expense.expense_category_id, month, func.sum(Expense.amount))
        .where(
            extract("year", Expense.date) == year,
            Expense.is_reversed.is_(False),
            Expense.reversal_of_id.is_(None),
        )
        .group_by(Expense.expense_category_id, month)
    )
    out: dict[uuid.UUID, list[Decimal]] = {}
    for category_id, m, total in rows:
        out.setdefault(category_id, [ZERO] * MONTHS)[int(m) - 1] = total
    return out


def usage_percent(actual: Decimal, budget: Decimal) -> Decimal | None:
    if budget <= ZERO:
        return None
    return (actual / budget * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def level(percent: Decimal | None, warn: int, actual: Decimal) -> str:
    """ok · warn (%90 sarı) · over (%100 kırmızı); bütçesiz harcama `over`."""
    if percent is None:
        return "over" if actual > ZERO else "ok"
    if percent >= 100:
        return "over"
    return "warn" if percent >= warn else "ok"


@dataclass(frozen=True, slots=True)
class ActualRow:
    category_id: uuid.UUID
    category_name: str
    month_budget: Decimal
    month_actual: Decimal
    cumulative_budget: Decimal
    cumulative_actual: Decimal
    annual_budget: Decimal
    usage_percent: Decimal | None  # kümülatif gerçekleşen / yıllık bütçe
    deviation_percent: Decimal | None  # kümülatif: (gerçekleşen − bütçe) / bütçe
    level: str


async def report(session: AsyncSession, budget: Budget, month: int) -> list[ActualRow]:
    planned = await lines_of(session, budget.id)
    spent = await actuals(session, budget.fiscal_year)
    warn = int(
        (await finance_params.params_at(session, YearMonth(budget.fiscal_year, month)))[
            "budget_warn_percent"
        ]
    )
    names = {
        c.id: c.name
        for c in await session.scalars(
            select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
        )
    }
    rows: list[ActualRow] = []
    for category_id, name in names.items():
        plan = planned.get(category_id)
        actual = spent.get(category_id)
        if plan is None and actual is None:
            continue
        plan = plan or [ZERO] * MONTHS
        actual = actual or [ZERO] * MONTHS
        cum_budget = sum(plan[:month], ZERO)
        cum_actual = sum(actual[:month], ZERO)
        annual = sum(plan, ZERO)
        usage = usage_percent(cum_actual, annual)
        deviation = (
            ((cum_actual - cum_budget) / cum_budget * 100).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
            if cum_budget > ZERO
            else None
        )
        rows.append(
            ActualRow(
                category_id,
                name,
                plan[month - 1],
                actual[month - 1],
                cum_budget,
                cum_actual,
                annual,
                usage,
                deviation,
                level(usage, warn, cum_actual),
            )
        )
    return rows


async def remaining(
    session: AsyncSession, category_id: uuid.UUID, year: int
) -> tuple[Decimal, Decimal] | None:
    """(yıllık bütçe, bu yıl harcanan) — satın alma bütçe kontrolü için (SAT-01, FIN-17)."""
    budget = await active_for(session, year)
    if budget is None:
        return None
    plan = (await lines_of(session, budget.id)).get(category_id)
    if plan is None:
        return None
    spent = (await actuals(session, year)).get(category_id, [ZERO] * MONTHS)
    return sum(plan, ZERO), sum(spent, ZERO)


async def run_alerts(session: AsyncSession, site: Site, today: date) -> int:
    """Bütçe sapma uyarısı (günlük): eşiğe ilk ulaşan kategori için bir kez bildirim."""
    budget = await active_for(session, today.year)
    if budget is None:
        return 0
    rows = await report(session, budget, today.month)
    warn = int(
        (await finance_params.params_at(session, YearMonth.of(today)))["budget_warn_percent"]
    )
    existing = {
        (a.expense_category_id, a.threshold)
        for a in await session.scalars(
            select(BudgetAlert).where(BudgetAlert.budget_id == budget.id)
        )
    }
    new: list[tuple[ActualRow, int]] = []
    for row in rows:
        if row.usage_percent is None:
            continue
        for threshold in sorted({warn, 100}):
            if row.usage_percent >= threshold and (row.category_id, threshold) not in existing:
                new.append((row, threshold))
    if not new:
        return 0
    users = {
        m.user.id
        for m in await members_svc.list_members(session, site)
        if m.is_active and Permission.FINANCE_BUDGET_MANAGE in ROLE_PERMISSIONS[m.role]
    }
    for row, threshold in new:
        alert = BudgetAlert(
            id=uuid.uuid7(),
            budget_id=budget.id,
            expense_category_id=row.category_id,
            threshold=threshold,
            budget_amount=row.annual_budget,
            actual_amount=row.cumulative_actual,
        )
        session.add(alert)
        await notify_svc.notify(
            session,
            users,
            notify_svc.Message(
                category=NotificationCategory.FINANCIAL,
                kind="budget.threshold",
                title=f"Bütçe %{threshold} eşiği: {row.category_name}",
                body=f"{row.category_name} kategorisinde {budget.fiscal_year} harcaması "
                f"{format_money_tr(row.cumulative_actual)}; yıllık bütçe "
                f"{format_money_tr(row.annual_budget)} (%{row.usage_percent}).",
                link_entity="budgets",
                link_id=budget.id,
            ),
            dedupe_key=f"budget-alert:{budget.id}:{row.category_id}:{threshold}",
        )
    await session.flush()
    return len(new)
