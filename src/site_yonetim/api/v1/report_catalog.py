"""Rapor kataloğu, metrik sözlüğü, talep/SLA raporu ve katalog raporu dışa aktarma
(Yazılım Dokümanı RAP-01, §18.2; servis isteği 79).

- `GET …/reports/catalog`: yalnız kullanıcının izni olan ve modülü açık raporlar; her metriğin
  tek tanımı (metrik sözlüğü).
- `GET …/reports/requests-sla`: `requests.read`, modül `requests` (kapalıysa 404). Varsayılan
  dönem bitişten geriye 30 gün.
- `GET …/reports/{key}/export.xlsx`: içerik üreticisi olan katalog raporu (rapor izni gerekir;
  yetkisizse 403). Dışa aktarma denetim kaydına yazılır. Planlı rapor gönderimi aynı üreticiyi
  kullanır (`services/report_catalog.produce`).
"""

import datetime as dt
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from site_yonetim.api.deps import (
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_module,
)
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.report_catalog import BY_KEY, SlaLine, visible
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import exports
from site_yonetim.services import report_catalog as svc

router = APIRouter(prefix="/sites/{slug}/reports", tags=["rapor"])
RequestsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]
FORBIDDEN = "Bu raporu görme yetkiniz yok."


class MetricOut(BaseModel):
    key: str
    name: str
    definition: str


class CatalogReportOut(BaseModel):
    key: str
    name: str
    group: str
    permission: str
    link: str = Field(description="site altındaki ekran yolu")
    exportable: bool = Field(description="Excel dışa aktarma ve planlı gönderim üreticisi var")
    metrics: list[MetricOut]


@router.get("/catalog", summary="Rapor kataloğu (yetki ve modüle göre süzülür)")
async def catalog(ctx: SiteContextDep) -> list[CatalogReportOut]:
    return [
        CatalogReportOut(
            key=r.key,
            name=r.name,
            group=r.group,
            permission=r.permission.value,
            link=r.link,
            exportable=svc.has_producer(r.key),
            metrics=[MetricOut(key=m.key, name=m.name, definition=m.definition) for m in r.metrics],
        )
        for r in visible(ctx.access.permissions, ctx.modules)
    ]


class SlaLineOut(BaseModel):
    total: int = Field(description="dönemde açılan")
    open: int = Field(description="dönemde açılıp hâlâ açık")
    closed: int = Field(description="dönemde çözülen")
    sla_rate: str | None = Field(description="SLA uyumu %, metin (`92.5`); veri yoksa null")
    avg_hours: str | None = Field(description="ortalama çözüm (saat), metin")
    satisfaction: str | None = Field(description="kapanış puanı ortalaması (1–5), metin")
    rated: int

    @classmethod
    def of(cls, line: SlaLine) -> SlaLineOut:
        def text(value: object) -> str | None:
            return None if value is None else format(value, "f")

        return cls(
            total=line.total,
            open=line.open,
            closed=line.closed,
            sla_rate=text(line.sla_rate),
            avg_hours=text(line.avg_hours),
            satisfaction=text(line.satisfaction),
            rated=line.rated,
        )


class SlaRowOut(SlaLineOut):
    category: str
    category_name: str


class SlaReportOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(alias="from")
    to: dt.date
    total: SlaLineOut
    rows: list[SlaRowOut]


def _period(start: dt.date | None, end: dt.date) -> svc.ReportPeriod:
    period = svc.sla_window(end, start)
    if period.start > period.end:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
            {"to": "Bitiş başlangıçtan önce olamaz."},
        )  # fmt: skip
    if (period.end - period.start).days >= svc.MAX_PERIOD_DAYS:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
            {"from": "Dönem en fazla 1 yıl olabilir."},
        )  # fmt: skip
    return period


DateFrom = Annotated[dt.date | None, Query(alias="from", description="boşsa bitişten 30 gün önce")]
DateTo = Annotated[dt.date | None, Query(description="boşsa bugün")]


@router.get("/requests-sla", summary="Talep ve SLA raporu (kategori bazında)")
async def requests_sla(
    ctx: RequestsModule, today: TodayDep, from_: DateFrom = None, to: DateTo = None
) -> SlaReportOut:
    if not ctx.access.can(Permission.REQUESTS_READ):
        raise ForbiddenError(FORBIDDEN)
    report = await svc.requests_sla(ctx.session, _period(from_, to or today))
    return SlaReportOut(
        from_=report.period.start,
        to=report.period.end,
        total=SlaLineOut.of(report.total),
        rows=[
            SlaRowOut(
                category=r.category,
                category_name=r.category_name,
                **SlaLineOut.of(r.line).model_dump(),
            )
            for r in report.rows
        ],
    )


@router.get(
    "/{key}/export.xlsx",
    summary="Katalog raporunu Excel'e aktar (özet, tablolar, metrik tanımları)",
    response_class=Response,
    responses={200: {"content": {exports.XLSX_MEDIA_TYPE: {}}}},
)
async def export_report(
    ctx: SiteContextDep,
    key: str,
    today: TodayDep,
    now: NowDep,
    from_: DateFrom = None,
    to: DateTo = None,
) -> Response:
    report = BY_KEY.get(key)
    if report is None or not svc.has_producer(key):
        raise NotFoundError("Rapor bulunamadı.")
    if report.module is not None and report.module not in ctx.modules:
        raise NotFoundError("Rapor bulunamadı.")
    if not ctx.access.can(report.permission):
        raise ForbiddenError(FORBIDDEN)
    content = await svc.produce(key, ctx.session, _period(from_, to or today), now=now)
    data = exports.content_xlsx(
        content.title,
        content.period.label,
        content.summary,
        [(t.title, t.columns, t.rows) for t in content.tables],
        [(m.name, m.definition) for m in content.metrics],
    )
    record(
        ctx.session,
        AuditAction.EXPORT,
        "reports",
        None,
        {"report": key, "from": content.period.start.isoformat(),
         "to": content.period.end.isoformat()},
    )  # fmt: skip
    await ctx.session.commit()
    return Response(
        data,
        media_type=exports.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="rapor-{key}.xlsx"'},
    )
