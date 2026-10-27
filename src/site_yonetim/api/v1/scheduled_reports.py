"""Planlı raporlar — Yazılım Dokümanı RAP-05 (API taslağı `…/scheduled-reports`).

Yetki: `reports.schedule` (yönetici, muhasebe). Alıcı yalnız raporun verisine yetkili site
kullanıcısı olabilir. Gönderim `cli.py run-scheduled-reports` işiyle; "Şimdi gönder" aynı yolu
elle tetikler. Rapor içerikleri sonra gelir (e-posta şimdilik dönem + panel bağlantısı).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    BUSINESS_TZ,
    CurrentUserDep,
    NowDep,
    SettingsDep,
    SiteContext,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.scheduled_reports import (
    FREQUENCY_LABELS,
    REPORT_KINDS,
    ReportFormat,
    ReportFrequency,
    ReportRunStatus,
)
from site_yonetim.models import ScheduledReport, ScheduledReportRun
from site_yonetim.services import scheduled_reports as svc

router = APIRouter(prefix="/sites/{slug}", tags=["planlı raporlar"])
Schedule = Annotated[SiteContext, Depends(require_permission(Permission.REPORTS_SCHEDULE))]
Paging = Annotated[PageParams, Depends()]


class ReportKindOut(BaseModel):
    key: str
    label: str


class ReportRecipientOut(BaseModel):
    user_id: uuid.UUID
    full_name: str
    has_email: bool


class ScheduledReportOptionsOut(BaseModel):
    kinds: list[ReportKindOut]
    recipients: dict[str, list[ReportRecipientOut]] = Field(
        description="rapor türü → yetkili alıcı adayları"
    )


class ScheduledReportOut(BaseModel):
    id: uuid.UUID
    name: str
    report_key: str
    report_label: str
    frequency: ReportFrequency
    frequency_label: str
    weekday: int | None = Field(description="0 = Pazartesi")
    day_of_month: int | None
    send_time: str = Field(description='"SS:DD" tesis saati')
    format: ReportFormat
    recipients: list[ReportRecipientOut]
    is_active: bool
    next_run_at: dt.datetime
    last_run_at: dt.datetime | None
    created_by_name: str
    test_provider: bool = Field(default=True, description="e-posta test sağlayıcısıyla (K5)")


class ScheduledReportIn(BaseModel):
    name: str = Field(max_length=120)
    report_key: str = Field(max_length=40)
    frequency: ReportFrequency
    weekday: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    send_time: dt.time
    format: ReportFormat = ReportFormat.PDF
    recipient_user_ids: list[uuid.UUID] = Field(max_length=50)
    is_active: bool = True


class ScheduledReportRunOut(BaseModel):
    id: uuid.UUID
    run_at: dt.datetime
    period_label: str
    status: ReportRunStatus
    recipient_count: int
    error: str | None


def _form(exc: svc.ReportRuleError) -> ApiError:
    return ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "validation_error",
        "Formda düzeltilmesi gereken alanlar var.",
        exc.fields,
    )


async def _out(ctx: SiteContext, r: ScheduledReport) -> ScheduledReportOut:
    people = await svc.eligible_recipients(ctx.session, ctx.site, r.report_key)
    return ScheduledReportOut(
        id=r.id,
        name=r.name,
        report_key=r.report_key,
        report_label=REPORT_KINDS[r.report_key].label,
        frequency=ReportFrequency(r.frequency),
        frequency_label=FREQUENCY_LABELS[ReportFrequency(r.frequency)],
        weekday=r.weekday,
        day_of_month=r.day_of_month,
        send_time=r.send_time.strftime("%H:%M"),
        format=ReportFormat(r.format),
        recipients=[
            ReportRecipientOut(
                user_id=i,
                full_name=people[i].full_name if i in people else "Yetkisi kalmayan kullanıcı",
                has_email=bool(people[i].email) if i in people else False,
            )
            for i in r.recipient_user_ids
        ],
        is_active=r.is_active,
        next_run_at=r.next_run_at,
        last_run_at=r.last_run_at,
        created_by_name=r.created_by_name,
    )


def _data(body: ScheduledReportIn) -> svc.ReportData:
    return svc.ReportData(
        name=body.name,
        report_key=body.report_key,
        frequency=body.frequency,
        weekday=body.weekday,
        day_of_month=body.day_of_month,
        send_time=body.send_time.replace(second=0, microsecond=0, tzinfo=None),
        format=body.format,
        recipient_user_ids=body.recipient_user_ids,
        is_active=body.is_active,
    )


async def _get(ctx: SiteContext, report_id: uuid.UUID) -> ScheduledReport:
    report = await ctx.session.scalar(
        select(ScheduledReport).where(ScheduledReport.id == report_id).with_for_update()
    )
    if report is None:
        raise NotFoundError("Planlı rapor bulunamadı.")
    return report


@router.get("/scheduled-reports/options", summary="Planlı rapor: türler ve yetkili alıcılar")
async def options(ctx: Schedule) -> ScheduledReportOptionsOut:
    recipients = {}
    for key in REPORT_KINDS:
        people = await svc.eligible_recipients(ctx.session, ctx.site, key)
        recipients[key] = [
            ReportRecipientOut(user_id=p.user_id, full_name=p.full_name, has_email=bool(p.email))
            for p in sorted(people.values(), key=lambda p: p.full_name.casefold())
        ]
    return ScheduledReportOptionsOut(
        kinds=[ReportKindOut(key=k.key, label=k.label) for k in REPORT_KINDS.values()],
        recipients=recipients,
    )


@router.get("/scheduled-reports", summary="Planlı raporlar")
async def list_reports(ctx: Schedule, paging: Paging) -> Page[ScheduledReportOut]:
    query = select(ScheduledReport).order_by(ScheduledReport.name, ScheduledReport.id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=[await _out(ctx, r) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.post(
    "/scheduled-reports", status_code=status.HTTP_201_CREATED, summary="Planlı rapor tanımla"
)
async def create_report(
    body: ScheduledReportIn, ctx: Schedule, current: CurrentUserDep, now: NowDep
) -> Written[ScheduledReportOut]:
    try:
        report = await svc.create(
            ctx.session, ctx.site, _data(body), now=now, by=current.user.full_name
        )
    except svc.ReportRuleError as exc:
        raise _form(exc) from exc
    out = await _out(ctx, report)
    await ctx.session.commit()
    return Written(
        data=out, message=f"Planlı rapor kaydedildi; ilk gönderim {_when(out.next_run_at)}."
    )


def _when(at: dt.datetime) -> str:
    return at.astimezone(BUSINESS_TZ).strftime("%d.%m.%Y %H:%M")


@router.put("/scheduled-reports/{report_id}", summary="Planlı raporu güncelle")
async def update_report(
    report_id: uuid.UUID, body: ScheduledReportIn, ctx: Schedule, now: NowDep
) -> Written[ScheduledReportOut]:
    report = await _get(ctx, report_id)
    try:
        await svc.update(ctx.session, ctx.site, report, _data(body), now=now)
    except svc.ReportRuleError as exc:
        raise _form(exc) from exc
    out = await _out(ctx, report)
    await ctx.session.commit()
    return Written(data=out, message="Planlı rapor güncellendi.")


@router.delete(
    "/scheduled-reports/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Planlı raporu sil",
)
async def delete_report(report_id: uuid.UUID, ctx: Schedule) -> Response:
    """Tanım silinmez, pasifleşir (çalışma geçmişi korunur)."""
    report = await _get(ctx, report_id)
    report.is_active = False
    await ctx.session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/scheduled-reports/{report_id}/run", summary="Şimdi gönder")
async def run_now(
    report_id: uuid.UUID, ctx: Schedule, settings: SettingsDep, now: NowDep
) -> Written[ScheduledReportRunOut]:
    report = await _get(ctx, report_id)
    run = await svc.run_one(ctx.session, ctx.site, report, run_at=now, web_url=settings.web_url)
    out = ScheduledReportRunOut(
        id=run.id,
        run_at=run.run_at,
        period_label=run.period_label,
        status=ReportRunStatus(run.status),
        recipient_count=run.recipient_count,
        error=run.error,
    )
    await ctx.session.commit()
    message = (
        f"{run.recipient_count} alıcıya e-posta kuyruğa alındı (test sağlayıcısı, K5)."
        if run.status == ReportRunStatus.SENT.value
        else "Gönderilmedi: yetkili ve e-postası olan alıcı yok."
    )
    return Written(data=out, message=message)


@router.get("/scheduled-reports/{report_id}/runs", summary="Planlı rapor çalışma geçmişi")
async def runs(report_id: uuid.UUID, ctx: Schedule, paging: Paging) -> Page[ScheduledReportRunOut]:
    await _get(ctx, report_id)
    query = (
        select(ScheduledReportRun)
        .where(ScheduledReportRun.report_id == report_id)
        .order_by(ScheduledReportRun.run_at.desc(), ScheduledReportRun.id.desc())
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=[
            ScheduledReportRunOut(
                id=r.id,
                run_at=r.run_at,
                period_label=r.period_label,
                status=ReportRunStatus(r.status),
                recipient_count=r.recipient_count,
                error=r.error,
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )
