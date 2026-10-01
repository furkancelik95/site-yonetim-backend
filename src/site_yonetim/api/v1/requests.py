"""Talep / arıza uçları — docs/06 §2.10 (modül: `requests`; kapalıysa 404).

- `requests.read` olan tüm talepleri görür; izni olmayan sakin yalnız **kendi** taleplerini
  (docs/05 §6). Başkasının talebi "yok"tur (404).
- Sakin talebi kendi adına ve yalnız kendi bölümü (ya da ortak alan) için açar.
- Durum ve atama `requests.assign`; çözüldü/kapandı yapılırken `resolution` zorunlu.
- Talebi açanın adı `people.read` ya da kendi talebi değilse `null` (kişisel veri).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import (
    STATUS_LABELS,
    OperationRuleError,
    RequestCategory,
    RequestEventKind,
    RequestPriority,
    RequestStatus,
)
from site_yonetim.models import Request, RequestEvent
from site_yonetim.services import requests as svc

router = APIRouter(prefix="/sites/{slug}/requests", tags=["talep"])
RequestsModule = Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]
Paging = Annotated[PageParams, Depends()]


def rule_error(exc: OperationRuleError) -> ApiError:
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    return ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        exc.code,
        exc.message,
        {exc.field: exc.message} if exc.field else None,
    )


def _own_only(ctx: SiteContext) -> uuid.UUID | None:
    """`None` → tüm talepler; kişi kimliği → yalnız onun talepleri; ikisi de yoksa 403."""
    if ctx.access.can(Permission.REQUESTS_READ):
        return None
    if ctx.access.person_id is not None:
        return ctx.access.person_id
    raise ForbiddenError


def _require(ctx: SiteContext, permission: Permission) -> None:
    if not ctx.access.can(permission):
        raise ForbiddenError


class RequestOut(BaseModel):
    id: uuid.UUID
    number: int = Field(description="site içinde artan")
    title: str
    description: str | None
    category: RequestCategory
    priority: RequestPriority
    status: RequestStatus
    status_label: str = Field(description="`İşlemde`")
    unit_id: uuid.UUID | None
    unit_name: str | None = Field(description="`A-12`; ortak alanda null")
    location: str | None
    reported_by_person_id: uuid.UUID | None
    reporter_name: str | None = Field(description="people.read ya da kendi talebi; yoksa null")
    assigned_to: str | None
    due_at: dt.datetime | None
    resolved_at: dt.datetime | None
    resolution: str | None
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


async def _out(ctx: SiteContext, requests: list[Request]) -> list[RequestOut]:
    session = ctx.session
    units = await svc.unit_names(session, {r.unit_id for r in requests if r.unit_id})
    people = await svc.person_names(
        session, {r.reported_by_person_id for r in requests if r.reported_by_person_id}
    )
    names = ctx.access.can(Permission.PEOPLE_READ)
    me = ctx.access.person_id
    return [
        RequestOut(
            id=r.id,
            number=r.number,
            title=r.title,
            description=r.description,
            category=RequestCategory(r.category),
            priority=RequestPriority(r.priority),
            status=RequestStatus(r.status),
            status_label=STATUS_LABELS[RequestStatus(r.status)],
            unit_id=r.unit_id,
            unit_name=units.get(r.unit_id) if r.unit_id else None,
            location=r.location,
            reported_by_person_id=r.reported_by_person_id,
            reporter_name=people.get(r.reported_by_person_id)
            if r.reported_by_person_id and (names or r.reported_by_person_id == me)
            else None,
            assigned_to=r.assigned_to,
            due_at=r.due_at,
            resolved_at=r.resolved_at,
            resolution=r.resolution,
            created_by_name=r.created_by_name,
            created_at=r.created_at,
        )
        for r in requests
    ]


async def _detail(ctx: SiteContext, request: Request) -> RequestDetail:
    [base] = await _out(ctx, [request])
    events = await svc.events(ctx.session, request.id)
    return RequestDetail(**base.model_dump(), events=[EventOut.of(e) for e in events])


async def _visible(ctx: SiteContext, request_id: uuid.UUID, *, for_update: bool = False) -> Request:
    own = _own_only(ctx)
    request = await svc.get(ctx.session, request_id, for_update=for_update)
    if request is None or (own is not None and request.reported_by_person_id != own):
        raise NotFoundError("Talep bulunamadı.")
    return request


@router.get("", summary="Talepler (yeni üstte)")
async def list_requests(
    ctx: RequestsModule,
    paging: Paging,
    status_filter: Annotated[RequestStatus | None, Query(alias="status")] = None,
    category: RequestCategory | None = None,
    priority: RequestPriority | None = None,
) -> Page[RequestOut]:
    query = svc.list_query(
        status=status_filter, category=category, priority=priority, reporter=_own_only(ctx)
    )
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _out(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


@router.get("/{request_id}", summary="Talep ayrıntısı ve geçmişi")
async def get_request(request_id: uuid.UUID, ctx: RequestsModule) -> RequestDetail:
    return await _detail(ctx, await _visible(ctx, request_id))


class RequestCreate(BaseModel):
    title: str = Field(min_length=3, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    category: RequestCategory = RequestCategory.OTHER
    priority: RequestPriority = RequestPriority.NORMAL
    unit_id: uuid.UUID | None = Field(default=None, description="ortak alan talebinde boş")
    location: str | None = Field(default=None, max_length=200, description="`B blok otopark`")
    reported_by_person_id: uuid.UUID | None = Field(
        default=None, description="personel başkası adına açarken; sakin için yok sayılır"
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Talep aç")
async def create_request(
    ctx: RequestsModule, body: RequestCreate, current: CurrentUserDep, today: TodayDep
) -> Written[RequestDetail]:
    _require(ctx, Permission.REQUESTS_CREATE)
    reporter = body.reported_by_person_id
    if not ctx.access.can(Permission.REQUESTS_READ) and ctx.access.person_id is not None:
        # Sakin: talep kendi adına, yalnız kendi bölümü ya da ortak alan için.
        reporter = ctx.access.person_id
        own_units = await svc.person_unit_ids(ctx.session, reporter, today)
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
                priority=body.priority,
                unit_id=body.unit_id,
                location=body.location,
                reported_by_person_id=reporter,
            ),
            actor=current.user.full_name,
        )
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _detail(ctx, request), message=f"#{request.number} numaralı talep açıldı."
    )


class StatusIn(BaseModel):
    status: RequestStatus
    resolution: str | None = Field(
        default=None, max_length=2000, description="çözüldü/kapandı yapılırken zorunlu"
    )


@router.post("/{request_id}/status", summary="Durumu değiştir")
async def change_status(
    request_id: uuid.UUID, ctx: RequestsModule, body: StatusIn, current: CurrentUserDep, now: NowDep
) -> Written[RequestDetail]:
    _require(ctx, Permission.REQUESTS_ASSIGN)
    request = await _visible(ctx, request_id, for_update=True)
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
        data=await _detail(ctx, request),
        message=f"#{request.number} talebin durumu '{STATUS_LABELS[body.status]}' oldu.",
    )


class AssignIn(BaseModel):
    assignee: str = Field(min_length=2, max_length=120, description="personel ya da firma adı")


@router.post("/{request_id}/assign", summary="Ata")
async def assign_request(
    request_id: uuid.UUID, ctx: RequestsModule, body: AssignIn, current: CurrentUserDep
) -> Written[RequestDetail]:
    _require(ctx, Permission.REQUESTS_ASSIGN)
    request = await _visible(ctx, request_id, for_update=True)
    try:
        await svc.assign(ctx.session, request, body.assignee, actor=current.user.full_name)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=await _detail(ctx, request),
        message=f"#{request.number} talep {request.assigned_to} kişisine atandı.",
    )


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


@router.post("/{request_id}/comments", status_code=status.HTTP_201_CREATED, summary="Yorum ekle")
async def add_comment(
    request_id: uuid.UUID, ctx: RequestsModule, body: CommentIn, current: CurrentUserDep
) -> Written[RequestDetail]:
    request = await _visible(ctx, request_id)  # requests.read ya da kendi talebi
    try:
        await svc.comment(ctx.session, request, body.body, actor=current.user.full_name)
    except OperationRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(data=await _detail(ctx, request), message="Yorum eklendi.")
