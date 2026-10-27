"""Denetim kurulu paketi — Yazılım Dokümanı RAP-05 (servis isteği 81).

- Liste: hazırlayan (`finance.charge.post`) ya da denetçi (`audit.read`).
- Hazırlama `finance.charge.post`: `{title, from, to, document_ids}` → dönem özeti, karar defteri
  ve belgeler dondurulur. Gizli belgeyi yalnız `documents.confidential` sahibi ekler.
- Denetim raporu `audit.read`: `{document_id, note}` → "denetlendi"; ikinci rapor 409.
- Excel: paketin dondurulmuş özeti (`…/{id}/export.xlsx`).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, SiteContextDep, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.models import AuditPackage
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import audit_packages as svc
from site_yonetim.services import exports

router = APIRouter(prefix="/sites/{slug}/audit-packages", tags=["rapor"])
Paging = Annotated[PageParams, Depends()]
NOT_FOUND = "Paket bulunamadı."


async def _reader(ctx: SiteContextDep) -> SiteContext:
    if not (
        ctx.access.can(Permission.FINANCE_CHARGE_POST) or ctx.access.can(Permission.AUDIT_READ)
    ):
        raise ForbiddenError
    return ctx


async def _preparer(ctx: SiteContextDep) -> SiteContext:
    if not ctx.access.can(Permission.FINANCE_CHARGE_POST):
        raise ForbiddenError("Denetim paketini muhasebe ya da yönetim hazırlar.")
    return ctx


async def _auditor(ctx: SiteContextDep) -> SiteContext:
    if not ctx.access.can(Permission.AUDIT_READ):
        raise ForbiddenError("Denetim raporunu denetçi yükler.")
    return ctx


Reader = Annotated[SiteContext, Depends(_reader)]
Preparer = Annotated[SiteContext, Depends(_preparer)]
Auditor = Annotated[SiteContext, Depends(_auditor)]


class PackageDecisionOut(BaseModel):
    no: int
    meeting_title: str
    item_title: str
    decision: str
    result: str | None
    date: dt.date


class PackageDocumentOut(BaseModel):
    id: uuid.UUID
    title: str


class AuditPackageOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    title: str
    from_: dt.date = Field(alias="from")
    to: dt.date
    created_by: str
    created_at: dt.datetime
    decisions: list[PackageDecisionOut]
    document_ids: list[uuid.UUID]
    documents: list[PackageDocumentOut]
    snapshot: dict[str, Any] = Field(
        description="hazırlandığı andaki dönem özeti (dondurulmuş): income, expense, difference, "
        "months, categories, budget_year, budget, collections, cash, cash_total — para metin"
    )
    status: str = Field(description="open · reviewed")
    report_document_id: uuid.UUID | None
    report_document_title: str | None
    reviewed_by: str | None
    reviewed_at: dt.datetime | None
    note: str | None


async def _outs(ctx: SiteContext, packages: list[AuditPackage]) -> list[AuditPackageOut]:
    ids = {i for p in packages for i in p.document_ids} | {
        p.report_document_id for p in packages if p.report_document_id
    }
    titles = await svc.document_titles(ctx.session, ids)
    return [
        AuditPackageOut(
            id=p.id,
            title=p.title,
            from_=p.period_from,
            to=p.period_to,
            created_by=p.created_by_name,
            created_at=p.created_at,
            decisions=[PackageDecisionOut(**d) for d in p.decisions],
            document_ids=list(p.document_ids),
            documents=[PackageDocumentOut(id=i, title=titles.get(i, "—")) for i in p.document_ids],
            snapshot=p.snapshot,
            status=p.status,
            report_document_id=p.report_document_id,
            report_document_title=titles.get(p.report_document_id)
            if p.report_document_id
            else None,
            reviewed_by=p.reviewed_by_name,
            reviewed_at=p.reviewed_at,
            note=p.note,
        )
        for p in packages
    ]


@router.get("", summary="Denetim paketleri (yeni üstte; sayfalı)")
async def list_packages(ctx: Reader, paging: Paging) -> Page[AuditPackageOut]:
    query = svc.list_query()
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _outs(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


class AuditPackageIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = Field(default=None, max_length=400)
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


def _error(exc: svc.PackageError) -> ApiError:
    if exc.field:
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {exc.field: exc.message},
        )
    return ApiError(HTTPStatus(exc.status), exc.code, exc.message)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Denetim paketi hazırla")
async def create_package(
    ctx: Preparer, body: AuditPackageIn, current: CurrentUserDep, today: TodayDep
) -> Written[AuditPackageOut]:
    errors = svc.check_form(body.title, body.from_, body.to)
    if errors:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, errors)
    assert body.from_ is not None  # noqa: S101  # nosec B101 — check_form garanti eder
    assert body.to is not None  # noqa: S101  # nosec B101
    try:
        package = await svc.create(
            ctx.session,
            title=body.title or "",
            start=body.from_,
            end=body.to,
            document_ids=body.document_ids,
            confidential=ctx.access.can(Permission.DOCUMENTS_CONFIDENTIAL),
            actor=current.user.full_name,
            today=today,
        )
    except svc.PackageError as exc:
        raise _error(exc) from None
    record(
        ctx.session, AuditAction.CREATE, "audit_packages", package.id,
        {"title": package.title, "from": package.period_from.isoformat(),
         "to": package.period_to.isoformat(), "documents": len(package.document_ids)},
    )  # fmt: skip
    (out,) = await _outs(ctx, [package])
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"Denetim paketi hazırlandı: {len(package.decisions)} karar, "
        f"{len(package.document_ids)} belge; dönem özeti donduruldu.",
    )


class AuditReportIn(BaseModel):
    document_id: uuid.UUID | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/{package_id}/report", summary="Denetim raporunu ekle (denetçi) → denetlendi")
async def add_report(
    ctx: Auditor, package_id: uuid.UUID, body: AuditReportIn, current: CurrentUserDep, now: NowDep
) -> Written[AuditPackageOut]:
    package = await svc.get(ctx.session, package_id, lock=True)
    if package is None:
        raise NotFoundError(NOT_FOUND)
    try:
        await svc.add_report(
            ctx.session,
            package,
            document_id=body.document_id,
            note=body.note,
            actor=current.user.full_name,
            now=now,
        )
    except svc.PackageError as exc:
        raise _error(exc) from None
    record(
        ctx.session, AuditAction.UPDATE, "audit_packages", package.id,
        {"status": "reviewed", "report_document_id": str(package.report_document_id)},
    )  # fmt: skip
    (out,) = await _outs(ctx, [package])
    await ctx.session.commit()
    return Written(data=out, message="Denetim raporu pakete eklendi; paket arşivlendi.")


@router.get(
    "/{package_id}/export.xlsx",
    summary="Paketin dondurulmuş özeti (Excel)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX_MEDIA_TYPE: {}}}},
)
async def export_package(ctx: Reader, package_id: uuid.UUID) -> Response:
    package = await svc.get(ctx.session, package_id)
    if package is None:
        raise NotFoundError(NOT_FOUND)
    (out,) = await _outs(ctx, [package])
    s = package.snapshot
    data = exports.content_xlsx(
        out.title,
        f"{out.from_:%d.%m.%Y} – {out.to:%d.%m.%Y}",
        [("Gelir", s.get("income")), ("Gider", s.get("expense")), ("Fark", s.get("difference")),
         ("Kasa ve banka (aktif)", s.get("cash_total")), ("Hazırlayan", out.created_by),
         ("Durum", "Denetlendi" if out.status == "reviewed" else "Denetim bekliyor")],
        [
            ("Aylar", ("Dönem", "Gelir", "Gider"),
             [(m["period"], m["income"], m["expense"]) for m in s.get("months", [])]),
            ("Gider kategorileri", ("Kategori", "Tutar", "Adet"),
             [(c["name"], c["amount"], c["count"]) for c in s.get("categories", [])]),
            (f"Bütçe {s.get('budget_year', '')}",
             ("Kategori", "Bütçe", "Gerçekleşen", "Kullanım %"),
             [(b["name"], b["budgeted"], b["actual"], b["usage"]) for b in s.get("budget", [])]),
            ("Tahsilat", ("Dönem", "Kesilen", "Tahsil", "Oran %"),
             [(c["period"], c["charged"], c["collected"], c["rate"])
              for c in s.get("collections", [])]),
            ("Kasa ve banka", ("Hesap", "Bakiye", "Aktif"),
             [(c["name"], c["balance"], c["is_active"]) for c in s.get("cash", [])]),
            ("Kararlar", ("No", "Toplantı", "Madde", "Karar", "Tarih"),
             [(d.no, d.meeting_title, d.item_title, d.decision, d.date) for d in out.decisions]),
            ("Belgeler", ("Belge",), [(d.title,) for d in out.documents]),
        ],
        [],
    )  # fmt: skip
    record(ctx.session, AuditAction.EXPORT, "audit_packages", package.id, {"xlsx": True})
    await ctx.session.commit()
    return Response(
        data,
        media_type=exports.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="denetim-paketi.xlsx"'},
    )
