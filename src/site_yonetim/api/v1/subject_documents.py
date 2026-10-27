"""Kayda bağlı belgeler (poliçe, hasar dosyası, teknik varlık, muayene raporu) ve saha
fotoğrafları.

- `POST /subject-documents` (multipart): belge arşivine yükler ve kayda bağlar. İzin kaydın
  modülünden: sigorta → `expenses.manage`, teknik varlık → `inventory.manage`.
- `GET /subject-documents?subject_type=&subject_id=` ve `…/{id}/download`: kaydın okuma izni
  (`expenses.read` / `inventory.read`) — belge modülü kapalı olsa da.
- `GET /operation-photos/{id}`: sayaç okuması (`units.manage`), denetim (`requests.assign`) ya da
  görev kapanışı (`requests.read`) fotoğrafı; bağlı olmadığı kayıttan indirilemez.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from site_yonetim.api.deps import CurrentUserDep, SiteContext, SiteContextDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.expenses import StoreDep, send_document
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.documents import DocumentFieldError, DocumentRuleError
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.models import (
    ChecklistInspection,
    Document,
    InsuranceClaim,
    InsurancePolicy,
    MeterReading,
    TaskCompletion,
    TechnicalAsset,
)
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import documents as documents_svc
from site_yonetim.services import subject_documents as svc
from site_yonetim.services.facility_common import stored_file

router = APIRouter(prefix="/sites/{slug}", tags=["kayda bağlı belge"])
NOT_FOUND = "Belge bulunamadı."


class LinkedDocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    version: int = Field(description="son sürüm")
    valid_until: dt.date | None
    subject_type: str
    subject_id: uuid.UUID
    created_by: str | None
    created_at: dt.datetime
    updated_at: dt.datetime | None

    @classmethod
    def of(cls, d: Document) -> LinkedDocumentOut:
        return cls(
            id=d.id,
            title=d.title,
            version=d.current_version,
            valid_until=d.valid_until,
            subject_type=d.subject_type or "",
            subject_id=d.subject_id or uuid.UUID(int=0),
            created_by=d.created_by_name,
            created_at=d.created_at,
            updated_at=d.updated_at,
        )


def _spec(ctx: SiteContext, subject_type: str, *, manage: bool) -> svc.SubjectSpec:
    spec = svc.SUBJECTS.get(subject_type)
    if spec is None or (subject_type == "asset_inspection_records" and manage):
        raise ApiError(
            422, "validation_error", FORM_ERROR, {"subject_type": "Belge bu kayda bağlanamaz."}
        )
    if not ctx.access.can(spec.manage if manage else spec.read):
        raise ForbiddenError
    return spec


async def _default_title(ctx: SiteContext, subject_type: str, subject_id: uuid.UUID) -> str | None:
    session = ctx.session
    if subject_type == "insurance_policies":
        policy = await session.get(InsurancePolicy, subject_id)
        return f"Poliçe {policy.policy_no} ({policy.insurer})" if policy else None
    if subject_type == "insurance_claims":
        claim = await session.get(InsuranceClaim, subject_id)
        if claim is None:
            return None
        return f"Hasar dosyası {claim.incident_date:%d.%m.%Y} — {claim.description[:40]}"
    if subject_type == "technical_assets":
        asset = await session.get(TechnicalAsset, subject_id)
        return f"{asset.code} {asset.name}" if asset else None
    return None  # pragma: no cover - `_spec` engeller


@router.post(
    "/subject-documents",
    status_code=status.HTTP_201_CREATED,
    summary="Kayda belge ekle (multipart) — belge arşivine yazılır",
)
async def upload_subject_document(
    ctx: SiteContextDep,
    current: CurrentUserDep,
    store: StoreDep,
    subject_type: Annotated[
        str,
        Form(max_length=40, description="insurance_policies · insurance_claims · technical_assets"),
    ],
    subject_id: Annotated[uuid.UUID, Form()],
    title: Annotated[str | None, Form(max_length=200, description="boş: dosya adı")] = None,
    file: Annotated[UploadFile | None, File(description="PDF, PNG, JPEG, WebP · ≤ 10 MB")] = None,
) -> Written[LinkedDocumentOut]:
    _spec(ctx, subject_type, manage=True)
    base = await _default_title(ctx, subject_type, subject_id)
    if base is None:
        raise NotFoundError("Kayıt bulunamadı.")
    data = await file.read(MAX_FILE_BYTES + 1) if file is not None else b""
    label = " ".join((title or (file.filename if file else "") or "belge").split())
    written: str | None = None
    try:
        uploaded = await svc.attach(
            ctx.session,
            store,
            subject_type=subject_type,
            subject_id=subject_id,
            title=f"{base}: {label}",
            file_name=file.filename if file is not None else None,
            data=data,
            uploader=documents_svc.Uploader(current.user.id, current.user.full_name, False),
        )
        written = uploaded.storage_path
        await ctx.session.refresh(uploaded.document)
        out = LinkedDocumentOut.of(uploaded.document)
        await ctx.session.commit()
    except DocumentFieldError as exc:
        if written:
            store.delete(written)
        fields = {("file" if k in {"file", "title"} else k): v for k, v in exc.fields.items()}
        raise ApiError(422, "validation_error", FORM_ERROR, fields) from exc
    except DocumentRuleError as exc:
        if written:
            store.delete(written)
        raise ApiError(409, exc.code, exc.message) from exc
    except BaseException:
        if written:
            store.delete(written)
        raise
    return Written(
        data=out,
        message="Belge eklendi."
        if uploaded.is_new
        else f"Belgenin {uploaded.version}. sürümü eklendi.",
    )


@router.get("/subject-documents", summary="Kayda bağlı belgeler (eskiden yeniye)")
async def list_subject_documents(
    ctx: SiteContextDep,
    subject_type: Annotated[str, Query(max_length=40)],
    subject_id: uuid.UUID,
) -> list[LinkedDocumentOut]:
    _spec(ctx, subject_type, manage=False)
    found = await svc.for_subjects(ctx.session, subject_type, [subject_id])
    return [LinkedDocumentOut.of(d) for d in found.get(subject_id, [])]


@router.get(
    "/subject-documents/{document_id}/download",
    summary="Kayda bağlı belgenin son sürümünü indir",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/*": {}}}},
)
async def download_subject_document(
    document_id: uuid.UUID, ctx: SiteContextDep, store: StoreDep
) -> Response:
    found = await svc.latest_file(ctx.session, document_id)
    if found is None:
        raise NotFoundError(NOT_FOUND)
    document, stored = found
    _spec(ctx, document.subject_type or "", manage=False)
    response = send_document(stored, store)
    record(
        ctx.session, AuditAction.DOWNLOAD, "documents", document.id,
        {"version": document.current_version, "file_name": stored.file_name},
    )  # fmt: skip
    await ctx.session.commit()
    return response


@router.get(
    "/operation-photos/{photo_id}",
    summary="Saha fotoğrafı (sayaç, denetim, görev)",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}}},
)
async def download_photo(photo_id: uuid.UUID, ctx: SiteContextDep, store: StoreDep) -> Response:
    session = ctx.session
    needed: Permission | None = None
    if await session.scalar(select(MeterReading.id).where(MeterReading.photo_file_id == photo_id)):
        needed = Permission.UNITS_MANAGE
    elif await session.scalar(
        select(TaskCompletion.id).where(TaskCompletion.photo_file_id == photo_id)
    ):
        needed = Permission.REQUESTS_READ
    elif await session.scalar(
        select(ChecklistInspection.id).where(
            ChecklistInspection.answers.contains(cast([{"photo_id": str(photo_id)}], JSONB))
        )
    ):
        needed = Permission.REQUESTS_ASSIGN
    row = await stored_file(session, photo_id) if needed is not None else None
    if row is None or needed is None:
        raise NotFoundError("Fotoğraf bulunamadı.")
    if not ctx.access.can(needed):
        raise ForbiddenError
    return send_document(row, store)
