"""Belge arşivi — Yazılım Dokümanı RAP-03, 18.2 (frontend servis isteği 78).

Modül `documents` (kapalıysa 404).
- Personel: liste ve indirme `documents.read`; yükleme/yeni sürüm `documents.manage`; gizli
  belge `documents.confidential` — yetkisi olmayana gizli belge **yok**tur (404) ve her açılışı
  kayda geçer (`…/access-log`).
- Sakin (`/resident/documents`): sakinlere açık belgeler (kapsamındaysa) ve bölümüne özel
  belgeler; son sürüm indirilir.
- Dosya içerik imzasıyla doğrulanır (PDF, PNG, JPEG, WebP · ≤ 10 MB), web kökü dışında saklanır,
  SHA-256 özeti tutulur. Virüs taraması yoktur.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.expenses import StoreDep, send_document
from site_yonetim.api.v1.resident import resident
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.documents import (
    KIND_LABELS,
    Confidentiality,
    DocumentFieldError,
    DocumentKind,
    DocumentRuleError,
    DocumentScope,
    ExpiryWarning,
    days_left,
    expiry_warning,
)
from site_yonetim.domain.files import MAX_FILE_BYTES
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Document
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import documents as svc

router = APIRouter(prefix="/sites/{slug}/documents", tags=["belge"])
resident_router = APIRouter(prefix="/sites/{slug}/resident/documents", tags=["sakin"])
Paging = Annotated[PageParams, Depends()]
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.DOCUMENTS))]
NOT_FOUND = "Belge bulunamadı."


async def _reader(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.DOCUMENTS_READ):
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.DOCUMENTS_MANAGE):
        raise ForbiddenError
    return ctx


async def _resident(ctx: Module) -> SiteContext:
    return await resident(ctx)


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]
Resident = Annotated[SiteContext, Depends(_resident)]


def _form_error(exc: DocumentFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def _conflict(exc: DocumentRuleError) -> ApiError:
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


class DocumentVersionOut(BaseModel):
    v: int
    file_name: str
    content_type: str
    size: int = Field(description="bayt")
    sha256: str
    uploaded_by: str
    at: dt.datetime


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    kind: DocumentKind
    kind_label: str
    scope: DocumentScope
    scope_id: uuid.UUID | None = Field(description="blok ya da bölüm; tüm sitede null")
    scope_name: str | None = Field(description="`A Blok` · `A-12`; tüm sitede null")
    confidentiality: Confidentiality
    valid_until: dt.date | None
    days_left: int | None = Field(description="bitişe kalan gün (sitenin bugünü); geçmişse eksi")
    warning: ExpiryWarning | None = Field(description="60 · 30 · expired · null")
    retention_years: int | None = Field(
        description="saklama süresi (yıl, K8) — bilgi amaçlı, otomatik silme yok"
    )
    version: int = Field(description="son sürüm")
    file_name: str
    content_type: str
    size: int
    sha256: str
    uploaded_by: str
    updated_at: dt.datetime = Field(description="son sürümün yüklenme zamanı")
    versions: list[DocumentVersionOut] = Field(description="eskiden yeniye")


class ResidentDocumentOut(BaseModel):
    """Sakin görünümü: yükleyen, özet ve sürüm geçmişi yok."""

    id: uuid.UUID
    title: str
    kind: DocumentKind
    kind_label: str
    scope: DocumentScope
    scope_name: str | None
    confidentiality: Confidentiality
    valid_until: dt.date | None
    days_left: int | None
    warning: ExpiryWarning | None
    version: int
    file_name: str
    content_type: str
    size: int
    updated_at: dt.datetime


class DocumentAccessOut(BaseModel):
    by: str
    at: dt.datetime
    v: int


async def _outputs(ctx: SiteContext, rows: list[Document], today: dt.date) -> list[DocumentOut]:
    by_doc = await svc.versions(ctx.session, [d.id for d in rows])
    names = await svc.scope_names(ctx.session, rows)
    out: list[DocumentOut] = []
    for d in rows:
        history = by_doc[d.id]
        last = history[-1]
        left = days_left(d.valid_until, today)
        target = d.block_id or d.unit_id
        out.append(
            DocumentOut(
                id=d.id,
                title=d.title,
                kind=DocumentKind(d.kind),
                kind_label=KIND_LABELS[DocumentKind(d.kind)],
                scope=DocumentScope(d.scope),
                scope_id=target,
                scope_name=names.get(target) if target else None,
                confidentiality=Confidentiality(d.confidentiality),
                valid_until=d.valid_until,
                days_left=left,
                warning=expiry_warning(left),
                retention_years=d.retention_years,
                version=last.version,
                file_name=last.file_name,
                content_type=last.content_type,
                size=last.size,
                sha256=last.sha256,
                uploaded_by=last.uploaded_by,
                updated_at=last.at,
                versions=[
                    DocumentVersionOut(
                        v=v.version,
                        file_name=v.file_name,
                        content_type=v.content_type,
                        size=v.size,
                        sha256=v.sha256,
                        uploaded_by=v.uploaded_by,
                        at=v.at,
                    )
                    for v in history
                ],
            )
        )
    return out


def _can_confidential(ctx: SiteContext) -> bool:
    return ctx.access.can(Permission.DOCUMENTS_CONFIDENTIAL)


async def _visible(ctx: SiteContext, document_id: uuid.UUID) -> Document:
    """Personel için belge; gizli belge yetkisizse "yok" (404 — varlığı sızmaz)."""
    document = await svc.get(ctx.session, document_id)
    if document is None or (
        document.confidentiality == Confidentiality.SECRET.value and not _can_confidential(ctx)
    ):
        raise NotFoundError(NOT_FOUND)
    return document


async def _page(
    ctx: SiteContext, query: Select[Document], paging: PageParams
) -> tuple[list[Document], int]:
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return rows, total


# --- Personel -----------------------------------------------------------------------


@router.get("", summary="Belgeler (son yüklenen üstte; sayfalı)")
async def list_documents(
    ctx: Reader,
    paging: Paging,
    today: TodayDep,
    q: Annotated[str | None, Query(max_length=120, description="ad içinde arar")] = None,
    kind: Annotated[DocumentKind | None, Query()] = None,
    expiring: Annotated[
        bool, Query(description="yalnız 60 gün içinde biten ya da bitmiş (`1`/`true`)")
    ] = False,
) -> Page[DocumentOut]:
    query = svc.staff_query(
        can_confidential=_can_confidential(ctx),
        today=today,
        q=q,
        kind=kind.value if kind else None,
        expiring=expiring,
    )
    rows, total = await _page(ctx, query, paging)
    return Page(
        items=await _outputs(ctx, rows, today),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Belge yükle (multipart) — aynı ad + kapsam yeni sürüm olur",
    response_model=Written[DocumentOut],
    responses={409: {"description": "`same_file` — dosya son sürümle aynı"}},
)
async def upload_document(
    ctx: Manager,
    current: CurrentUserDep,
    store: StoreDep,
    today: TodayDep,
    title: Annotated[str | None, Form(max_length=400)] = None,
    kind: Annotated[str | None, Form(max_length=30, description="plan · rules · …")] = None,
    scope: Annotated[str | None, Form(max_length=10, description="site · block · unit")] = None,
    scope_id: Annotated[str | None, Form(max_length=64, description="blok ya da bölüm id")] = None,
    confidentiality: Annotated[
        str | None, Form(max_length=20, description="public · unit · management · secret")
    ] = None,
    valid_until: Annotated[str | None, Form(max_length=10, description="YYYY-AA-GG")] = None,
    retention_years: Annotated[str | None, Form(max_length=4, description="yıl (K8)")] = None,
    file: Annotated[
        UploadFile | None, File(description="PDF, PNG, JPEG, WebP · ≤ 10 MB (içerik doğrulanır)")
    ] = None,
) -> Written[DocumentOut]:
    errors: dict[str, str] = {}
    target = _parse_uuid(scope_id) if scope_id else None
    if scope_id and target is None:
        errors["scope"] = "Blok ya da bölüm seçin."
    until = _parse_date(valid_until) if valid_until else None
    if valid_until and until is None:
        errors["valid_until"] = "Tarih seçin."
    years = _parse_int(retention_years) if retention_years else None
    if retention_years and years is None:
        errors["retention_years"] = "Saklama süresini yıl olarak yazın."
    data = await file.read(MAX_FILE_BYTES + 1) if file is not None else b""
    written: str | None = None
    try:
        uploaded = await svc.upload(
            ctx.session,
            svc.Upload(
                title=title, kind=kind, scope=scope, scope_id=target,
                confidentiality=confidentiality, valid_until=until, retention_years=years,
                file_name=file.filename if file is not None else None, data=data,
                form_errors=errors,
            ),
            uploader=svc.Uploader(
                current.user.id, current.user.full_name, _can_confidential(ctx)
            ),
            store=store,
        )  # fmt: skip
        written = uploaded.storage_path
        document = uploaded.document
        await ctx.session.flush()
        await ctx.session.refresh(document)
        out = (await _outputs(ctx, [document], today))[0]
        await ctx.session.commit()
    except DocumentFieldError as exc:
        if written:
            store.delete(written)
        raise _form_error(exc) from exc
    except DocumentRuleError as exc:
        if written:
            store.delete(written)
        raise _conflict(exc) from exc
    except BaseException:
        if written:
            store.delete(written)  # reddedilen kayıttan diskte iz kalmaz
        raise
    message = (
        f"{document.title} arşive eklendi."
        if uploaded.is_new
        else f"{document.title} için {uploaded.version}. sürüm kaydedildi; eski sürümler korunur."
    )
    return Written(data=out, message=message)


def _parse_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    return int(value) if value.strip().isdigit() else None


@router.get(
    "/{document_id}/versions/{version}/download",
    summary="Sürümü indir (gizli belgede açılış kayda geçer)",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/*": {}}}},
)
async def download_version(
    document_id: uuid.UUID,
    version: int,
    ctx: Reader,
    current: CurrentUserDep,
    store: StoreDep,
) -> Response:
    document = await _visible(ctx, document_id)
    stored = await svc.version_file(ctx.session, document, version)
    if stored is None:
        raise NotFoundError(NOT_FOUND)
    response = send_document(stored, store)
    svc.log_secret_access(
        ctx.session, document, version, user_id=current.user.id, user_name=current.user.full_name
    )
    record(
        ctx.session, AuditAction.DOWNLOAD, "documents", document.id,
        {"version": version, "file_name": stored.file_name},
    )  # fmt: skip
    await ctx.session.commit()
    return response


@router.get("/{document_id}/access-log", summary="Gizli belgenin açılış kaydı (yeni üstte)")
async def access_log(
    document_id: uuid.UUID, ctx: Reader, paging: Paging
) -> Page[DocumentAccessOut]:
    await _visible(ctx, document_id)
    query = svc.access_log_query(document_id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size))
    return Page(
        items=[DocumentAccessOut(by=r.user_name, at=r.created_at, v=r.version) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


# --- Sakin --------------------------------------------------------------------------


async def _resident_visible(ctx: SiteContext, today: dt.date) -> svc.MyPlaces:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı zaten engeller
        raise ForbiddenError
    return await svc.my_places(ctx.session, person, today)


@resident_router.get("", summary="Bana açık belgeler (sayfalı)")
async def my_documents(ctx: Resident, paging: Paging, today: TodayDep) -> Page[ResidentDocumentOut]:
    query = svc.resident_query(await _resident_visible(ctx, today))
    rows, total = await _page(ctx, query, paging)
    items = [
        ResidentDocumentOut(
            id=o.id, title=o.title, kind=o.kind, kind_label=o.kind_label, scope=o.scope,
            scope_name=o.scope_name, confidentiality=o.confidentiality,
            valid_until=o.valid_until, days_left=o.days_left, warning=o.warning,
            version=o.version, file_name=o.file_name, content_type=o.content_type, size=o.size,
            updated_at=o.updated_at,
        )
        for o in await _outputs(ctx, rows, today)
    ]  # fmt: skip
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


@resident_router.get(
    "/{document_id}/download",
    summary="Belgenin son sürümünü indir",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}, "image/*": {}}}},
)
async def my_document_download(
    document_id: uuid.UUID, ctx: Resident, store: StoreDep, today: TodayDep
) -> Response:
    places = await _resident_visible(ctx, today)
    document = await ctx.session.scalar(
        svc.resident_query(places).where(Document.id == document_id)
    )
    stored = (
        await svc.version_file(ctx.session, document, document.current_version)
        if document is not None
        else None
    )
    if document is None or stored is None:
        raise NotFoundError(NOT_FOUND)
    response = send_document(stored, store)
    record(
        ctx.session, AuditAction.DOWNLOAD, "documents", document.id,
        {"version": document.current_version, "file_name": stored.file_name},
    )  # fmt: skip
    await ctx.session.commit()
    return response
