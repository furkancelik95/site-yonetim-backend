"""Talep / arıza uçları — docs/06 §2.10 (modül: `requests`; kapalıysa 404).

- `requests.read` olan tüm talepleri görür; izni olmayan sakin yalnız **kendi** taleplerini
  (docs/05 §6). Başkasının talebi "yok"tur (404).
- Sakin talebi kendi adına ve yalnız kendi bölümü (ya da ortak alan) için açar.
- Durum akışı dokümandaki geçiş tablosuyla (K18): tablo dışı 409; çözüldü/iptal yapılırken
  `resolution` zorunlu. Satırda `allowed_transitions` (personelin seçebileceği durumlar).
- Satırda SLA (`sla`), ana olay özeti, son sakin değerlendirmesi ve son ücretli hizmet teklifi.
- İç not (K26) ayrı uçtadır (`/internal-notes`, `requests.assign`); talep yanıtında, olay
  geçmişinde ve sakin uçlarında **hiç** dönmez.
- Talebi açanın adı `people.read` ya da kendi talebi değilse `null` (kişisel veri).
"""

import datetime as dt
import uuid
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
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
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FORM_ERROR
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import (
    RESIDENT_CANCELLABLE,
    STATUS_LABELS,
    OperationRuleError,
    RequestEventKind,
    RequestPriority,
    RequestStatus,
    next_statuses,
)
from site_yonetim.domain.service_desk import HourType, QuoteStatus, RuleError, SlaState
from site_yonetim.domain.text import format_money_tr
from site_yonetim.models import (
    Outage,
    Request,
    RequestEvent,
    RequestFeedback,
    RequestInternalNote,
    ServiceQuote,
    ServiceQuoteLine,
)
from site_yonetim.services import departments as departments_svc
from site_yonetim.services import paid_services as paid_svc
from site_yonetim.services import requests as svc
from site_yonetim.services import service_desk as desk_svc

router = APIRouter(prefix="/sites/{slug}/requests", tags=["talep"])
RequestsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]
Paging = Annotated[PageParams, Depends()]

QUOTE_LABELS = {
    QuoteStatus.PENDING: "Sakin onayı bekliyor",
    QuoteStatus.ACCEPTED: "Sakin onayladı",
    QuoteStatus.REJECTED: "Sakin reddetti",
    QuoteStatus.EXTRA_PENDING: "Ek tutar onayı bekliyor",
    QuoteStatus.BILLED: "Cariye yansıdı",
}


def rule_error(exc: OperationRuleError) -> ApiError:
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    return ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        exc.code,
        exc.message,
        {exc.field: exc.message} if exc.field else None,
    )


