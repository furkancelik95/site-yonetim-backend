"""Gider ve belge uçları — docs/06 §2.7, docs/04 §9, docs/09 §3.

- Kayıt `multipart/form-data`: alanlar + isteğe bağlı `document` (PDF/JPG/PNG/WEBP, ≤ 10 MB,
  içerik imzası doğrulanır). "Ödendi" ise kasa hesabı zorunlu; kasadan çıkış aynı transaction'da.
- Silme/düzenleme yok: sonradan ödeme ve ters kayıt (`reverse`).
- Belge yalnız yetkili uçtan, `Content-Disposition: inline` ve temiz adla indirilir.
"""

import datetime as dt
import uuid
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SettingsDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.finance_common import (
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import Expense
from site_yonetim.services import expenses as svc
from site_yonetim.services.files import FileStore

router = APIRouter(prefix="/sites/{slug}", tags=["gider"])
ExpensesRead = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_READ))]
ExpensesManage = Annotated[SiteContext, Depends(require_permission(Permission.EXPENSES_MANAGE))]
Paging = Annotated[PageParams, Depends()]


def get_file_store(settings: SettingsDep) -> FileStore:
    return FileStore(settings.file_storage_root)


StoreDep = Annotated[FileStore, Depends(get_file_store)]


class ExpenseOut(BaseModel):
    id: uuid.UUID
    expense_category_id: uuid.UUID
    description: str = Field(description="sakinler bu metni görür")
    amount: Money = Field(description="düzeltme kaydında eksi")
    date: dt.date = Field(description="belge tarihi")
    vendor: str | None
    document_number: str | None
    note: str | None
    stored_file_id: uuid.UUID | None = Field(description="belge: GET …/files/{id}")
    paid_on: dt.date | None = Field(description="null = kaydedildi ama parası çıkmadı")
    cash_account_id: uuid.UUID | None
    is_paid: bool
    is_reversed: bool = Field(description="ters kaydı atılmış — toplama girmez")
    is_reversal: bool = Field(description="düzeltme kaydı — toplama girmez")
    reversal_of_id: uuid.UUID | None
    created_by_name: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, e: Expense) -> ExpenseOut:
        return cls(
            id=e.id,
            expense_category_id=e.expense_category_id,
            description=e.description,
            amount=e.amount,
            date=e.date,
            vendor=e.vendor,
            document_number=e.document_number,
            note=e.note,
            stored_file_id=e.stored_file_id,
            paid_on=e.paid_on,
            cash_account_id=e.cash_account_id,
            is_paid=e.paid_on is not None,
            is_reversed=e.is_reversed,
            is_reversal=e.reversal_of_id is not None,
            reversal_of_id=e.reversal_of_id,
            created_by_name=e.created_by_name,
            created_at=e.created_at,
        )


class CategoryTotalOut(BaseModel):
    category_id: uuid.UUID
    name: str
    total: Money
    count: int


class ExpenseSummaryOut(BaseModel):
    total: Money = Field(description="gerçekleşen gider (geri alınan ve düzeltmeler hariç)")
    unpaid_total: Money
    unpaid_count: int
    by_category: list[CategoryTotalOut]


class ExpensePage(Page[ExpenseOut]):
    summary: ExpenseSummaryOut


