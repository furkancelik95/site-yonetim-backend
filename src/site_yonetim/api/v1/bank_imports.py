"""Banka hareketi aktarımı ve eşleştirme — frontend servis isteği 06 (backend issue #35).

`finance.payment.record` + `finance.cash.manage`. Önizleme hiçbir tahsilat yazmaz; onay seçilen
satırları mevcut tahsilat servisiyle tek transaction'da işler. Aktarım denetim kaydına yazılır.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Written
from site_yonetim.api.v1.finance_common import (
    IdempotencyDep,
    commit_with_key,
    finance_error,
    replayed,
)
from site_yonetim.core.errors import ApiError, ForbiddenError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.bank import BankFileError, MatchStatus
from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import BankImport, LedgerAccount
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import accounts as account_svc
from site_yonetim.services import bank_imports as svc

router = APIRouter(prefix="/sites/{slug}/bank-imports", tags=["banka aktarımı"])
PaymentRecord = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]


def _cash_manager(ctx: SiteContext) -> None:
    if not ctx.access.can(Permission.FINANCE_CASH_MANAGE):
        raise ForbiddenError


def _not_found() -> ApiError:
    return ApiError(
        HTTPStatus.NOT_FOUND,
        "not_found",
        "Aktarım bulunamadı ya da süresi doldu; dosyayı yeniden yükleyin.",
    )


class SuggestionOut(BaseModel):
    ledger_account_id: uuid.UUID
    reference_code: str
    unit_name: str
    person_name: str | None = Field(description="people.read izni yoksa null")
    balance: Money
    confidence: Literal["high", "medium"]
    reason: str


class RowOut(BaseModel):
    row_number: int
    date: dt.date
    description: str
    amount: Money
    direction: Literal["in", "out"]
    bank_reference: str | None
    status: MatchStatus = Field(
        description="matched (işaretli gelir) · suggested · unmatched · ignored (çıkış) · duplicate"
    )
    suggestion: SuggestionOut | None


class PreviewOut(BaseModel):
    import_id: uuid.UUID
    file_name: str
    cash_account_id: uuid.UUID
    expires_at: dt.datetime
    row_count: int
    matched_count: int
    suggested_count: int
    unmatched_count: int
    ignored_count: int
    duplicate_count: int
    total_in: Money = Field(description="giriş hareketlerinin toplamı")
    rows: list[RowOut]


async def _preview_out(ctx: SiteContext, bank_import: BankImport) -> PreviewOut:
    ids = {
        uuid.UUID(str(r["suggestion"]["ledger_account_id"]))  # type: ignore[index]
        for r in bank_import.rows
        if r["suggestion"]
    }
    accounts = {
        row.account.id: row
        for row in (
            account_svc.to_row(tuple(values))
            for values in await ctx.session.execute(
                account_svc.accounts_query().where(LedgerAccount.id.in_(ids))
            )
        )
    } if ids else {}  # fmt: skip
    names = ctx.access.can(Permission.PEOPLE_READ)
    rows = []
    counts = dict.fromkeys(MatchStatus, 0)
    total_in = ZERO
    for r in bank_import.rows:
        status_ = MatchStatus(str(r["status"]))
        counts[status_] += 1
        if r["direction"] == "in":
            total_in += Decimal(str(r["amount"]))
        suggestion = None
        if r["suggestion"]:
            hint: dict[str, str] = r["suggestion"]  # type: ignore[assignment]
            account = accounts.get(uuid.UUID(hint["ledger_account_id"]))
            if account is not None:
                suggestion = SuggestionOut(
                    ledger_account_id=account.account.id,
                    reference_code=account.account.reference_code,
                    unit_name=account.unit_name,
                    person_name=account.person_name if names else None,
                    balance=account.balance,
                    confidence=hint["confidence"],  # type: ignore[arg-type]
                    reason=hint["reason"],
                )
        rows.append(
            RowOut(
                row_number=int(str(r["row_number"])),
                date=dt.date.fromisoformat(str(r["date"])),
                description=str(r["description"]),
                amount=Decimal(str(r["amount"])),
                direction=r["direction"],  # type: ignore[arg-type]
                bank_reference=str(r["bank_reference"]) if r["bank_reference"] else None,
                status=status_,
                suggestion=suggestion,
            )
        )
    return PreviewOut(
        import_id=bank_import.id,
        file_name=bank_import.file_name,
        cash_account_id=bank_import.cash_account_id,
        expires_at=bank_import.expires_at,
        row_count=len(rows),
        matched_count=counts[MatchStatus.MATCHED],
        suggested_count=counts[MatchStatus.SUGGESTED],
        unmatched_count=counts[MatchStatus.UNMATCHED],
        ignored_count=counts[MatchStatus.IGNORED],
        duplicate_count=counts[MatchStatus.DUPLICATE],
        total_in=total_in,
        rows=rows,
    )


@router.post("", summary="Banka ekstresini yükle — önizleme (hiçbir tahsilat yazılmaz)")
async def upload(
    ctx: PaymentRecord,
    current: CurrentUserDep,
    now: NowDep,
    file: Annotated[UploadFile, File(description=".xlsx, .xls ya da .csv; en fazla 5 MB")],
    cash_account_id: Annotated[
        uuid.UUID | None, Form(description="paranın girdiği banka hesabı")
    ] = None,
) -> PreviewOut:
    _cash_manager(ctx)
    data = await file.read(svc.MAX_UPLOAD_BYTES + 1)
    try:
        rows = await run_in_threadpool(svc.read_rows, file.filename, data)
    except BankFileError as exc:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, {"file": exc.message}
        ) from exc
    try:
        bank_import = await svc.preview(
            ctx.session,
            cash_account_id=cash_account_id,
            file_name=file.filename or "ekstre",
            rows=rows,
            user_id=current.user.id,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    out = await _preview_out(ctx, bank_import)
    await ctx.session.commit()
    return out


class SelectionIn(BaseModel):
    row_number: int = Field(ge=1)
    ledger_account_id: uuid.UUID


class ConfirmIn(BaseModel):
    rows: list[SelectionIn] = Field(min_length=1, max_length=5000)


class SkippedOut(BaseModel):
    row_number: int
    code: str
    message: str


class ConfirmOut(BaseModel):
    created_payments: int
    total_amount: Money
    skipped: list[SkippedOut]


@router.post(
    "/{import_id}/confirm",
    summary="Seçilen satırları tahsilat olarak işle",
    response_model=Written[ConfirmOut],
)
async def confirm(
    import_id: uuid.UUID,
    body: ConfirmIn,
    ctx: PaymentRecord,
    current: CurrentUserDep,
    idem: IdempotencyDep,
    today: TodayDep,
    now: NowDep,
) -> Written[ConfirmOut] | JSONResponse:
    """Tek transaction; işlenemeyen satır `skipped`'a düşer. İkinci onay 409 `already_confirmed`."""
    _cash_manager(ctx)
    if (stored := await idem.replay(ctx.session, now)) is not None:
        return replayed(stored.status_code, stored.body)
    bank_import = await svc.pending(
        ctx.session, import_id, user_id=current.user.id, now=now, lock=True
    )
    if bank_import is None:
        raise _not_found()
    try:
        outcome = await svc.confirm(
            ctx.session,
            bank_import,
            [svc.Selection(r.row_number, r.ledger_account_id) for r in body.rows],
            today=today,
            recorded_by=current.user.full_name,
            now=now,
        )
    except FinanceRuleError as exc:
        raise finance_error(exc) from exc
    record(ctx.session, AuditAction.IMPORT, "bank_imports", bank_import.id, {
        "file_name": bank_import.file_name,
        "row_count": bank_import.row_count,
        "created_payments": outcome.created_payments,
        "total_amount": outcome.total_amount,
    })  # fmt: skip
    written = Written(
        data=ConfirmOut(
            created_payments=outcome.created_payments,
            total_amount=outcome.total_amount,
            skipped=[
                SkippedOut(row_number=s.row_number, code=s.code, message=s.message)
                for s in outcome.skipped
            ],
        ),
        message=(
            f"{outcome.created_payments} banka hareketi tahsilat olarak işlendi "
            f"({format_money_tr(outcome.total_amount)})."
        ),
    )
    await commit_with_key(ctx.session, idem, status.HTTP_200_OK, written)
    return written
