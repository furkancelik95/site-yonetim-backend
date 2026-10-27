"""Aktarım geçmişi, araç ve açılış bakiyesi aktarımı, geri alma — servis isteği 25 (YAP-06).

Yükleme **hiçbir kayıt oluşturmaz**: önizleme döner, dosya geçici depoya yazılır (6 saat).
Onay aynı dosyayı yeniden doğrular, tek transaction ile yazar; aynı önizleme iki kez
onaylanamaz. Açılış bakiyesi para yazar: eski sistem toplamıyla mutabakat zorunlu, satır başına
devir bakiye kuralı (hesap başına bir kez) geçerlidir. Geri alma 7 gün içinde, sonradan hareket
girilmemişse.
"""

import datetime as dt
import uuid
from functools import cache
from http import HTTPStatus
from io import BytesIO
from typing import Annotated, Literal

import openpyxl
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from openpyxl.styles import Font
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.imports import XLSX_MEDIA_TYPE, StoreDep
from site_yonetim.core.errors import ApiError, ForbiddenError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.imports.numbers import CellValue
from site_yonetim.domain.imports.unit_validator import ImportFileError
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import ImportBatch
from site_yonetim.models.audit import AuditAction, bulk_operation, record
from site_yonetim.services import import_history as svc
from site_yonetim.services.imports import MAX_UPLOAD_BYTES

router = APIRouter(prefix="/sites/{slug}/imports", tags=["excel aktarımı"])
Paging = Annotated[PageParams, Depends()]
UnitsManage = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
OpeningManage = Annotated[
    SiteContext, Depends(require_permission(Permission.FINANCE_PAYMENT_RECORD))
]
PERMISSION_OF = {
    "units": Permission.UNITS_MANAGE,
    "vehicles": Permission.UNITS_MANAGE,
    "opening_balances": Permission.FINANCE_PAYMENT_RECORD,
}
NOT_FOUND = "Önizleme bulunamadı ya da süresi doldu; dosyayı yeniden yükleyin."


def _file_error(exc: ImportFileError) -> ApiError:
    code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code == "file_too_large" else 422
    return ApiError(code, exc.code, exc.message, {"file": exc.message})


def _rule_error(exc: svc.ImportRuleError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.fields)


# --- Geçmiş ---------------------------------------------------------------------------------


class ImportBatchOut(BaseModel):
    id: uuid.UUID
    type: Literal["units", "vehicles", "opening_balances"]
    file_name: str
    status: Literal["imported", "rolled_back"]
    row_count: int
    created_count: int
    total_amount: Money | None
    created_by: str | None
    created_at: dt.datetime
    rollback_until: dt.datetime | None
    rollback_blocked_by: str | None = Field(description="geri almayı engelleyen sonraki kayıt")
    can_rollback: bool


