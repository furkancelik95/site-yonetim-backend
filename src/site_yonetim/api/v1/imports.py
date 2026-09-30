"""Excel'den bölüm ve kişi aktarımı uçları (docs/06 §2.5, docs/11).

Yükleme **hiçbir kayıt oluşturmaz**: önizleme döner ve dosya geçici depoya yazılır. Onay aynı
dosyayı yeniden doğrular ve tek transaction ile yazar; zaten kayıtlı bölümler atlanır.
Aktarımı yalnız onu yükleyen kullanıcı, aynı sitede, 6 saat içinde onaylayabilir.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from functools import cache
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SettingsDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Written
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.imports.unit_validator import (
    ImportFileError,
    Issue,
    PersonData,
    Severity,
    UnitRow,
)
from site_yonetim.services import imports as svc

router = APIRouter(prefix="/sites/{slug}/imports/units", tags=["excel aktarımı"])

UnitsManage = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def get_import_store(settings: SettingsDep) -> svc.ImportStore:
    return svc.ImportStore(settings.import_dir)


StoreDep = Annotated[svc.ImportStore, Depends(get_import_store)]


def _file_error(exc: ImportFileError) -> ApiError:
    status = (
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        if exc.code == "file_too_large"
        else HTTPStatus.UNPROCESSABLE_ENTITY
    )
    return ApiError(status, exc.code, exc.message, {"file": exc.message})


# --- Şablon -------------------------------------------------------------------------


@cache
def _template() -> bytes:
    return svc.build_template()


@router.get(
    "/template.xlsx",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "Boş şablon"}},
)
async def download_template(ctx: UnitsManage) -> Response:
    """Şablon: 'Daireler' (başlıklar + 3 örnek satır) ve 'Açıklama' sayfaları."""
    del ctx
    return Response(
        await run_in_threadpool(_template),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="daire-aktarim-sablonu.xlsx"'},
    )


# --- Önizleme -----------------------------------------------------------------------


class ImportIssueOut(BaseModel):
    row_number: int | None = Field(description="Excel satır numarası; dosya geneli için null")
    column: str | None = Field(description="Şablondaki sütun adı (ör. 'Brüt m²')")
    message: str
    severity: Literal["error", "warning"]

    @classmethod
    def of(cls, issue: Issue) -> ImportIssueOut:
        return cls(
            row_number=issue.row_number,
            column=issue.column,
            message=issue.message,
            severity=issue.severity.value,
        )


class ImportPersonOut(BaseModel):
    first_name: str
    last_name: str
    phone: str | None = Field(description="Yalnız people.read izniyle; aksi halde null")
    email: str | None = Field(description="Yalnız people.read izniyle; aksi halde null")

    @classmethod
    def of(cls, person: PersonData, *, contact: bool) -> ImportPersonOut:
        return cls(
            first_name=person.first_name,
            last_name=person.last_name,
            phone=person.phone if contact else None,
            email=person.email if contact else None,
        )


def _area(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


class ImportRowOut(BaseModel):
    row_number: int
    display_name: str = Field(description="`A-12`")
    block: str
    number: str
    floor: int | None
    unit_type: str | None
    gross_area: str | None = Field(description="m², metin")
    net_area: str | None = Field(description="m², metin")
    land_share_numerator: int | None
    land_share_denominator: int | None
    usage: str
    owner: ImportPersonOut
    tenant: ImportPersonOut | None
    already_exists: bool = Field(description="Sitede zaten kayıtlı; onayda atlanır")

    @classmethod
    def of(cls, row: UnitRow, *, exists: bool, contact: bool) -> ImportRowOut:
        return cls(
            row_number=row.row_number,
            display_name=row.display_name,
            block=row.block,
            number=row.number,
            floor=row.floor,
            unit_type=row.unit_type,
            gross_area=_area(row.gross_area),
            net_area=_area(row.net_area),
            land_share_numerator=row.land_share_numerator,
            land_share_denominator=row.land_share_denominator,
            usage=row.usage.value,
            owner=ImportPersonOut.of(row.owner, contact=contact),
            tenant=ImportPersonOut.of(row.tenant, contact=contact) if row.tenant else None,
            already_exists=exists,
        )


class ImportPreviewOut(BaseModel):
    import_id: uuid.UUID | None = Field(
        description="Onay için; aktarılacak yeni bölüm yoksa null (onay düğmesi kapalı)"
    )
    expires_at: datetime | None
    total_rows: int = Field(description="Boş olmayan veri satırı")
    importable_count: int = Field(description="Hatasız satır (zaten kayıtlı olanlar dahil)")
    new_count: int = Field(description="Onayda oluşturulacak bölüm")
    existing_count: int = Field(description="Sitede zaten kayıtlı; atlanacak")
    error_count: int
    warning_count: int
    rows: list[ImportRowOut]
    issues: list[ImportIssueOut]


@router.post("", response_model=ImportPreviewOut)
async def upload_units(
    ctx: UnitsManage,
    current: CurrentUserDep,
    store: StoreDep,
    now: NowDep,
    file: Annotated[UploadFile, File(description=".xlsx, en fazla 5 MB")],
) -> ImportPreviewOut:
    """Dosyayı doğrular ve önizleme döner. **Hiçbir kayıt oluşturulmaz.**"""
    data = await file.read(svc.MAX_UPLOAD_BYTES + 1)
    try:
        svc.check_upload(file.filename, data)
        result = await run_in_threadpool(svc.read_and_validate, data)
    except ImportFileError as exc:
        raise _file_error(exc) from exc
    preview = await svc.build_preview(ctx.session, ctx.site, result)
    new_count = len(preview.new_rows)
    pending = (
        await run_in_threadpool(store.save, ctx.site.id, current.user.id, data, now)
        if new_count
        else None
    )
    contact = ctx.access.can(Permission.PEOPLE_READ)
    issues = [*preview.notices, *result.issues]
    return ImportPreviewOut(
        import_id=pending.import_id if pending else None,
        expires_at=pending.expires_at if pending else None,
        total_rows=result.total_rows,
        importable_count=result.importable_count,
        new_count=new_count,
        existing_count=result.importable_count - new_count,
        error_count=result.error_count,
        warning_count=sum(1 for i in issues if i.severity is Severity.WARNING),
        rows=[
            ImportRowOut.of(row, exists=row.key in preview.existing, contact=contact)
            for row in result.rows
        ],
        issues=[ImportIssueOut.of(i) for i in issues],
    )


# --- Onay ---------------------------------------------------------------------------


class ImportResultOut(BaseModel):
    created_units: int
    created_people: int
    created_accounts: int
    created_blocks: int
    created_unit_types: int
    skipped: list[str] = Field(description="Zaten kayıtlı olduğu için atlanan bölümler (`A-5`)")


@router.post("/{import_id}/confirm", response_model=Written[ImportResultOut])
async def confirm_units(
    import_id: uuid.UUID,
    ctx: UnitsManage,
    current: CurrentUserDep,
    store: StoreDep,
    now: NowDep,
    today: TodayDep,
) -> Written[ImportResultOut]:
    """Önizlenen aktarımı tek transaction ile yazar; aynı aktarım ikinci kez onaylanamaz."""
    with store.claim(ctx.site.id, current.user.id, import_id, now) as data:
        if data is None:
            raise ApiError(
                HTTPStatus.NOT_FOUND,
                "import_not_found",
                "Aktarım bulunamadı ya da süresi doldu. Dosyayı yeniden yükleyin.",
            )
        try:
            result = await run_in_threadpool(svc.read_and_validate, data)
        except ImportFileError as exc:  # pragma: no cover - yüklemede zaten doğrulandı
            raise _file_error(exc) from exc
        try:
            outcome = await svc.apply_import(
                ctx.session, result.rows, start_date=svc.import_start_date(today)
            )
            await ctx.session.commit()
        except IntegrityError as exc:
            await ctx.session.rollback()
            raise ApiError(
                HTTPStatus.CONFLICT,
                "import_conflict",
                "Aktarım sırasında sitedeki bölümler değişti. Önizlemeyi yenilemek için "
                "dosyayı yeniden yükleyin.",
            ) from exc
    return Written(
        data=ImportResultOut(
            created_units=outcome.created_units,
            created_people=outcome.created_people,
            created_accounts=outcome.created_accounts,
            created_blocks=outcome.created_blocks,
            created_unit_types=outcome.created_unit_types,
            skipped=outcome.skipped,
        ),
        message=outcome.message,
    )
