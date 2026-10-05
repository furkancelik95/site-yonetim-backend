"""Tekrarlanan gider — frontend servis isteği 07. Açık site kapsamında; transaction'ı çağıran
yönetir.

Her ay aynı gelen gider bir kez tanımlanır; gece işi (`cli run-recurring-expenses`, günde bir)
`day_of_month` gelen etkin tanımlar için **normal gider kaydı** yazar (`expenses.create`, belge
yok). Takvim kuralları otomatik tahakkukla aynı (`domain/charging/schedule.py`):

- Açıldığı (ya da yeniden başlatıldığı) günden önceki gün geriye dönük oluşturulmaz.
- Sunucu o gün çalışmadıysa ay içinde ertesi gün yetişir.
- Ay başına tek sonuç (`recurring_expense_runs`): iş iki kez çalışsa da ikinci gider oluşmaz.
- `auto_pay`: gider "ödendi" olarak, seçilen hesaptan çıkışla yazılır.
- Tanım değişse ya da kaldırılsa da oluşmuş giderler değişmez; tanım silinmez, arşive alınır.

Gider kaydı denetim kaydına "Tekrarlanan gider" aktörüyle düşer.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.cash import VENDOR_MAX, clip
from site_yonetim.domain.charging.schedule import is_due, next_run_on
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.models import ExpenseCategory, RecurringExpense, RecurringExpenseRun
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import cash, expenses

logger = logging.getLogger(__name__)

ACTOR = "Tekrarlanan gider"
DESCRIPTION_MAX = 150  # ay/yıl eki ile gider açıklaması 200'ü aşmasın
SETTLED = ("created", "skipped")


@dataclass(frozen=True, slots=True)
class Definition:
    description: str
    expense_category_id: uuid.UUID
    amount: Decimal
    vendor: str | None
    day_of_month: int
    auto_pay: bool
    cash_account_id: uuid.UUID | None
    is_active: bool


async def _check(session: AsyncSession, d: Definition) -> Definition:
    description = " ".join(d.description.split())
    if not description:
        raise FinanceRuleError("description_required", "Açıklama zorunlu.", field="description")
    if len(description) > DESCRIPTION_MAX:
        raise FinanceRuleError(
            "description_too_long",
            f"Açıklama en fazla {DESCRIPTION_MAX} karakter olabilir.",
            field="description",
        )
    if d.amount <= ZERO or d.amount != round_money(d.amount):
        raise FinanceRuleError("invalid_amount", "Tutar sıfırdan büyük olmalı.", field="amount")
    if not 1 <= d.day_of_month <= 28:
        raise FinanceRuleError(
            "invalid_day",
            "Gün 1–28 arasında olmalı (her ayda olsun diye).",
            field="day_of_month",
        )
    category = await session.scalar(
        select(ExpenseCategory.id).where(ExpenseCategory.id == d.expense_category_id)
    )
    if category is None:
        raise FinanceRuleError("category_not_found", "Kategori seçin.", field="expense_category_id")
    account_id = d.cash_account_id
    if d.auto_pay:
        account = await cash.get_account(session, account_id) if account_id else None
        if account is None or not account.is_active:
            raise FinanceRuleError(
                "cash_account_required", "Otomatik ödeme için hesap seçin.", field="cash_account_id"
            )
    elif account_id is not None and await cash.get_account(session, account_id) is None:
        account_id = None  # ödeme yoksa hesap anlamsız; başka sitenin hesabı da "yok"tur
    return Definition(
        description,
        d.expense_category_id,
        d.amount,
        clip(d.vendor, VENDOR_MAX),
        d.day_of_month,
        d.auto_pay,
        account_id if d.auto_pay else None,
        d.is_active,
    )


async def listing(session: AsyncSession) -> list[RecurringExpense]:
    return list(
        await session.scalars(
            select(RecurringExpense)
            .where(RecurringExpense.removed_at.is_(None))
            .order_by(RecurringExpense.day_of_month, RecurringExpense.description)
        )
    )


async def get(session: AsyncSession, item_id: uuid.UUID) -> RecurringExpense | None:
    found: RecurringExpense | None = await session.scalar(
        select(RecurringExpense).where(
            RecurringExpense.id == item_id, RecurringExpense.removed_at.is_(None)
        )
    )
    return found


async def create(session: AsyncSession, data: Definition, *, today: date) -> RecurringExpense:
    d = await _check(session, data)
    item = RecurringExpense(
        id=uuid.uuid7(),
        description=d.description,
        expense_category_id=d.expense_category_id,
        amount=d.amount,
        vendor=d.vendor,
        day_of_month=d.day_of_month,
        auto_pay=d.auto_pay,
        cash_account_id=d.cash_account_id,
        is_active=d.is_active,
        active_since=today,
    )
    session.add(item)
    await session.flush()
    return item


def current(item: RecurringExpense) -> Definition:
    return Definition(
        item.description,
        item.expense_category_id,
        item.amount,
        item.vendor,
        item.day_of_month,
        item.auto_pay,
        item.cash_account_id,
        item.is_active,
    )


async def update(
    session: AsyncSession, item: RecurringExpense, data: Definition, *, today: date
) -> RecurringExpense:
    d = await _check(session, data)
    if d.is_active and not item.is_active:
        item.active_since = today  # yeniden başlatılınca durdurulduğu aylar geriye dönük yazılmaz
    item.description = d.description
    item.expense_category_id = d.expense_category_id
    item.amount = d.amount
    item.vendor = d.vendor
    item.day_of_month = d.day_of_month
    item.auto_pay = d.auto_pay
    item.cash_account_id = d.cash_account_id
    item.is_active = d.is_active
    await session.flush()
    return item


async def remove(session: AsyncSession, item: RecurringExpense, *, now: datetime) -> None:
    item.is_active = False
    item.removed_at = now
    await session.flush()


# --- Sıradaki çalışma, son gider ----------------------------------------------------


async def runs_this_month(
    session: AsyncSession, today: date
) -> dict[uuid.UUID, RecurringExpenseRun]:
    rows = await session.scalars(
        select(RecurringExpenseRun).where(
            RecurringExpenseRun.year == today.year, RecurringExpenseRun.month == today.month
        )
    )
    return {r.recurring_expense_id: r for r in rows}


async def last_created(session: AsyncSession) -> dict[uuid.UUID, date]:
    rows = await session.execute(
        select(
            RecurringExpenseRun.recurring_expense_id,
            RecurringExpenseRun.year,
            RecurringExpenseRun.month,
        )
        .where(RecurringExpenseRun.status == "created")
        .order_by(RecurringExpenseRun.year, RecurringExpenseRun.month)
    )
    items = {i.id: i.day_of_month for i in await listing(session)}
    return {
        item_id: date(year, month, items[item_id])
        for item_id, year, month in rows
        if item_id in items
    }


def next_run(item: RecurringExpense, today: date, run: RecurringExpenseRun | None) -> date | None:
    if not item.is_active:
        return None
    settled = run is not None and run.status in SETTLED
    return next_run_on(today, item.day_of_month, item.active_since, settled=settled)


# --- Gece işi -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Outcome:
    item_id: uuid.UUID
    status: str  # created · skipped · failed
    message: str | None
    expense_id: uuid.UUID | None = None


async def _attempt(
    session: AsyncSession, item: RecurringExpense, today: date
) -> tuple[str, str | None, uuid.UUID | None]:
    day = date(today.year, today.month, item.day_of_month)
    try:
        async with session.begin_nested():
            created = await expenses.create(
                session,
                expenses.NewExpense(
                    expense_category_id=item.expense_category_id,
                    description=f"{item.description} — {day:%m/%Y}",
                    amount=item.amount,
                    day=day,
                    vendor=item.vendor,
                    paid=item.auto_pay,
                    paid_on=day if item.auto_pay else None,
                    cash_account_id=item.cash_account_id,
                ),
                today=today,
                created_by=ACTOR,
            )
    except FinanceRuleError as exc:
        return "skipped", exc.message, None
    except Exception:
        logger.exception("Tekrarlanan gider oluşturulamadı: %s", item.id)
        return "failed", "Beklenmeyen hata; yarın yeniden denenecek.", None
    return "created", None, created.expense.id


async def run_site(session: AsyncSession, *, today: date, now: datetime) -> list[Outcome]:
    runs = await runs_this_month(session, today)
    outcomes = []
    for item in await listing(session):
        if not item.is_active or not is_due(today, item.day_of_month, item.active_since):
            continue
        existing = runs.get(item.id)
        if existing is not None and existing.status in SETTLED:
            continue
        status, message, expense_id = await _attempt(session, item, today)
        row = existing or RecurringExpenseRun(
            recurring_expense_id=item.id, year=today.year, month=today.month
        )
        row.status, row.message, row.expense_id, row.ran_at = status, message, expense_id, now
        session.add(row)
        await session.flush()
        outcomes.append(Outcome(item.id, status, message, expense_id))
    return outcomes


async def run_all(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[tuple[uuid.UUID, Outcome]]:
    """Etkin tanımı olan her site için, site başına ayrı transaction."""
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(
                await session.scalars(
                    select(RecurringExpense.site_id)
                    .where(RecurringExpense.is_active, RecurringExpense.removed_at.is_(None))
                    .distinct()
                    .order_by(RecurringExpense.site_id)
                )
            )
    set_actor(Actor(None, ACTOR, None))
    results: list[tuple[uuid.UUID, Outcome]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                outcomes = await run_site(session, today=today, now=now)
        results.extend((site_id, o) for o in outcomes)
    return results