@router.get("", summary="Aktarım geçmişi (yeni üstte)")
async def list_imports(ctx: SiteContextDep, paging: Paging, now: NowDep) -> Page[ImportBatchOut]:
    allowed = [t for t, p in PERMISSION_OF.items() if ctx.access.can(p)]
    if not allowed:
        raise ForbiddenError
    query = (
        select(ImportBatch)
        .where(ImportBatch.type.in_(allowed), ImportBatch.status != "previewed")
        .order_by(ImportBatch.imported_at.desc().nulls_last(), ImportBatch.id.desc())
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    items = []
    for b in rows:
        rollbackable = svc.can_rollback(b, now)
        blocked = await svc.blocked_by(ctx.session, b) if rollbackable else None
        items.append(
            ImportBatchOut(
                id=b.id,
                type=b.type,  # type: ignore[arg-type]
                file_name=b.file_name,
                status=b.status,  # type: ignore[arg-type]
                row_count=b.row_count,
                created_count=b.created_count,
                total_amount=b.total_amount,
                created_by=b.created_by_name,
                created_at=b.imported_at or b.created_at,
                rollback_until=b.rollback_until,
                rollback_blocked_by=blocked,
                can_rollback=rollbackable and blocked is None,
            )
        )
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


# --- Şablonlar ------------------------------------------------------------------------------

_TEMPLATES = {
    "vehicles": (
        "Araçlar",
        ["Bölüm", "Plaka", "Marka", "Renk"],
        [["A-1", "34 ABC 123", "Renault Clio", "Beyaz"], ["B-12", "06 XY 4567", "", ""]],
        "arac-aktarim-sablonu.xlsx",
    ),
    "opening-balances": (
        "Açılış bakiyeleri",
        ["Referans", "Tutar", "Tarih"],
        [["A1-O", "1.250,00", "01.10.2026"], ["A2-K", "-300,00", "01.10.2026"]],
        "acilis-bakiyesi-sablonu.xlsx",
    ),
}


@cache
def _template(kind: str) -> bytes:
    title, headers, examples, _ = _TEMPLATES[kind]
    workbook = openpyxl.Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = title
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in examples:
        sheet.append(row)
    for letter in "ABCD":
        sheet.column_dimensions[letter].width = 18
        sheet.column_dimensions[letter].number_format = "@"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def _send_template(kind: str) -> Response:
    data = await run_in_threadpool(_template, kind)
    name = _TEMPLATES[kind][3]
    return Response(
        data,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get(
    "/vehicles/template.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Araç şablonu"}},
)
async def vehicles_template(ctx: UnitsManage) -> Response:
    del ctx
    return await _send_template("vehicles")


@router.get(
    "/opening-balances/template.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Açılış bakiyesi şablonu"}},
)
async def opening_template(ctx: OpeningManage) -> Response:
    del ctx
    return await _send_template("opening-balances")


# --- Önizleme -------------------------------------------------------------------------------


class ImportRowIssueOut(BaseModel):
    row_number: int | None
    column: str | None
    message: str
    severity: Literal["error", "warning"]


class VehicleImportRowOut(BaseModel):
    row_number: int
    unit_id: uuid.UUID | None
    unit_name: str | None
    plate: str
    plate_display: str
    brand_model: str | None
    color: str | None
    ok: bool


class OpeningImportRowOut(BaseModel):
    row_number: int
    account_id: uuid.UUID | None
    reference_code: str
    unit_name: str | None
    person_name: str | None
    amount: Money | None = Field(description="mutlak tutar")
    direction: Literal["debit", "credit"]
    date: dt.date
    ok: bool


class VehicleImportPreviewOut(BaseModel):
    import_id: uuid.UUID
    type: Literal["vehicles"]
    total_rows: int
    importable_count: int
    total_amount: None = None
    issues: list[ImportRowIssueOut]
    rows: list[VehicleImportRowOut]
    expires_at: dt.datetime


class OpeningImportPreviewOut(BaseModel):
    import_id: uuid.UUID
    type: Literal["opening_balances"]
    total_rows: int
    importable_count: int
    total_amount: Money = Field(description="yüklenen toplam (borç +, alacak −)")
    issues: list[ImportRowIssueOut]
    rows: list[OpeningImportRowOut]
    expires_at: dt.datetime


async def _read(file: UploadFile) -> tuple[bytes, list[tuple[CellValue, ...]]]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        kind = svc.check_file(file.filename, data)
        rows = await run_in_threadpool(svc.read_rows, kind, data)
    except ImportFileError as exc:
        raise _file_error(exc) from exc
    return data, rows


def _issues(preview: svc.Preview) -> list[ImportRowIssueOut]:
    return [
        ImportRowIssueOut(
            row_number=i.row_number, column=i.column, message=i.message,
            severity=i.severity,  # type: ignore[arg-type]
        )
        for i in preview.issues
    ]  # fmt: skip


