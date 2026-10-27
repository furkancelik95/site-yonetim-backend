"""Hizmetler uçları — talep ayarları, kategoriler, hazır yanıtlar, ana olaylar, hizmet tarifesi,
teklif tamamlama, tadilat ve taşınma (Yazılım Dokümanı HIZ-02..05; servis istekleri 47–52).
Modül: `requests` (kapalıysa 404).

Yetki: okuma `requests.read`; işlem (ana olay, teklif, izin kararı) `requests.assign`; ayar
yazma (kategori, SLA, takvim, şablon, tarife, tadilat kuralları) `requests.settings`. Kategori
listesi talep açabilen herkese (sakin dahil) açıktır.
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_module,
)
from site_yonetim.api.schemas import Money, Page, PageParams, Written
from site_yonetim.api.v1.requests import (
    ServiceQuoteOut,
    form_error,
    require,
    rule_error,
)
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.approvals import ApprovalStatus
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError, RequestPriority
from site_yonetim.domain.service_desk import (
    HourType,
    PermitStatus,
    PermitType,
    RuleError,
    calendar_json,
)
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Outage,
    Permit,
    ReplyTemplate,
    RequestCategory,
    ServiceTariff,
)
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import paid_services as paid_svc
from site_yonetim.services import permits as permits_svc
from site_yonetim.services import request_setup as setup_svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services import service_desk as desk_svc

router = APIRouter(prefix="/sites/{slug}", tags=["hizmetler"])
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]
Paging = Annotated[PageParams, Depends()]


def _person(current: CurrentUserDep) -> approvals_svc.Person:
    return approvals_svc.Person(current.user.id, current.user.full_name)


# --- Kategoriler (D-26, HIZ-03) ------------------------------------------------------------


class ResolveHours(BaseModel):
    low: int
    normal: int
    high: int
    urgent: int


class RequestCategoryOut(BaseModel):
    id: uuid.UUID
    key: str = Field(description="değişmez anahtar; talepte `category`")
    name: str
    hour_type: HourType
    resolve_hours: ResolveHours = Field(description="öncelik bazında çözüm süresi (saat)")
    default_priority: RequestPriority
    department_id: uuid.UUID | None = Field(description="atanacak ekip (talep buraya yönlenir)")
    is_active: bool

    @classmethod
    def of(cls, c: RequestCategory) -> RequestCategoryOut:
        return cls(
            id=c.id,
            key=c.key,
            name=c.name,
            hour_type=HourType(c.hour_type),
            resolve_hours=ResolveHours(
                low=c.hours_low, normal=c.hours_normal, high=c.hours_high, urgent=c.hours_urgent
            ),
            default_priority=RequestPriority(c.default_priority),
            department_id=c.department_id,
            is_active=c.is_active,
        )


@router.get("/request-categories", summary="Talep kategorileri")
async def list_categories(
    ctx: Module,
    active: Annotated[bool, Query(description="true → yalnız kullanımdakiler")] = False,
) -> list[RequestCategoryOut]:
    if not (
        ctx.access.can(Permission.REQUESTS_READ)
        or ctx.access.can(Permission.REQUESTS_CREATE)
        or ctx.access.person_id is not None
    ):
        raise ForbiddenError
    return [
        RequestCategoryOut.of(c)
        for c in await setup_svc.categories(ctx.session, active_only=active)
    ]


class CategoryIn(BaseModel):
    name: str = Field(max_length=60)
    hour_type: HourType = HourType.BUSINESS
    resolve_hours: dict[str, Any] = Field(
        default_factory=lambda: {"low": 168, "normal": 72, "high": 24, "urgent": 4}
    )
    default_priority: RequestPriority = RequestPriority.NORMAL
    department_id: uuid.UUID | None = None
    is_active: bool = True


async def _save_category(
    ctx: SiteContext, body: CategoryIn, category: RequestCategory | None
) -> RequestCategory:
    require(ctx, Permission.REQUESTS_SETTINGS)
    try:
        saved = await setup_svc.save_category(
            ctx.session,
            setup_svc.CategoryData(
                name=body.name,
                hour_type=body.hour_type,
                resolve_hours=body.resolve_hours,
                default_priority=body.default_priority,
                department_id=body.department_id,
                is_active=body.is_active,
            ),
            category=category,
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return saved


@router.post("/request-categories", status_code=status.HTTP_201_CREATED, summary="Kategori ekle")
async def create_category(ctx: Module, body: CategoryIn) -> Written[RequestCategoryOut]:
    saved = await _save_category(ctx, body, None)
    return Written(
        data=RequestCategoryOut.of(saved),
        message=f'"{saved.name}" kategorisi eklendi; yeni talepler bu kuralla açılır.',
    )


@router.patch("/request-categories/{category_id}", summary="Kategoriyi düzenle / pasifleştir")
async def update_category(
    category_id: uuid.UUID, ctx: Module, body: CategoryIn
) -> Written[RequestCategoryOut]:
    category = await ctx.session.get(RequestCategory, category_id)
    if category is None:
        raise NotFoundError("Kategori bulunamadı.")
    saved = await _save_category(ctx, body, category)
    return Written(
        data=RequestCategoryOut.of(saved),
        message="Kategori kaydedildi; açık talepler açıldıkları kuralla devam eder.",
    )


# --- SLA ve takvim (servis isteği 47) ------------------------------------------------------


class DayHoursOut(BaseModel):
    closed: bool
    open: str = Field(description="SS:DD")
    close: str


class CategorySlaOut(BaseModel):
    hour_type: HourType
    resolve_hours: ResolveHours


class RequestSettingsOut(BaseModel):
    categories: dict[str, CategorySlaOut] = Field(description="kategori anahtarı → SLA")
    calendar: list[DayHoursOut] = Field(description="7 gün, 0 = pazartesi")
    auto_close_days: int = Field(description="çözüldü talep, sakin yanıtsızsa kaç günde kapanır")
    merge_window_hours: int = Field(description="birleştirme önerisi penceresi")
    low_rating_threshold: int = Field(description="bu puan ve altı yöneticiye bildirilir")


async def _settings_out(ctx: SiteContext) -> RequestSettingsOut:
    settings = await setup_svc.load_settings(ctx.session)
    return RequestSettingsOut(
        categories={
            c.key: CategorySlaOut(
                hour_type=HourType(c.hour_type),
                resolve_hours=RequestCategoryOut.of(c).resolve_hours,
            )
            for c in await setup_svc.categories(ctx.session)
        },
        calendar=[DayHoursOut(**d) for d in calendar_json(settings.calendar)],
        auto_close_days=settings.auto_close_days,
        merge_window_hours=settings.merge_window_hours,
        low_rating_threshold=settings.low_rating_threshold,
    )


@router.get("/settings/requests", summary="Talep ayarları: SLA, takvim, süreler")
async def get_settings(ctx: Module) -> RequestSettingsOut:
    require(ctx, Permission.REQUESTS_READ)
    return await _settings_out(ctx)


class RequestSettingsIn(BaseModel):
    categories: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="yalnız değişen kategoriler de gönderilebilir"
    )
    calendar: list[dict[str, Any]]
    auto_close_days: int
    merge_window_hours: int
    low_rating_threshold: int


@router.put("/settings/requests", summary="Talep ayarlarını kaydet")
async def put_settings(ctx: Module, body: RequestSettingsIn) -> Written[RequestSettingsOut]:
    require(ctx, Permission.REQUESTS_SETTINGS)
    try:
        await setup_svc.save_settings(
            ctx.session,
            setup_svc.SettingsInput(
                categories=body.categories,
                calendar=body.calendar,
                auto_close_days=body.auto_close_days,
                merge_window_hours=body.merge_window_hours,
                low_rating_threshold=body.low_rating_threshold,
            ),
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _settings_out(ctx),
        message="Talep ayarları kaydedildi; yeni talepler bu kurallarla açılır, açık talepler "
        "açıldıkları kuralla devam eder.",
    )


# --- Hazır yanıtlar (servis isteği 48) -----------------------------------------------------


class ReplyTemplateOut(BaseModel):
    id: uuid.UUID
    title: str
    category: str | None = Field(description="null = tüm kategoriler")
    body: str = Field(description="değişkenler: {{sakin.ad}}, {{talep.no}}, {{talep.konu}}")
    is_active: bool

    @classmethod
    def of(cls, t: ReplyTemplate) -> ReplyTemplateOut:
        return cls(id=t.id, title=t.title, category=t.category, body=t.body, is_active=t.is_active)


class ReplyTemplateIn(BaseModel):
    title: str = Field(max_length=60)
    category: str | None = Field(default=None, max_length=60)
    body: str = Field(max_length=1000)
    is_active: bool = True


@router.get("/reply-templates", summary="Hazır yanıt şablonları")
async def list_templates(ctx: Module) -> list[ReplyTemplateOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    return [ReplyTemplateOut.of(t) for t in await setup_svc.templates(ctx.session)]


async def _save_template(
    ctx: SiteContext, body: ReplyTemplateIn, template: ReplyTemplate | None
) -> ReplyTemplate:
    require(ctx, Permission.REQUESTS_SETTINGS)
    try:
        saved = await setup_svc.save_template(
            ctx.session,
            setup_svc.TemplateData(body.title, body.category, body.body, body.is_active),
            template=template,
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return saved


@router.post("/reply-templates", status_code=status.HTTP_201_CREATED, summary="Şablon ekle")
async def create_template(ctx: Module, body: ReplyTemplateIn) -> Written[ReplyTemplateOut]:
    saved = await _save_template(ctx, body, None)
    return Written(data=ReplyTemplateOut.of(saved), message="Şablon eklendi.")


@router.patch("/reply-templates/{template_id}", summary="Şablonu düzenle")
async def update_template(
    template_id: uuid.UUID, ctx: Module, body: ReplyTemplateIn
) -> Written[ReplyTemplateOut]:
    template = await ctx.session.get(ReplyTemplate, template_id)
    if template is None:
        raise NotFoundError("Şablon bulunamadı.")
    saved = await _save_template(ctx, body, template)
    return Written(data=ReplyTemplateOut.of(saved), message="Şablon güncellendi.")


# --- Ana olaylar (servis isteği 49) --------------------------------------------------------


class OutageOut(BaseModel):
    id: uuid.UUID
    title: str
    category: str
    location: str | None
    public_description: str
    status: Literal["open", "resolved"]
    request_ids: list[uuid.UUID]
    created_by: str
    created_at: dt.datetime
    resolved_at: dt.datetime | None
    resolution: str | None


async def _outages_out(ctx: SiteContext, rows: list[Outage]) -> list[OutageOut]:
    linked = await desk_svc.outage_counts(ctx.session, [o.id for o in rows])
    return [
        OutageOut(
            id=o.id,
            title=o.title,
            category=o.category,
            location=o.location,
            public_description=o.public_description,
            status="resolved" if o.status == "resolved" else "open",
            request_ids=linked.get(o.id, []),
            created_by=o.created_by_name,
            created_at=o.created_at,
            resolved_at=o.resolved_at,
            resolution=o.resolution,
        )
        for o in rows
    ]


@router.get("/outages", summary="Ana olaylar (yeni üstte)")
async def list_outages(
    ctx: Module,
    paging: Paging,
    status_filter: Annotated[Literal["open", "resolved"] | None, Query(alias="status")] = None,
) -> Page[OutageOut]:
    require(ctx, Permission.REQUESTS_READ)
    query = select(Outage)
    if status_filter:
        query = query.where(Outage.status == status_filter)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(
        await ctx.session.scalars(
            query.order_by(Outage.created_at.desc(), Outage.id.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    return Page(
        items=await _outages_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class OutageIn(BaseModel):
    title: str = Field(max_length=120)
    category: str = Field(max_length=60)
    location: str | None = Field(default=None, max_length=200)
    public_description: str = Field(max_length=1000, description="sakinlerin göreceği açıklama")
    request_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)


async def _outage(ctx: SiteContext, outage_id: uuid.UUID) -> Outage:
    require(ctx, Permission.REQUESTS_ASSIGN)
    outage = await desk_svc.get_outage(ctx.session, outage_id, lock=True)
    if outage is None:
        raise NotFoundError("Ana olay bulunamadı.")
    return outage


@router.post("/outages", status_code=status.HTTP_201_CREATED, summary="Ana olay oluştur")
async def create_outage(
    ctx: Module, body: OutageIn, current: CurrentUserDep, now: NowDep
) -> Written[OutageOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    try:
        outage = await desk_svc.create_outage(
            ctx.session,
            desk_svc.OutageData(
                body.title, body.category, body.location, body.public_description,
                body.request_ids,
            ),
            actor=current.user.full_name,
            now=now,
        )  # fmt: skip
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await _outages_out(ctx, [outage])
    await ctx.session.commit()
    linked = len(data.request_ids)
    return Written(
        data=data,
        message=f"Ana olay oluşturuldu{f'; {linked} talep bağlandı' if linked else ''}.",
    )


class OutageLinkIn(BaseModel):
    request_id: uuid.UUID


@router.post("/outages/{outage_id}/links", summary="Talebi ana olaya bağla")
async def link_outage(
    outage_id: uuid.UUID, ctx: Module, body: OutageLinkIn, current: CurrentUserDep, now: NowDep
) -> Written[OutageOut]:
    outage = await _outage(ctx, outage_id)
    try:
        await desk_svc.link(
            ctx.session, outage, body.request_id, actor=current.user.full_name, now=now
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await _outages_out(ctx, [outage])
    await ctx.session.commit()
    return Written(
        data=data, message="Talep ana olaya bağlandı; sakin ana olayın genel açıklamasını görür."
    )


@router.post("/outages/{outage_id}/unlink", summary="Talebi ana olaydan ayır")
async def unlink_outage(
    outage_id: uuid.UUID, ctx: Module, body: OutageLinkIn, current: CurrentUserDep, now: NowDep
) -> Written[OutageOut]:
    outage = await _outage(ctx, outage_id)
    try:
        await desk_svc.unlink(
            ctx.session, outage, body.request_id, actor=current.user.full_name, now=now
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await _outages_out(ctx, [outage])
    await ctx.session.commit()
    return Written(data=data, message="Bağlantı kaldırıldı.")


class OutageResolveIn(BaseModel):
    resolution: str = Field(max_length=2000)


@router.post("/outages/{outage_id}/resolve", summary="Ana olay çözüldü (bağlı talepler çözülür)")
async def resolve_outage(
    outage_id: uuid.UUID, ctx: Module, body: OutageResolveIn, current: CurrentUserDep, now: NowDep
) -> Written[OutageOut]:
    outage = await _outage(ctx, outage_id)
    try:
        count = await desk_svc.resolve_outage(
            ctx.session, outage, body.resolution, actor=current.user.full_name, now=now
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await _outages_out(ctx, [outage])
    await ctx.session.commit()
    return Written(
        data=data,
        message=f'Ana olay çözüldü; {count} bağlı talep "çözüldü" oldu ve sakinlere bildirildi.',
    )


# --- Hizmet tarifesi (servis isteği 51) ----------------------------------------------------


class ServiceTariffOut(BaseModel):
    id: uuid.UUID
    name: str
    unit: str
    price: Money
    is_active: bool

    @classmethod
    def of(cls, t: ServiceTariff) -> ServiceTariffOut:
        return cls(id=t.id, name=t.name, unit=t.unit, price=t.price, is_active=t.is_active)


class ServiceTariffIn(BaseModel):
    name: str = Field(max_length=60)
    unit: str = Field(max_length=20)
    price: Money
    is_active: bool = True


class TariffWriteOut(BaseModel):
    status: Literal["saved", "approval_pending"]
    tariff: ServiceTariffOut | None
    approval_request_id: uuid.UUID | None


@router.get("/service-tariffs", summary="Hizmet tarifesi")
async def list_tariffs(ctx: Module) -> list[ServiceTariffOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    return [ServiceTariffOut.of(t) for t in await paid_svc.tariffs(ctx.session)]


async def _save_tariff(
    ctx: SiteContext,
    body: ServiceTariffIn,
    tariff: ServiceTariff | None,
    current: CurrentUserDep,
    today: dt.date,
    now: dt.datetime,
    response: Response,
) -> Written[TariffWriteOut]:
    require(ctx, Permission.REQUESTS_SETTINGS)
    try:
        result = await paid_svc.save_tariff(
            ctx.session,
            ctx.site,
            paid_svc.TariffData(body.name, body.unit, body.price, body.is_active),
            tariff=tariff,
            requester=_person(current),
            today=today,
            now=now,
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    except approvals_svc.ApprovalError as exc:  # pragma: no cover — yürütücü kayıtlı
        raise rule_error(OperationRuleError(exc.code, exc.message, conflict=True)) from exc
    await ctx.session.commit()
    if result.approval is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=TariffWriteOut(
                status="approval_pending", tariff=None, approval_request_id=result.approval.id
            ),
            message="Tarife değişikliği onaya gönderildi; onaylanınca yürürlüğe girer.",
        )
    if result.tariff is None:  # pragma: no cover — onaysız yazım kaydı döndürür
        raise RuntimeError("Tarife yazılmadı")
    return Written(
        data=TariffWriteOut(
            status="saved", tariff=ServiceTariffOut.of(result.tariff), approval_request_id=None
        ),
        message="Tarife kalemi kaydedildi." if tariff else "Tarife kalemi eklendi.",
    )


@router.post(
    "/service-tariffs",
    status_code=status.HTTP_201_CREATED,
    summary="Tarife kalemi ekle (onay kuralı varsa 202)",
)
async def create_tariff(
    ctx: Module,
    body: ServiceTariffIn,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[TariffWriteOut]:
    return await _save_tariff(ctx, body, None, current, today, now, response)


@router.patch(
    "/service-tariffs/{tariff_id}", summary="Tarife kalemini değiştir (onay kuralı varsa 202)"
)
async def update_tariff(
    tariff_id: uuid.UUID,
    ctx: Module,
    body: ServiceTariffIn,
    current: CurrentUserDep,
    today: TodayDep,
    now: NowDep,
    response: Response,
) -> Written[TariffWriteOut]:
    tariff = await ctx.session.get(ServiceTariff, tariff_id)
    if tariff is None:
        raise NotFoundError("Tarife kalemi bulunamadı.")
    return await _save_tariff(ctx, body, tariff, current, today, now, response)


class QuoteCompleteIn(BaseModel):
    actual_total: Money | None = Field(description="gerçekleşen tutar")


@router.post("/quotes/{quote_id}/complete", summary="İş tamamlandı: gerçekleşen tutar")
async def complete_quote(
    quote_id: uuid.UUID,
    ctx: Module,
    body: QuoteCompleteIn,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[ServiceQuoteOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    quote = await paid_svc.get_quote(ctx.session, quote_id)
    request = await requests_svc.get(ctx.session, quote.request_id) if quote else None
    if quote is None or request is None:
        raise NotFoundError("Teklif bulunamadı.")
    try:
        await paid_svc.complete(
            ctx.session, quote, request, body.actual_total, actor=current.user.full_name,
            now=now, today=today,
        )  # fmt: skip
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    lines = (await paid_svc.lines_of(ctx.session, [quote.id]))[quote.id]
    data = ServiceQuoteOut.of(quote, lines)
    await ctx.session.commit()
    if quote.billed_total is None:
        message = "Gerçekleşen tutar teklifi aşıyor; ek tutar için sakin onayı istendi."
    else:
        message = (
            f"İş tamamlandı; {format_money_tr(quote.billed_total)} cariye yansıdı (ücretli hizmet)."
        )
    return Written(data=data, message=message)


# --- Tadilat ve taşınma (servis isteği 52) -------------------------------------------------


class PermitRulesOut(BaseModel):
    weekday_hours: str = Field(description="`09:00-18:00`")
    saturday_hours: str | None = Field(description="null: cumartesi yasak")
    sunday_allowed: bool
    deposit_amount: Money = Field(description="0: depozito alınmaz")
    checklist: list[str] = Field(description="tamamlanma kontrol listesi (tadilat)")

    @classmethod
    def of(cls, r: permits_svc.Rules) -> PermitRulesOut:
        return cls(
            weekday_hours=r.weekday_hours,
            saturday_hours=r.saturday_hours,
            sunday_allowed=r.sunday_allowed,
            deposit_amount=r.deposit_amount,
            checklist=r.checklist,
        )


class PermitRulesIn(BaseModel):
    weekday_hours: str = Field(max_length=11)
    saturday_hours: str | None = Field(default=None, max_length=11)
    sunday_allowed: bool = False
    deposit_amount: Money = Decimal(0)
    checklist: list[str] = Field(default_factory=list, max_length=30)


@router.get("/settings/permits", summary="Tadilat kuralları")
async def get_permit_rules(ctx: Module) -> PermitRulesOut:
    require(ctx, Permission.REQUESTS_READ)
    return PermitRulesOut.of(await permits_svc.rules(ctx.session))


@router.put("/settings/permits", summary="Tadilat kurallarını kaydet")
async def put_permit_rules(ctx: Module, body: PermitRulesIn) -> Written[PermitRulesOut]:
    require(ctx, Permission.REQUESTS_SETTINGS)
    try:
        saved = await permits_svc.save_rules(
            ctx.session,
            permits_svc.Rules(
                body.weekday_hours, body.saturday_hours, body.sunday_allowed,
                body.deposit_amount, body.checklist,
            ),
        )  # fmt: skip
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(data=PermitRulesOut.of(saved), message="Tadilat kuralları kaydedildi.")


class ChecklistItem(BaseModel):
    item: str = Field(max_length=120)
    ok: bool


class PermitOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    type: PermitType
    date_from: dt.date
    date_to: dt.date
    contractor: str | None
    description: str
    status: PermitStatus
    approval_request_id: uuid.UUID | None = Field(description="onay kuralı varsa açılan talep")
    approval_status: ApprovalStatus | None
    reject_reason: str | None
    checklist: list[ChecklistItem]
    deposit: Money | None
    deposit_refunded: bool
    created_by: str
    created_at: dt.datetime
    decided_by: str | None
    decided_at: dt.datetime | None
    completed_by: str | None
    completed_at: dt.datetime | None


async def permits_out(ctx: SiteContext, rows: list[Permit]) -> list[PermitOut]:
    units = await requests_svc.unit_names(ctx.session, {p.unit_id for p in rows})
    approvals = await permits_svc.approval_status(
        ctx.session, [p.approval_request_id for p in rows if p.approval_request_id]
    )
    return [
        PermitOut(
            id=p.id,
            unit_id=p.unit_id,
            unit_name=units.get(p.unit_id, "—"),
            type=PermitType(p.type),
            date_from=p.date_from,
            date_to=p.date_to,
            contractor=p.contractor,
            description=p.description,
            status=PermitStatus(p.status),
            approval_request_id=p.approval_request_id,
            approval_status=approvals.get(p.approval_request_id) if p.approval_request_id else None,
            reject_reason=p.reject_reason,
            checklist=[ChecklistItem(**c) for c in p.checklist],
            deposit=p.deposit,
            deposit_refunded=p.refund_entry_id is not None,
            created_by=p.created_by_name,
            created_at=p.created_at,
            decided_by=p.decided_by_name,
            decided_at=p.decided_at,
            completed_by=p.completed_by_name,
            completed_at=p.completed_at,
        )
        for p in rows
    ]


@router.get("/permits", summary="Tadilat ve taşınma başvuruları")
async def list_permits(
    ctx: Module,
    paging: Paging,
    status_filter: Annotated[PermitStatus | None, Query(alias="status")] = None,
) -> Page[PermitOut]:
    require(ctx, Permission.REQUESTS_READ)
    query = permits_svc.list_query(status=status_filter, unit_ids=None)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await permits_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _permit(ctx: SiteContext, permit_id: uuid.UUID) -> Permit:
    require(ctx, Permission.REQUESTS_ASSIGN)
    permit = await permits_svc.get(ctx.session, permit_id)
    if permit is None:
        raise NotFoundError("Başvuru bulunamadı.")
    return permit


class PermitDecisionOut(BaseModel):
    status: Literal["approved", "approval_pending"]
    permit: PermitOut
    approval_request_id: uuid.UUID | None


@router.post("/permits/{permit_id}/approve", summary="Başvuruyu onayla (onay kuralı varsa 202)")
async def approve_permit(
    permit_id: uuid.UUID,
    ctx: Module,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[PermitDecisionOut]:
    permit = await _permit(ctx, permit_id)
    try:
        approval = await permits_svc.approve(
            ctx.session, ctx.site, permit, requester=_person(current), now=now, today=today
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await permits_out(ctx, [permit])
    await ctx.session.commit()
    if approval is not None:
        response.status_code = HTTPStatus.ACCEPTED
        return Written(
            data=PermitDecisionOut(
                status="approval_pending", permit=data, approval_request_id=approval.id
            ),
            message="Başvuru onaya gönderildi (Tadilat / taşınma izni kuralı); onaylanınca "
            "sakine, komşulara ve güvenliğe bildirilir.",
        )
    message = "Başvuru onaylandı; sakine, komşu bölümlere ve güvenliğe bildirildi."
    if permit.deposit:
        message += f" {format_money_tr(permit.deposit)} depozito malik hesabına yazıldı."
    return Written(
        data=PermitDecisionOut(status="approved", permit=data, approval_request_id=None),
        message=message,
    )


class PermitRejectIn(BaseModel):
    reason: str = Field(max_length=500)


@router.post("/permits/{permit_id}/reject", summary="Başvuruyu reddet (gerekçeli)")
async def reject_permit(
    permit_id: uuid.UUID, ctx: Module, body: PermitRejectIn, current: CurrentUserDep, now: NowDep
) -> Written[PermitOut]:
    permit = await _permit(ctx, permit_id)
    try:
        await permits_svc.reject(
            ctx.session, permit, body.reason, actor=current.user.full_name, now=now
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await permits_out(ctx, [permit])
    await ctx.session.commit()
    return Written(data=data, message="Başvuru reddedildi; sakine gerekçe bildirildi.")


class PermitCompleteIn(BaseModel):
    checklist: list[ChecklistItem] = Field(default_factory=list, max_length=30)
    refund_deposit: bool = False


@router.post("/permits/{permit_id}/complete", summary="Tamamla (kontrol listesi, depozito iadesi)")
async def complete_permit(
    permit_id: uuid.UUID,
    ctx: Module,
    body: PermitCompleteIn,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
) -> Written[PermitOut]:
    permit = await _permit(ctx, permit_id)
    try:
        await permits_svc.complete(
            ctx.session, permit, [(c.item, c.ok) for c in body.checklist], body.refund_deposit,
            actor=current.user.full_name, now=now, today=today,
        )  # fmt: skip
    except RuleError as exc:
        raise form_error(exc) from exc
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    [data] = await permits_out(ctx, [permit])
    await ctx.session.commit()
    message = "Tamamlandı."
    if permit.refund_entry_id:
        message += " Depozito iadesi ters kayıtla yapıldı."
    if permit.type == PermitType.MOVE_OUT.value:
        message += " Bölümdeki ilişkinin bitiş tarihini bölüm kartından girin."
    return Written(data=data, message=message)
