"""Gider servisleri — docs/04 §9. Açık site kapsamında; commit çağırana ait.

Gider ile ödeme ayrıdır: fatura gelince gider yazılır, parası çıkınca ödendi işaretlenir ve
**aynı transaction'da** kasadan çıkış hareketi yazılır. Gider silinmez; düzeltme eksi tutarlı
kayıttır (orijinal ödenmişse para kasaya geri girer).
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, Select, and_, case, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.cash import (
    DOCUMENT_MAX,
    NOTE_MAX,
    VENDOR_MAX,
    CashSource,
    ExpenseState,
    check_expense_amount,
    check_expense_reversible,
    check_not_future,
    check_paid_on,
    check_payable,
    clean_text,
    clip,
    closed_period_error,
    expense_movement_text,
    missing_cash_account,
)
from site_yonetim.domain.files import check_document
from site_yonetim.domain.finance import FinanceRuleError, PeriodStatus
from site_yonetim.models import Expense, ExpenseCategory, Period, StoredFile
from site_yonetim.services import cash
from site_yonetim.services.files import FileStore


async def _period(session: AsyncSession, day: date) -> Period:
    """Belge tarihinin dönemi; yoksa açılır, kapalıysa reddedilir (docs/04 §9.1)."""
    row: Period | None = await session.scalar(
        select(Period).where(Period.year == day.year, Period.month == day.month)
    )
    if row is None:
        row = Period(year=day.year, month=day.month)
        session.add(row)
        await session.flush()
    if row.status == PeriodStatus.CLOSED.value:
        raise closed_period_error(day)
    return row


@dataclass(frozen=True, slots=True)
class Document:
    name: str | None
    data: bytes


@dataclass(frozen=True, slots=True)
class NewExpense:
    expense_category_id: uuid.UUID
    description: str
    amount: Decimal
    day: date
    vendor: str | None = None
    document_number: str | None = None
    note: str | None = None
    paid: bool = False
    paid_on: date | None = None
    cash_account_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class Created:
    expense: Expense
    written_path: str | None  # transaction başarısız olursa çağıran siler


async def create(
    session: AsyncSession,
    data: NewExpense,
    *,
    today: date,
    created_by: str,
    document: Document | None = None,
    store: FileStore | None = None,
) -> Created:
    description = clean_text(data.description, "description")
    check_expense_amount(data.amount)
    check_not_future(data.day, today, tolerance=1)  # bugün + 1 güne kadar tolerans
    if (
        await session.scalar(
            select(ExpenseCategory.id).where(ExpenseCategory.id == data.expense_category_id)
        )
        is None
    ):
        raise FinanceRuleError(
            "category_not_found", "Gider kategorisi bulunamadı.", field="expense_category_id"
        )
    account = None
    paid_on = None
    if data.paid:
        if data.cash_account_id is None:
            raise missing_cash_account()
        account = await cash.active_account(session, data.cash_account_id)
        paid_on = check_paid_on(data.paid_on, data.day, today)
    file_kind = file_name = None
    if document is not None:  # önce doğrula: reddedilen dosya diske hiç yazılmaz
        file_name, file_kind = check_document(document.name, document.data)
    period = await _period(session, data.day)

    stored_file_id = None
    written = None
    if document is not None and file_kind is not None and file_name is not None and store:
        scope = current_scope()
        file_id = uuid.uuid7()
        written, digest = store.write(
            scope.site_id if scope and scope.site_id else uuid.UUID(int=0),
            file_id,
            file_kind,
            document.data,
        )
        session.add(
            StoredFile(
                id=file_id,
                file_name=file_name,
                content_type=file_kind.content_type,
                byte_size=len(document.data),
                storage_path=written,
                sha256=digest,
                uploaded_by_name=created_by,
            )
        )
        await session.flush()
        stored_file_id = file_id

    vendor = clip(data.vendor, VENDOR_MAX)
    expense = Expense(
        id=uuid.uuid7(),
        expense_category_id=data.expense_category_id,
        period_id=period.id,
        description=description,
        amount=data.amount,
        date=data.day,
        vendor=vendor,
        document_number=clip(data.document_number, DOCUMENT_MAX),
        note=clip(data.note, NOTE_MAX),
        stored_file_id=stored_file_id,
        paid_on=paid_on,
        cash_account_id=account.id if account else None,
        created_by_name=created_by,
    )
    session.add(expense)
    await session.flush()
    if account is not None and paid_on is not None:
        await cash.add_movement(
            session, account, day=paid_on, outflow=data.amount,
            description=expense_movement_text(description, vendor), source=CashSource.EXPENSE,
            source_id=expense.id, reference=expense.document_number, created_by=created_by,
        )  # fmt: skip
    return Created(expense, written)


async def get(
    session: AsyncSession, expense_id: uuid.UUID, *, lock: bool = False
) -> Expense | None:
    query = select(Expense).where(Expense.id == expense_id)
    if lock:
        query = query.with_for_update()
    expense: Expense | None = await session.scalar(query)
    return expense


def _state(expense: Expense) -> ExpenseState:
    return ExpenseState(
        is_reversed=expense.is_reversed,
        is_reversal=expense.reversal_of_id is not None,
        is_paid=expense.paid_on is not None,
    )


async def pay(
    session: AsyncSession,
    expense: Expense,
    *,
    cash_account_id: uuid.UUID,
    paid_on: date,
    today: date,
    created_by: str,
) -> Expense:
    """Sonradan ödeme (docs/04 §9.2): ödeme bilgisi + kasadan çıkış, tek transaction."""
    check_payable(_state(expense))
    check_paid_on(paid_on, expense.date, today)
    account = await cash.active_account(session, cash_account_id)
    expense.paid_on = paid_on
    expense.cash_account_id = account.id
    await session.flush()
    await cash.add_movement(
        session, account, day=paid_on, outflow=expense.amount,
        description=expense_movement_text(expense.description, expense.vendor),
        source=CashSource.EXPENSE, source_id=expense.id, reference=expense.document_number,
        created_by=created_by,
    )  # fmt: skip
    return expense


async def reverse(
    session: AsyncSession, expense: Expense, *, reason: str, today: date, created_by: str
) -> Expense:
    """Eksi tutarlı düzeltme kaydı; orijinal `is_reversed`. Ödenmişse para kasaya döner (§9.3)."""
    text_reason = check_expense_reversible(_state(expense), reason)
    account = None
    if expense.paid_on is not None and expense.cash_account_id is not None:
        account = await cash.active_account(session, expense.cash_account_id)
    correction = Expense(
        id=uuid.uuid7(),
        expense_category_id=expense.expense_category_id,
        period_id=expense.period_id,
        description=f"DÜZELTME — {expense.description}"[:200],
        amount=-expense.amount,
        date=today,
        vendor=expense.vendor,
        document_number=expense.document_number,
        note=text_reason,
        reversal_of_id=expense.id,
        created_by_name=created_by,
    )
    session.add(correction)
    expense.is_reversed = True
    await session.flush()
    if account is not None:
        await cash.add_movement(
            session, account, day=today, inflow=expense.amount,
            description=f"Gider düzeltmesi: {expense.description}", source=CashSource.EXPENSE,
            source_id=correction.id, reference=text_reason, created_by=created_by,
        )  # fmt: skip
    return correction


# --- Liste ve toplamlar (§9.4) -------------------------------------------------------


def realized() -> ColumnElement[bool]:
    """Gerçekleşen gider: geri alınmamış ve düzeltme kaydı olmayan."""
    return and_(Expense.is_reversed.is_(False), Expense.reversal_of_id.is_(None))


def _filters(year: int | None, category_id: uuid.UUID | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if year is not None:
        conditions.append(extract("year", Expense.date) == year)
    if category_id is not None:
        conditions.append(Expense.expense_category_id == category_id)
    return conditions


def list_query(*, year: int | None, category_id: uuid.UUID | None, paid: str) -> Select[Expense]:
    query = select(Expense).where(*_filters(year, category_id))
    if paid == "paid":
        query = query.where(Expense.paid_on.is_not(None))
    elif paid == "unpaid":
        query = query.where(Expense.paid_on.is_(None), realized())
    return query.order_by(Expense.date.desc(), Expense.created_at.desc(), Expense.id.desc())


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category_id: uuid.UUID
    name: str
    total: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class Summary:
    total: Decimal
    unpaid_total: Decimal
    unpaid_count: int
    by_category: list[CategoryTotal]


async def summary(
    session: AsyncSession, *, year: int | None, category_id: uuid.UUID | None
) -> Summary:
    """Toplamlar veritabanında; geri alınan ve düzeltme kayıtları toplama girmez."""
    conditions = [*_filters(year, category_id), realized()]
    unpaid = Expense.paid_on.is_(None)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Expense.amount), 0),
                func.coalesce(func.sum(case((unpaid, Expense.amount), else_=0)), 0),
                func.count(case((unpaid, 1))),
            ).where(*conditions)
        )
    ).one()
    categories = await session.execute(
        select(ExpenseCategory.id, ExpenseCategory.name, func.sum(Expense.amount), func.count())
        .join(
            ExpenseCategory,
            and_(
                ExpenseCategory.id == Expense.expense_category_id,
                ExpenseCategory.site_id == Expense.site_id,
            ),
        )
        .where(*conditions)
        .group_by(ExpenseCategory.id, ExpenseCategory.name)
        .order_by(func.sum(Expense.amount).desc())
    )
    cent = Decimal("0.01")
    return Summary(
        total=Decimal(row[0]).quantize(cent),
        unpaid_total=Decimal(row[1]).quantize(cent),
        unpaid_count=row[2],
        by_category=[
            CategoryTotal(cid, name, total, count) for cid, name, total, count in categories
        ],
    )


async def stored_file(session: AsyncSession, file_id: uuid.UUID) -> StoredFile | None:
    row: StoredFile | None = await session.scalar(
        select(StoredFile).where(StoredFile.id == file_id)
    )
    return row


async def is_expense_document(session: AsyncSession, file_id: uuid.UUID) -> bool:
    return (
        await session.scalar(select(Expense.id).where(Expense.stored_file_id == file_id).limit(1))
    ) is not None