@router.post("/vehicles", summary="Araç aktarımı önizlemesi (.xlsx / .csv)")
async def upload_vehicles(
    ctx: UnitsManage,
    current: CurrentUserDep,
    store: StoreDep,
    now: NowDep,
    file: Annotated[UploadFile, File(description=".xlsx ya da .csv, en fazla 5 MB")],
) -> VehicleImportPreviewOut:
    """Sütunlar `Bölüm | Plaka | Marka | Renk`; hatalı satır aktarılmaz. **Kayıt oluşmaz.**"""
    data, rows = await _read(file)
    try:
        preview = await svc.preview_vehicles(ctx.session, rows)
    except ImportFileError as exc:
        raise _file_error(exc) from exc
    pending = await run_in_threadpool(store.save, ctx.site.id, current.user.id, data, now)
    await svc.open_batch(
        ctx.session, batch_id=pending.import_id, type_="vehicles", file_name=file.filename or "",
        preview=preview, user_id=current.user.id, user_name=current.user.full_name, now=now,
    )  # fmt: skip
    await ctx.session.commit()
    return VehicleImportPreviewOut(
        import_id=pending.import_id,
        type="vehicles",
        total_rows=len(preview.rows),
        importable_count=preview.importable_count,
        issues=_issues(preview),
        rows=[VehicleImportRowOut.model_validate(r) for r in preview.rows],
        expires_at=pending.expires_at,
    )


@router.post("/opening-balances", summary="Açılış bakiyesi aktarımı önizlemesi (.xlsx / .csv)")
async def upload_opening(
    ctx: OpeningManage,
    current: CurrentUserDep,
    store: StoreDep,
    now: NowDep,
    today: TodayDep,
    file: Annotated[UploadFile, File(description=".xlsx ya da .csv, en fazla 5 MB")],
) -> OpeningImportPreviewOut:
    """Sütunlar `Referans | Tutar | Tarih` (borç +, alacak −). Onayda tüm satırlar temiz
    olmalı ve eski sistem toplamı tutmalı. **Kayıt oluşmaz.**"""
    data, rows = await _read(file)
    try:
        preview = await svc.preview_opening(ctx.session, rows, today)
    except ImportFileError as exc:
        raise _file_error(exc) from exc
    pending = await run_in_threadpool(store.save, ctx.site.id, current.user.id, data, now)
    await svc.open_batch(
        ctx.session, batch_id=pending.import_id, type_="opening_balances",
        file_name=file.filename or "", preview=preview, user_id=current.user.id,
        user_name=current.user.full_name, now=now,
    )  # fmt: skip
    await ctx.session.commit()
    return OpeningImportPreviewOut(
        import_id=pending.import_id,
        type="opening_balances",
        total_rows=len(preview.rows),
        importable_count=preview.importable_count,
        total_amount=preview.total_amount or 0,  # type: ignore[arg-type]
        issues=_issues(preview),
        rows=[OpeningImportRowOut.model_validate(r) for r in preview.rows],
        expires_at=pending.expires_at,
    )


# --- Onay ve geri alma ---------------------------------------------------------------------


class ImportConfirmIn(BaseModel):
    reconciliation_total: Money | None = Field(
        default=None, description="eski sistem toplamı (yalnız açılış bakiyesinde, zorunlu)"
    )


class ImportConfirmOut(BaseModel):
    created: int
    skipped: list[str]
    total_amount: Money | None


async def _batch(ctx: SiteContext, import_id: uuid.UUID) -> ImportBatch:
    batch = await ctx.session.scalar(
        select(ImportBatch).where(ImportBatch.id == import_id).with_for_update()
    )
    if batch is None:
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", NOT_FOUND)
    if not ctx.access.can(PERMISSION_OF[batch.type]):
        raise ForbiddenError
    return batch