def form_error(exc: RuleError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def _own_only(ctx: SiteContext) -> uuid.UUID | None:
    """`None` → tüm talepler; kişi kimliği → yalnız onun talepleri; ikisi de yoksa 403."""
    if ctx.access.can(Permission.REQUESTS_READ):
        return None
    if ctx.access.person_id is not None:
        return ctx.access.person_id
    raise ForbiddenError


def require(ctx: SiteContext, permission: Permission) -> None:
    if not ctx.access.can(permission):
        raise ForbiddenError


# --- Çıktı ---------------------------------------------------------------------------------


class RequestSlaOut(BaseModel):
    due_at: dt.datetime
    hour_type: HourType = Field(description="`24x7` · `business` (mesai dışında sayaç durur)")
    target_hours: int
    used_minutes: int
    remaining_minutes: int
    state: SlaState = Field(description="ok · warning (≥%80) · breached · met · missed")
    paused: bool = Field(description="çalışma saati dışında: sayaç durdu")


class RequestOutageBrief(BaseModel):
    """Ana olay özeti — sakin de görür (genel açıklama); diğer sakinlerin talepleri yok."""

    id: uuid.UUID
    title: str
    public_description: str
    status: str = Field(description="open · resolved")


class RequestFeedbackOut(BaseModel):
    solved: bool
    score: int | None
    comment: str | None
    by_name: str
    created_at: dt.datetime

    @classmethod
    def of(cls, f: RequestFeedback) -> RequestFeedbackOut:
        return cls(
            solved=f.solved,
            score=f.score,
            comment=f.comment,
            by_name=f.by_name,
            created_at=f.created_at,
        )


class QuoteLineOut(BaseModel):
    description: str
    qty: Decimal = Field(description="miktar (en çok 2 ondalık)")
    unit_price: Money
    total: Money


class ServiceQuoteOut(BaseModel):
    id: uuid.UUID
    request_id: uuid.UUID
    lines: list[QuoteLineOut]
    total: Money = Field(description="teklif toplamı")
    note: str | None
    status: QuoteStatus
    status_label: str
    actual_total: Money | None
    billed_total: Money | None = Field(description="cariye yansıyan (dönem dışı ücret)")
    created_by_name: str
    created_at: dt.datetime
    decided_at: dt.datetime | None

    @classmethod
    def of(cls, q: ServiceQuote, lines: list[ServiceQuoteLine]) -> ServiceQuoteOut:
        return cls(
            id=q.id,
            request_id=q.request_id,
            lines=[
                QuoteLineOut(
                    description=line.description,
                    qty=line.qty,
                    unit_price=line.unit_price,
                    total=line.total,
                )
                for line in lines
            ],
            total=q.total,
            note=q.note,
            status=QuoteStatus(q.status),
            status_label=QUOTE_LABELS[QuoteStatus(q.status)],
            actual_total=q.actual_total,
            billed_total=q.billed_total,
            created_by_name=q.created_by_name,
            created_at=q.created_at,
            decided_at=q.decided_at,
        )


class RequestOut(BaseModel):
    id: uuid.UUID
    number: int = Field(description="site içinde artan")
    title: str
    description: str | None
    category: str = Field(description="site kategorisinin anahtarı (GET …/request-categories)")
    category_name: str
    priority: RequestPriority
    status: RequestStatus
    status_label: str = Field(description="`İşlemde`")
    allowed_transitions: list[RequestStatus] = Field(
        description="personelin seçebileceği durumlar (requests.assign yoksa boş)"
    )
    can_cancel: bool = Field(description="sakin kendi talebini iptal edebilir mi (iş başlamadan)")
    unit_id: uuid.UUID | None
    unit_name: str | None = Field(description="`A-12`; ortak alanda null")
    location: str | None
    reported_by_person_id: uuid.UUID | None
    reporter_name: str | None = Field(description="people.read ya da kendi talebi; yoksa null")
    assigned_to: str | None
    department_id: uuid.UUID | None
    department_name: str | None
    due_at: dt.datetime | None
    sla: RequestSlaOut | None = Field(description="SLA kuralı olmayan eski talepte null")
    resolved_at: dt.datetime | None
    resolution: str | None
    auto_close_at: dt.datetime | None = Field(
        description="çözüldü talep sakin yanıt vermezse bu anda kapanır"
    )
    closed_at: dt.datetime | None
    outage: RequestOutageBrief | None
    feedback: RequestFeedbackOut | None = Field(description="son sakin değerlendirmesi")
    quote: ServiceQuoteOut | None = Field(description="son ücretli hizmet teklifi")
    created_by_name: str | None
    created_at: dt.datetime


class EventOut(BaseModel):
    id: uuid.UUID
    kind: RequestEventKind
    description: str
    actor_name: str | None
    new_status: RequestStatus | None
    created_at: dt.datetime

    @classmethod
    def of(cls, event: RequestEvent) -> EventOut:
        return cls(
            id=event.id,
            kind=RequestEventKind(event.kind),
            description=event.description,
            actor_name=event.actor_name,
            new_status=RequestStatus(event.new_status) if event.new_status else None,
            created_at=event.created_at,
        )


class RequestDetail(RequestOut):
    events: list[EventOut] = Field(description="eskiden yeniye olay geçmişi")


async def out(ctx: SiteContext, requests: list[Request], now: dt.datetime) -> list[RequestOut]:
    session = ctx.session
    ids = [r.id for r in requests]
    units = await svc.unit_names(session, {r.unit_id for r in requests if r.unit_id})
    people = await svc.person_names(
        session, {r.reported_by_person_id for r in requests if r.reported_by_person_id}
    )
    names = ctx.access.can(Permission.PEOPLE_READ)
    staff = ctx.access.can(Permission.REQUESTS_ASSIGN)
    me = ctx.access.person_id
    teams = await departments_svc.names(
        session, {r.department_id for r in requests if r.department_id}
    )
    categories = await svc.category_names(session, {r.category for r in requests})
    outage_ids = {r.outage_id for r in requests if r.outage_id}
    outages = (
        {o.id: o for o in await session.scalars(select(Outage).where(Outage.id.in_(outage_ids)))}
        if outage_ids
        else {}
    )
    feedback = await desk_svc.latest_feedback(session, ids)
    quotes = await paid_svc.quote_for(session, ids)
    lines = await paid_svc.lines_of(session, [q.id for q in quotes.values()])
    rows: list[RequestOut] = []
    for r in requests:
        sla = svc.sla_of(r, now)
        outage = outages.get(r.outage_id) if r.outage_id else None
        quote = quotes.get(r.id)
        state = svc.state_of(r)
        rows.append(
            RequestOut(
                id=r.id,
                number=r.number,
                title=r.title,
                description=r.description,
                category=r.category,
                category_name=categories.get(r.category, r.category),
                priority=RequestPriority(r.priority),
                status=RequestStatus(r.status),
                status_label=STATUS_LABELS[RequestStatus(r.status)],
                allowed_transitions=next_statuses(state) if staff else [],
                can_cancel=me is not None
                and r.reported_by_person_id == me
                and state.status in RESIDENT_CANCELLABLE,
                unit_id=r.unit_id,
                unit_name=units.get(r.unit_id) if r.unit_id else None,
                location=r.location,
                reported_by_person_id=r.reported_by_person_id,
                reporter_name=people.get(r.reported_by_person_id)
                if r.reported_by_person_id and (names or r.reported_by_person_id == me)
                else None,
                assigned_to=r.assigned_to,
                department_id=r.department_id,
                department_name=teams.get(r.department_id) if r.department_id else None,
                due_at=r.due_at,
                sla=RequestSlaOut(
                    due_at=sla.due_at,
                    hour_type=sla.hour_type,
                    target_hours=sla.target_hours,
                    used_minutes=sla.used_minutes,
                    remaining_minutes=sla.remaining_minutes,
                    state=sla.state,
                    paused=sla.paused,
                )
                if sla
                else None,
                resolved_at=r.resolved_at,
                resolution=r.resolution,
                auto_close_at=r.auto_close_at,
                closed_at=r.closed_at,
                outage=RequestOutageBrief(
                    id=outage.id,
                    title=outage.title,
                    public_description=outage.public_description,
                    status=outage.status,
                )
                if outage
                else None,
                feedback=RequestFeedbackOut.of(feedback[r.id]) if r.id in feedback else None,
                quote=ServiceQuoteOut.of(quote, lines.get(quote.id, [])) if quote else None,
                created_by_name=r.created_by_name,
                created_at=r.created_at,
            )
        )
    return rows


async def detail(ctx: SiteContext, request: Request, now: dt.datetime) -> RequestDetail:
    [base] = await out(ctx, [request], now)
    events = await svc.events(ctx.session, request.id)
    return RequestDetail(**base.model_dump(), events=[EventOut.of(e) for e in events])


async def visible(ctx: SiteContext, request_id: uuid.UUID, *, for_update: bool = False) -> Request:
    own = _own_only(ctx)
    request = await svc.get(ctx.session, request_id, for_update=for_update)
    if request is None or (own is not None and request.reported_by_person_id != own):
        raise NotFoundError("Talep bulunamadı.")
    return request


# --- Liste ve ayrıntı ----------------------------------------------------------------------


@router.get("", summary="Talepler (yeni üstte)")
async def list_requests(
    ctx: RequestsModule,
    paging: Paging,
    now: NowDep,
    status_filter: Annotated[RequestStatus | None, Query(alias="status")] = None,
    category: Annotated[str | None, Query(max_length=60)] = None,
    priority: RequestPriority | None = None,
    department_id: uuid.UUID | None = None,
    sla_breached: Annotated[
        bool | None, Query(description="true → süresi geçmiş açık talepler")
    ] = None,
) -> Page[RequestOut]:
    query = svc.list_query(
        status=status_filter,
        category=category or None,
        priority=priority,
        reporter=_own_only(ctx),
        department_id=department_id,
        sla_breached=sla_breached,
        now=now,
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await out(ctx, rows, now), page=paging.page, page_size=paging.page_size, total=total
    )


@router.get("/{request_id}", summary="Talep ayrıntısı ve geçmişi")
async def get_request(request_id: uuid.UUID, ctx: RequestsModule, now: NowDep) -> RequestDetail:
    return await detail(ctx, await visible(ctx, request_id), now)


class RequestCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    category: str = Field(default="other", max_length=60, description="kategori anahtarı")
    priority: RequestPriority | None = Field(
        default=None,
        description="boş → kategorinin varsayılanı; sakinde yok sayılır (öncelik kategoriden)",
    )
    unit_id: uuid.UUID | None = Field(default=None, description="ortak alan talebinde boş")
    location: str | None = Field(default=None, max_length=200, description="`B blok otopark`")
    reported_by_person_id: uuid.UUID | None = Field(
        default=None, description="personel başkası adına açarken; sakin için yok sayılır"
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Talep aç")
async def create_request(
    ctx: RequestsModule, body: RequestCreate, current: CurrentUserDep, today: TodayDep, now: NowDep
) -> Written[RequestDetail]:
    require(ctx, Permission.REQUESTS_CREATE)
    own = not ctx.access.can(Permission.REQUESTS_READ) and ctx.access.person_id is not None
    return await create_for(ctx, body, current, today, now, own=own)


async def create_for(
    ctx: SiteContext,
    body: RequestCreate,
    current: CurrentUserDep,
    today: dt.date,
    now: dt.datetime,
    *,
    own: bool,
) -> Written[RequestDetail]:
    """`own`: sakin — talep kendi adına, yalnız kendi bölümü ya da ortak alan için; öncelik
    kategoriden gelir (HIZ-01: aciliyet beyanı bilgi amaçlıdır)."""
    reporter = body.reported_by_person_id
    priority = body.priority
    if own:
        reporter = ctx.access.person_id
        priority = None
        own_units = await svc.person_unit_ids(ctx.session, reporter, today) if reporter else set()
        if body.unit_id is not None and body.unit_id not in own_units:
            message = "Yalnız kendi bölümünüz için talep açabilirsiniz."
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY, "unit_not_yours", message, {"unit_id": message}
            )
    try:
        request = await svc.create(
            ctx.session,
            svc.NewRequest(
                title=body.title,
                description=body.description,
                category=body.category,
                priority=priority,
                unit_id=body.unit_id,
                location=body.location,
                reported_by_person_id=reporter,
            ),
            actor=current.user.full_name,
            now=now,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await detail(ctx, request, now), message=f"#{request.number} numaralı talep açıldı."
    )


# --- Durum, atama, yorum -------------------------------------------------------------------


class StatusIn(BaseModel):
    status: RequestStatus
    resolution: str | None = Field(
        default=None, max_length=2000, description="çözüldü yapılırken çözüm, iptalde gerekçe"
    )


@router.post("/{request_id}/status", summary="Durumu değiştir (geçiş tablosuyla)")
async def change_status(
    request_id: uuid.UUID, ctx: RequestsModule, body: StatusIn, current: CurrentUserDep, now: NowDep
) -> Written[RequestDetail]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id, for_update=True)
    try:
        await svc.change_status(
            ctx.session,
            request,
            body.status,
            body.resolution,
            actor=current.user.full_name,
            now=now,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await detail(ctx, request, now),
        message=f"#{request.number} talebin durumu '{STATUS_LABELS[body.status]}' oldu.",
    )


