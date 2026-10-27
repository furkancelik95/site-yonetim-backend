"""Şeffaflık raporu uçları — Yazılım Dokümanı RAP-02, PM kararı K32. Modül `finance` (çekirdek).

- Personel okuma: `finance.reports.read` ya da `transparency.manage`; hazırlama, silme ve yayına
  gönderme `transparency.manage` (yönetici, muhasebe).
- Yayın: "Şeffaflık raporu" onay kuralı varsa 202 `approval_pending`; yoksa yalnız
  `transparency.publish` (yönetici) doğrudan yayınlar, diğerine 403.
- Sakin yalnız yayınlanmış raporları görür; raporda kişi ve bölüm bilgisi yoktur.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.requests import rule_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.communication import TransparencyStatus
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.text import format_period_tr
from site_yonetim.models import TransparencyReport
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import transparency as svc

router = APIRouter(prefix="/sites/{slug}/transparency-reports", tags=["şeffaflık"])
resident_router = APIRouter(prefix="/sites/{slug}/resident/transparency-reports", tags=["sakin"])
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.FINANCE))]
Paging = Annotated[PageParams, Depends()]


async def _reader(ctx: Module) -> SiteContext:
    if not (
        ctx.access.can(Permission.FINANCE_REPORTS_READ)
        or ctx.access.can(Permission.TRANSPARENCY_MANAGE)
    ):
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.TRANSPARENCY_MANAGE):
        raise ForbiddenError
    return ctx


async def _resident(ctx: Module) -> SiteContext:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]
Resident = Annotated[SiteContext, Depends(_resident)]


class TransparencyScope(BaseModel):
    include_categories: bool = Field(default=True, description="gider kategorisi dökümü")
    include_collection: bool = Field(default=True, description="aidat tahsilat oranı")
    include_cash_balance: bool = Field(default=False, description="kasa + banka toplam bakiyesi")


class TransparencyCategory(BaseModel):
    name: str
    amount: Money
    count: int
    share: str = Field(description="giderdeki pay %, metin")


class TransparencyCollection(BaseModel):
    charged: Money = Field(description="dönemde kesilen aidat")
    collected: Money = Field(description="o borçlara yapılan tahsilat")
    rate: str = Field(description="tahsilat oranı %, metin")


class TransparencyContent(BaseModel):
    """Kişi/bölüm içermez: yalnız toplamlar ve kategori adları."""

    period: str
    income: Money
    expense: Money
    difference: Money
    categories: list[TransparencyCategory] | None = Field(description="kapsamda yoksa null")
    collection: TransparencyCollection | None
    cash_balance: Money | None


class TransparencyReportOut(BaseModel):
    id: uuid.UUID
    year: int
    month: int
    period: str
    status: TransparencyStatus
    scope: TransparencyScope
    content: TransparencyContent
    note: str | None
    prepared_by: str
    created_at: dt.datetime
    approval_request_id: uuid.UUID | None
    published_at: dt.datetime | None
    published_by: str | None
    rejection_reason: str | None


class ResidentTransparencyReportOut(BaseModel):
    id: uuid.UUID
    year: int
    month: int
    period: str
    content: TransparencyContent
    note: str | None
    published_at: dt.datetime


def _out(r: TransparencyReport) -> TransparencyReportOut:
    return TransparencyReportOut(
        id=r.id,
        year=r.year,
        month=r.month,
        period=format_period_tr(r.year, r.month),
        status=TransparencyStatus(r.status),
        scope=TransparencyScope(**r.scope),
        content=TransparencyContent(**r.content),
        note=r.note,
        prepared_by=r.prepared_by,
        created_at=r.created_at,
        approval_request_id=r.approval_request_id,
        published_at=r.published_at,
        published_by=r.published_by,
        rejection_reason=r.rejection_reason,
    )


def _resident_out(r: TransparencyReport) -> ResidentTransparencyReportOut:
    return ResidentTransparencyReportOut(
        id=r.id,
        year=r.year,
        month=r.month,
        period=format_period_tr(r.year, r.month),
        content=TransparencyContent(**r.content),
        note=r.note,
        published_at=r.published_at or r.created_at,
    )


_ORDER = (TransparencyReport.year.desc(), TransparencyReport.month.desc(), TransparencyReport.id)


@router.get("", summary="Şeffaflık raporları (yeni dönem üstte)")
async def list_reports(ctx: Reader, paging: Paging) -> Page[TransparencyReportOut]:
    query = select(TransparencyReport)
    total = await ctx.session.scalar(select(func.count()).select_from(TransparencyReport)) or 0
    rows = await ctx.session.scalars(
        query.order_by(*_ORDER).offset(paging.offset).limit(paging.page_size)
    )
    return Page(
        items=[_out(r) for r in rows], page=paging.page, page_size=paging.page_size, total=total
    )


class TransparencyReportIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    scope: TransparencyScope = Field(default_factory=TransparencyScope)
    note: str | None = Field(default=None, max_length=2000)


@router.post("", status_code=status.HTTP_201_CREATED, summary="Dönem için taslak hazırla")
async def prepare(
    body: TransparencyReportIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[TransparencyReportOut]:
    try:
        report = await svc.prepare(
            ctx.session,
            year=body.year,
            month=body.month,
            scope=body.scope.model_dump(),
            note=body.note,
            by=current.user.full_name,
            today=today,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    await ctx.session.refresh(report)
    return Written(
        data=_out(report),
        message="Taslak hazırlandı; tutarlar bu an donduruldu. Kontrol edip yayına gönderin.",
    )


async def _report(
    ctx: SiteContext, report_id: uuid.UUID, *, lock: bool = False
) -> TransparencyReport:
    report = await svc.get(ctx.session, report_id, lock=lock)
    if report is None:
        raise NotFoundError("Rapor bulunamadı.")
    return report


@router.get("/{report_id}", summary="Şeffaflık raporu")
async def get_report(report_id: uuid.UUID, ctx: Reader) -> TransparencyReportOut:
    return _out(await _report(ctx, report_id))


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Taslağı sil")
async def delete_report(report_id: uuid.UUID, ctx: Manager) -> Response:
    report = await _report(ctx, report_id, lock=True)
    try:
        await svc.delete_draft(ctx.session, report)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Response(status_code=HTTPStatus.NO_CONTENT)


class TransparencyPublishOut(BaseModel):
    status: Literal["published", "approval_pending"]
    report: TransparencyReportOut
    approval_request_id: uuid.UUID | None


@router.post("/{report_id}/publish", summary="Yayına gönder (onay kuralı varsa 202)")
async def publish_report(
    report_id: uuid.UUID,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[TransparencyPublishOut]:
    report = await _report(ctx, report_id, lock=True)
    try:
        outcome = await svc.submit(
            ctx.session,
            ctx.site,
            report,
            requester=approvals_svc.Person(current.user.id, current.user.full_name),
            can_publish=ctx.access.can(Permission.TRANSPARENCY_PUBLISH),
            today=today,
            now=now,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    except PermissionError as exc:
        raise ApiError(HTTPStatus.FORBIDDEN, "forbidden", str(exc)) from exc
    except approvals_svc.ApprovalError as exc:  # pragma: no cover — yürütücü kayıtlı
        raise ApiError(exc.status, exc.code, exc.message) from exc
    await ctx.session.commit()
    await ctx.session.refresh(report)
    if outcome.approval_request_id is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=TransparencyPublishOut(
                status="approval_pending",
                report=_out(report),
                approval_request_id=outcome.approval_request_id,
            ),
            message="Rapor onaya gönderildi; onaylanınca sakinlere yayınlanır.",
        )
    return Written(
        data=TransparencyPublishOut(
            status="published", report=_out(report), approval_request_id=None
        ),
        message=f"Rapor yayınlandı; {outcome.notified} sakine bildirim gitti.",
    )


# --- Sakin --------------------------------------------------------------------------------


@resident_router.get("", summary="Yayınlanmış şeffaflık raporları")
async def my_reports(ctx: Resident, paging: Paging) -> Page[ResidentTransparencyReportOut]:
    published = TransparencyReport.status == TransparencyStatus.PUBLISHED.value
    total = (
        await ctx.session.scalar(
            select(func.count()).select_from(TransparencyReport).where(published)
        )
        or 0
    )
    rows = await ctx.session.scalars(
        select(TransparencyReport)
        .where(published)
        .order_by(*_ORDER)
        .offset(paging.offset)
        .limit(paging.page_size)
    )
    return Page(
        items=[_resident_out(r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@resident_router.get("/{report_id}", summary="Yayınlanmış şeffaflık raporu")
async def my_report(report_id: uuid.UUID, ctx: Resident) -> ResidentTransparencyReportOut:
    report = await svc.get(ctx.session, report_id)
    if report is None or report.status != TransparencyStatus.PUBLISHED.value:
        raise NotFoundError("Rapor bulunamadı.")
    return _resident_out(report)