@router.post("/{import_id}/confirm", summary="Önizlenen araç / açılış bakiyesi aktarımını onayla")
async def confirm_import(
    import_id: uuid.UUID,
    ctx: SiteContextDep,
    current: CurrentUserDep,
    store: StoreDep,
    now: NowDep,
    today: TodayDep,
    body: ImportConfirmIn | None = None,
) -> Written[ImportConfirmOut]:
    batch = await _batch(ctx, import_id)
    if batch.type == "units" or batch.status != "previewed" or batch.user_id != current.user.id:
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", NOT_FOUND)
    with store.claim(ctx.site.id, current.user.id, import_id, now) as data:
        if data is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "not_found", NOT_FOUND)
        try:
            kind = svc.check_file(batch.file_name, data)
            rows = await run_in_threadpool(svc.read_rows, kind, data)
            if batch.type == "vehicles":
                preview = await svc.preview_vehicles(ctx.session, rows)
            else:
                preview = await svc.preview_opening(ctx.session, rows, today)
        except ImportFileError as exc:  # pragma: no cover — yüklemede doğrulandı
            raise _file_error(exc) from exc
        try:
            with bulk_operation():  # tek "import" olayı yazılır
                if batch.type == "vehicles":
                    outcome = await svc.apply_vehicles(ctx.session, batch, preview, today=today)
                else:
                    outcome = await svc.apply_opening(
                        ctx.session,
                        batch,
                        preview,
                        reconciliation_total=body.reconciliation_total if body else None,
                        today=today,
                    )
                svc.finish(batch, outcome, now)
                await ctx.session.flush()
        except svc.ImportRuleError as exc:
            await ctx.session.rollback()
            raise _rule_error(exc) from None
        record(
            ctx.session, AuditAction.IMPORT, batch.type, batch.id,
            {"file_name": batch.file_name, "created": outcome.created,
             "skipped": len(outcome.skipped),
             "total_amount": str(batch.total_amount) if batch.total_amount is not None else None},
        )  # fmt: skip
        await ctx.session.commit()
    if batch.type == "vehicles":
        message = f"{outcome.created} araç aktarıldı."
        if outcome.skipped:
            message += f" Atlanan: {', '.join(outcome.skipped)}."
    else:
        total = format_money_tr(batch.total_amount or 0)  # type: ignore[arg-type]
        message = (
            f"{outcome.created} hesaba açılış bakiyesi yazıldı; toplam {total} eski sistemle "
            "mutabık."
        )
    return Written(
        data=ImportConfirmOut(
            created=outcome.created, skipped=outcome.skipped, total_amount=batch.total_amount
        ),
        message=message,
    )


@router.post("/{import_id}/rollback", summary="Aktarımı geri al (7 gün)")
async def rollback_import(
    import_id: uuid.UUID, ctx: SiteContextDep, now: NowDep, today: TodayDep
) -> Written[ImportBatchOut]:
    batch = await _batch(ctx, import_id)
    if batch.status == "previewed":
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Aktarım bulunamadı.")
    try:
        with bulk_operation():
            removed = await svc.rollback(ctx.session, batch, today=today, now=now)
    except svc.ImportRuleError as exc:
        raise _rule_error(exc) from None
    record(
        ctx.session, AuditAction.DELETE, batch.type, batch.id,
        {"rolled_back": True, "removed": removed, "file_name": batch.file_name},
        reason="Aktarım geri alındı",
    )  # fmt: skip
    out = ImportBatchOut(
        id=batch.id,
        type=batch.type,  # type: ignore[arg-type]
        file_name=batch.file_name,
        status="rolled_back",
        row_count=batch.row_count,
        created_count=batch.created_count,
        total_amount=batch.total_amount,
        created_by=batch.created_by_name,
        created_at=batch.imported_at or batch.created_at,
        rollback_until=batch.rollback_until,
        rollback_blocked_by=None,
        can_rollback=False,
    )
    await ctx.session.commit()
    return Written(data=out, message=f"Aktarım geri alındı; {removed} kayıt kaldırıldı.")