@router.get("/expenses", summary="Giderler (yeni üstte) ve özet")
async def list_expenses(
    ctx: ExpensesRead,
    paging: Paging,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    category_id: uuid.UUID | None = None,
    paid: Literal["all", "paid", "unpaid"] = "all",
) -> ExpensePage:
    query = svc.list_query(year=year, category_id=category_id, paid=paid)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    summary = await svc.summary(ctx.session, year=year, category_id=category_id)
    return ExpensePage(
        items=[ExpenseOut.of(e) for e in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
        summary=ExpenseSummaryOut(
            total=summary.total,
            unpaid_total=summary.unpaid_total,
            unpaid_count=summary.unpaid_count,
            by_category=[
                CategoryTotalOut(
                    category_id=c.category_id, name=c.name, total=c.total, count=c.count
                )
                for c in summary.by_category
            ],
        ),
    )


async def _expense(ctx: SiteContext, expense_id: uuid.UUID, *, lock: bool = False) -> Expense:
    expense = await svc.get(ctx.session, expense_id, lock=lock)
    if expense is None:
        raise NotFoundError("Gider bulunamadı.")
    return expense


@router.get("/expenses/{expense_id}", summary="Gider")
async def get_expense(expense_id: uuid.UUID, ctx: ExpensesRead) -> ExpenseOut:
    return ExpenseOut.of(await _expense(ctx, expense_id))


@router.post(
    "/expenses",
    status_code=status.HTTP_201_CREATED,
    summary="Gider kaydet (multipart, belge isteğe bağlı)",
    response_model=Written[ExpenseOut],
)
async def create_expense(
    ctx: ExpensesManage,
    expense_category_id: Annotated[uuid.UUID, Form()],
    description: Annotated[str, Form(min_length=3, max_length=200)],
    amount: Annotated[Money, Form(description="metin, `1234.56`")],
    date: Annotated[dt.date, Form(description="belge tarihi")],
    current: CurrentUserDep,
    idem: IdempotencyDep,
    store: StoreDep,
    today: TodayDep,
    now: NowDep,
    vendor: Annotated[str | None, Form(max_length=200)] = None,
    document_number: Annotated[str | None, Form(max_length=200)] = None,
    note: Annotated[str | None, Form(max_length=1000)] = None,
    paid: Annotated[bool, Form(description="ödendi olarak kaydet")] = False,
    paid_on: Annotated[dt.date | None, Form()] = None,
    cash_account_id: Annotated[uuid.UUID | None, Form()] = None,
    document: Annotated[
        UploadFile | None, File(description="PDF, JPG, PNG, WEBP · ≤ 10 MB")
    ] = None,
) -> Written[ExpenseOut] | JSONResponse:
    written: str | None = None
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        upload = None
        if document is not None and (document.filename or document.size):
            upload = svc.Document(document.filename, await document.read(MAX_FILE_BYTES + 1))
        created = await svc.create(
            ctx.session,
            svc.NewExpense(
                expense_category_id=expense_category_id,
                description=description,
                amount=amount,
                day=date,
                vendor=vendor,
                document_number=document_number,
                note=note,
                paid=paid,
                paid_on=paid_on or None,
                cash_account_id=cash_account_id or None,
            ),
            today=today,
            created_by=current.user.full_name,
            document=upload,
            store=store,
        )
        written = created.written_path
        expense = created.expense
        state = "ödendi olarak " if expense.paid_on else ""
        result = Written(
            data=ExpenseOut.of(expense),
            message=f"{format_money_tr(expense.amount)} gider {state}kaydedildi.",
        )
        await commit_with_key(ctx.session, idem, status.HTTP_201_CREATED, result)
    except FinanceRuleError as exc:
        if written:
            store.delete(written)  # reddedilen kayıttan diskte iz kalmaz
        raise finance_error(exc) from exc
    except BaseException:
        if written:
            store.delete(written)
        raise
    return result


class PayIn(BaseModel):
    cash_account_id: uuid.UUID
    paid_on: dt.date


@router.post(
    "/expenses/{expense_id}/pay", summary="Sonradan öde", response_model=Written[ExpenseOut]
)
async def pay_expense(
    expense_id: uuid.UUID,
    ctx: ExpensesManage,
    body: PayIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ExpenseOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        expense = await _expense(ctx, expense_id, lock=True)
        await svc.pay(
            ctx.session, expense, cash_account_id=body.cash_account_id, paid_on=body.paid_on,
            today=today, created_by=current.user.full_name,
        )  # fmt: skip
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    result = Written(
        data=ExpenseOut.of(expense),
        message=f"Gider ödendi; {format_money_tr(expense.amount)} kasadan düştü.",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result


class ReverseIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post(
    "/expenses/{expense_id}/reverse",
    summary="Geri al (eksi tutarlı düzeltme kaydı)",
    response_model=Written[ExpenseOut],
)
async def reverse_expense(
    expense_id: uuid.UUID,
    ctx: ExpensesManage,
    body: ReverseIn,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ExpenseOut] | JSONResponse:
    try:
        if (stored := await idem.replay(ctx.session, now)) is not None:
            return replayed(stored.status_code, stored.body)
        expense = await _expense(ctx, expense_id, lock=True)
        correction = await svc.reverse(
            ctx.session, expense, reason=body.reason, today=today, created_by=current.user.full_name
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    returned = " ödenen tutar kasaya geri girdi." if expense.paid_on else ""
    result = Written(
        data=ExpenseOut.of(correction),
        message=f"Gider geri alındı (düzeltme kaydı oluşturuldu).{returned}",
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, result)
    return result


# --- Belge indirme -----------------------------------------------------------------


@router.get(
    "/files/{file_id}",
    summary="Belgeyi indir",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/*": {}}}},
)
async def download_file(file_id: uuid.UUID, ctx: ExpensesRead, store: StoreDep) -> Response:
    """Belge bağlı olduğu kaydın iznine göre (bugün yalnız gider: `expenses.read`)."""
    row = await svc.stored_file(ctx.session, file_id)
    if row is None or not await svc.is_expense_document(ctx.session, file_id):
        raise NotFoundError("Belge bulunamadı.")
    try:
        data = store.read(row.storage_path)
    except (FileNotFoundError, ValueError) as exc:
        raise NotFoundError("Belge bulunamadı.") from exc
    ascii_name = (
        row.file_name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    )
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(row.file_name)}"
    return Response(
        data,
        media_type=row.content_type,
        headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store"},
    )