class AssignIn(BaseModel):
    assignee: str = Field(min_length=2, max_length=120, description="personel ya da firma adı")


@router.post("/{request_id}/assign", summary="Ata (açık talep 'Atandı' olur)")
async def assign_request(
    request_id: uuid.UUID, ctx: RequestsModule, body: AssignIn, current: CurrentUserDep, now: NowDep
) -> Written[RequestDetail]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id, for_update=True)
    try:
        await svc.assign(ctx.session, request, body.assignee, actor=current.user.full_name, now=now)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await detail(ctx, request, now),
        message=f"#{request.number} talep {request.assigned_to} kişisine atandı.",
    )


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@router.post("/{request_id}/comments", status_code=status.HTTP_201_CREATED, summary="Yorum ekle")
async def add_comment(
    request_id: uuid.UUID,
    ctx: RequestsModule,
    body: CommentIn,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[RequestDetail]:
    """Yorumu talebi açan sakin de görür; yalnız personelin göreceği not için `/internal-notes`."""
    request = await visible(ctx, request_id)  # requests.read ya da kendi talebi
    try:
        await svc.comment(ctx.session, request, body.body, actor=current.user.full_name)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(data=await detail(ctx, request, now), message="Yorum eklendi.")


class DepartmentAssignIn(BaseModel):
    department_id: uuid.UUID | None = Field(description="null: departmanı kaldır")


@router.post("/{request_id}/department", summary="Talebi departmana yönlendir")
async def set_department(
    request_id: uuid.UUID,
    body: DepartmentAssignIn,
    ctx: RequestsModule,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[RequestDetail]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id, for_update=True)
    try:
        department = await departments_svc.assign(
            ctx.session, request, body.department_id, actor=current.user.full_name
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    data = await detail(ctx, request, now)
    await ctx.session.commit()
    message = (
        f'Talep "{department.name}" departmanına yönlendirildi.'
        if department
        else "Talebin departmanı kaldırıldı."
    )
    return Written(data=data, message=message)


# --- İç not (K26) --------------------------------------------------------------------------


class InternalNoteOut(BaseModel):
    id: uuid.UUID
    body: str
    author_name: str
    created_at: dt.datetime

    @classmethod
    def of(cls, n: RequestInternalNote) -> InternalNoteOut:
        return cls(id=n.id, body=n.body, author_name=n.author_name, created_at=n.created_at)


class InternalNoteIn(BaseModel):
    body: str = Field(max_length=2000)


@router.get("/{request_id}/internal-notes", summary="İç notlar (yalnız personel)")
async def internal_notes(request_id: uuid.UUID, ctx: RequestsModule) -> list[InternalNoteOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id)
    return [InternalNoteOut.of(n) for n in await desk_svc.notes(ctx.session, request.id)]


@router.post(
    "/{request_id}/internal-notes",
    status_code=status.HTTP_201_CREATED,
    summary="İç not ekle (sakin görmez; değişmez)",
)
async def add_internal_note(
    request_id: uuid.UUID, ctx: RequestsModule, body: InternalNoteIn, current: CurrentUserDep
) -> Written[InternalNoteOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id)
    try:
        note = await desk_svc.add_note(
            ctx.session,
            request,
            body.body,
            user_id=current.user.id,
            author=current.user.full_name,
        )
    except RuleError as exc:
        raise form_error(exc) from exc
    await ctx.session.commit()
    return Written(data=InternalNoteOut.of(note), message="İç not eklendi; sakin görmez.")


# --- Birleştirme önerisi (servis isteği 49) ------------------------------------------------


class SimilarRequestOut(BaseModel):
    id: uuid.UUID
    number: int
    title: str
    unit_name: str | None
    location: str | None
    created_at: dt.datetime


class OpenOutageBrief(BaseModel):
    id: uuid.UUID
    title: str
    request_count: int


class MergeSuggestionsOut(BaseModel):
    linked_outage: RequestOutageBrief | None
    similar: list[SimilarRequestOut]
    open_outages: list[OpenOutageBrief]
    window_hours: int


@router.get("/{request_id}/merge-suggestions", summary="Bu talepler aynı arıza olabilir")
async def merge_suggestions(request_id: uuid.UUID, ctx: RequestsModule) -> MergeSuggestionsOut:
    """Kural tabanlı öneri (yapay zekâ yok): aynı kategori + konum/blok + zaman penceresi."""
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id)
    found = await desk_svc.merge_suggestions(ctx.session, request)
    units = await svc.unit_names(ctx.session, {r.unit_id for r in found.similar if r.unit_id})
    linked = found.linked
    return MergeSuggestionsOut(
        linked_outage=RequestOutageBrief(
            id=linked.id,
            title=linked.title,
            public_description=linked.public_description,
            status=linked.status,
        )
        if linked
        else None,
        similar=[
            SimilarRequestOut(
                id=r.id,
                number=r.number,
                title=r.title,
                unit_name=units.get(r.unit_id) if r.unit_id else None,
                location=r.location,
                created_at=r.created_at,
            )
            for r in found.similar
        ],
        open_outages=[
            OpenOutageBrief(id=o.id, title=o.title, request_count=n) for o, n in found.open_outages
        ],
        window_hours=found.window_hours,
    )


# --- Ücretli hizmet teklifi (servis isteği 51) ---------------------------------------------


class QuoteLineIn(BaseModel):
    description: str = Field(max_length=200)
    qty: Decimal = Field(description="miktar > 0, en çok 2 ondalık")
    unit_price: Money


class QuoteIn(BaseModel):
    lines: list[QuoteLineIn] = Field(max_length=50)
    note: str | None = Field(default=None, max_length=200)


@router.post(
    "/{request_id}/quotes", status_code=status.HTTP_201_CREATED, summary="Ücretli hizmet teklifi"
)
async def create_quote(
    request_id: uuid.UUID, ctx: RequestsModule, body: QuoteIn, current: CurrentUserDep, now: NowDep
) -> Written[ServiceQuoteOut]:
    require(ctx, Permission.REQUESTS_ASSIGN)
    request = await visible(ctx, request_id, for_update=True)
    try:
        quote = await paid_svc.create_quote(
            ctx.session,
            request,
            [(line.description, line.qty, line.unit_price) for line in body.lines],
            body.note,
            actor=current.user.full_name,
            now=now,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    except RuleError as exc:
        raise form_error(exc) from exc
    lines = (await paid_svc.lines_of(ctx.session, [quote.id]))[quote.id]
    data = ServiceQuoteOut.of(quote, lines)
    await ctx.session.commit()
    return Written(
        data=data,
        message=f"Teklif sakine iletildi ({format_money_tr(quote.total)}); "
        "onaylamadan iş ücretli yapılmaz.",
    )
