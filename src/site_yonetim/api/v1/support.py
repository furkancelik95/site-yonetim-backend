"""Platform destek — site tarafı (Yazılım Dokümanı PYN-04, PYN-06, PLT-12; servis istekleri
82 ve 84; PM kararı K12).

- Destek biletleri: yönetim rolleri (`members.manage`, `finance.read` ya da `modules.manage`)
  açar ve görür; ekran bağlamı + iz kimliği (istek kimliği) otomatik eklenir.
- Destek oturumu: platform ekibinin talebini tesis yöneticisi (`members.manage`) onaylar,
  reddeder ya da süre dolmadan bitirir. Aktif oturum bandı sitedeki her kullanıcıya görünür.
- Sakin olarak görüntüle: `support.view_as_resident` (hiçbir şablonda varsayılan değil); en çok
  30 dakika, gerekçe zorunlu, salt okunur; sakine bildirim tesis ayarı (varsayılan: bildir).
"""

import datetime as dt
import re
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.core.logging import request_id_var
from site_yonetim.domain.access import AccessMode, Permission
from site_yonetim.domain.support import (
    BODY_MAX,
    BODY_MIN,
    CONTEXT_AGENT_MAX,
    CONTEXT_PATH_MAX,
    CONTEXT_VERSION_MAX,
    MESSAGE_MAX,
    MESSAGE_MIN,
    REASON_MAX,
    TICKET_PERMISSIONS,
    TITLE_MAX,
    TITLE_MIN,
    VIEW_AS_REASON_MIN,
    SupportSessionStatus,
    TicketCategory,
    TicketPriority,
    TicketStatus,
    ViewAsStatus,
    first_breached,
    resolve_breached,
    session_effective_status,
    ticket_number,
    view_as_effective_status,
)
from site_yonetim.models import (
    Person,
    Site,
    SupportSession,
    SupportTicket,
    SupportTicketMessage,
    ViewAsSession,
)
from site_yonetim.services import support as svc

router = APIRouter(prefix="/sites/{slug}", tags=["destek"])
Paging = Annotated[PageParams, Depends()]
_CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def rule_error(exc: svc.SupportRuleError) -> ApiError:
    if exc.field:
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            exc.message,
            {exc.field: exc.message},
        )
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


async def ticket_access(ctx: SiteContextDep) -> SiteContext:
    """Bileti yönetim rolleri açar ve görür (19.3; servis isteği 82 "Yetki")."""
    if not any(ctx.access.can(p) for p in TICKET_PERMISSIONS):
        raise ForbiddenError("Destek biletini yönetim rolleri açar.")
    return ctx


TicketAccess = Annotated[SiteContext, Depends(ticket_access)]


# --- Şemalar ---------------------------------------------------------------------------


class TicketContextIn(BaseModel):
    path: str = Field(default="", max_length=CONTEXT_PATH_MAX)
    app_version: str = Field(default="", max_length=CONTEXT_VERSION_MAX)
    user_agent: str = Field(default="", max_length=CONTEXT_AGENT_MAX)
    correlation_id: str | None = Field(
        default=None,
        max_length=64,
        description="kullanıcının gördüğü hatanın istek kimliği; yoksa bu isteğin kimliği",
    )


class SupportTicketIn(BaseModel):
    title: str = Field(min_length=TITLE_MIN, max_length=TITLE_MAX)
    body: str = Field(min_length=BODY_MIN, max_length=BODY_MAX)
    category: TicketCategory
    priority: TicketPriority
    context: TicketContextIn = Field(default_factory=TicketContextIn)

    @field_validator("title", "body", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TicketMessageIn(BaseModel):
    body: str = Field(min_length=MESSAGE_MIN, max_length=MESSAGE_MAX)

    @field_validator("body", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TicketContextOut(BaseModel):
    path: str
    app_version: str
    user_agent: str
    correlation_id: str


class TicketMessageOut(BaseModel):
    by: str
    side: Literal["customer", "support"]
    body: str
    at: dt.datetime


class TicketSlaOut(BaseModel):
    first_due: dt.datetime
    resolve_due: dt.datetime
    first_breached: bool
    resolve_breached: bool


class SupportTicketOut(BaseModel):
    id: uuid.UUID
    number: str = Field(description='"DST-0001" — platform genelinde tek')
    site_slug: str
    site_name: str
    category: TicketCategory
    priority: TicketPriority
    title: str
    context: TicketContextOut
    status: TicketStatus
    messages: list[TicketMessageOut]
    created_by: str
    created_at: dt.datetime
    first_response_at: dt.datetime | None
    resolved_at: dt.datetime | None
    sla: TicketSlaOut
    support_tier: str = Field(description="business_hours · critical_24_7 · full_24_7")
    on_call: bool = Field(description="kritik bilet nöbet hattına düştü (paket 7/24 ise)")


def ticket_out(
    ticket: SupportTicket, site: Site, messages: list[SupportTicketMessage], now: dt.datetime
) -> SupportTicketOut:
    return SupportTicketOut(
        id=ticket.id,
        number=ticket_number(ticket.number),
        site_slug=site.slug,
        site_name=site.name,
        category=TicketCategory(ticket.category),
        priority=TicketPriority(ticket.priority),
        title=ticket.title,
        context=TicketContextOut(
            path=ticket.context_path,
            app_version=ticket.app_version,
            user_agent=ticket.user_agent,
            correlation_id=ticket.correlation_id,
        ),
        status=TicketStatus(ticket.status),
        messages=[
            TicketMessageOut(
                by=m.author_name,
                side="support" if m.side == "support" else "customer",
                body=m.body,
                at=m.created_at,
            )
            for m in messages
        ],
        created_by=ticket.created_by_name,
        created_at=ticket.created_at,
        first_response_at=ticket.first_response_at,
        resolved_at=ticket.resolved_at,
        sla=TicketSlaOut(
            first_due=ticket.first_due,
            resolve_due=ticket.resolve_due,
            first_breached=first_breached(ticket.first_due, ticket.first_response_at, now),
            resolve_breached=resolve_breached(ticket.resolve_due, ticket.resolved_at, now),
        ),
        support_tier=ticket.support_tier,
        on_call=ticket.on_call,
    )


async def _one(ctx: SiteContext, ticket: SupportTicket, now: dt.datetime) -> SupportTicketOut:
    messages = await svc.messages_of(ctx.session, [ticket.id])
    return ticket_out(ticket, ctx.site, messages[ticket.id], now)


# --- Destek biletleri (82) ----------------------------------------------------------


@router.get("/support-tickets", summary="Sitenin destek biletleri")
async def list_tickets(
    ctx: TicketAccess,
    paging: Paging,
    now: NowDep,
    status_: Annotated[TicketStatus | None, Query(alias="status")] = None,
) -> Page[SupportTicketOut]:
    query = select(SupportTicket)
    if status_ is not None:
        query = query.where(SupportTicket.status == status_.value)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        await ctx.session.scalars(
            query.order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
            .offset(paging.offset)
            .limit(paging.page_size)
        )
    )
    messages = await svc.messages_of(ctx.session, [t.id for t in rows])
    return Page(
        items=[ticket_out(t, ctx.site, messages[t.id], now) for t in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.post("/support-tickets", status_code=status.HTTP_201_CREATED, summary="Destek bileti aç")
async def create_ticket(
    body: SupportTicketIn, ctx: TicketAccess, current: CurrentUserDep, now: NowDep
) -> Written[SupportTicketOut]:
    given = body.context.correlation_id
    correlation = given if given and _CORRELATION.match(given) else (request_id_var.get() or "")
    ticket = await svc.open_ticket(
        ctx.session,
        ctx.site,
        user_id=current.user.id,
        user_name=current.user.full_name,
        title=body.title,
        body=body.body,
        category=body.category,
        priority=body.priority,
        path=body.context.path,
        app_version=body.context.app_version,
        user_agent=body.context.user_agent,
        correlation_id=correlation,
        now=now,
    )
    out = await _one(ctx, ticket, now)
    await ctx.session.commit()
    number = ticket_number(ticket.number)
    if ticket.on_call:
        message = f"{number} açıldı. Kritik öncelik: nöbet hattına iletildi."
    elif body.priority is TicketPriority.CRITICAL:
        message = f"{number} açıldı. Paketiniz 7/24 destek içermediği için mesai içinde yanıtlanır."
    else:
        message = f"{number} açıldı."
    return Written(data=out, message=message)


async def _ticket(ctx: SiteContext, ticket_id: uuid.UUID) -> SupportTicket:
    ticket = await svc.get_ticket(ctx.session, ticket_id)
    if ticket is None:
        raise NotFoundError("Bilet bulunamadı.")
    return ticket


@router.post("/support-tickets/{ticket_id}/messages", summary="Bilete yaz (yeniden açar)")
async def write_ticket(
    ticket_id: uuid.UUID,
    body: TicketMessageIn,
    ctx: TicketAccess,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[SupportTicketOut]:
    ticket = await _ticket(ctx, ticket_id)
    try:
        await svc.customer_message(
            ctx.session,
            ticket,
            user_id=current.user.id,
            user_name=current.user.full_name,
            body=body.body,
        )
    except svc.SupportRuleError as exc:
        raise rule_error(exc) from exc
    out = await _one(ctx, ticket, now)
    await ctx.session.commit()
    return Written(data=out, message="Mesaj gönderildi.")


@router.post("/support-tickets/{ticket_id}/close", summary="Çözülen bileti kapat")
async def close_ticket(
    ticket_id: uuid.UUID, ctx: TicketAccess, now: NowDep
) -> Written[SupportTicketOut]:
    ticket = await _ticket(ctx, ticket_id)
    try:
        await svc.close_ticket(ctx.session, ticket, now=now)
    except svc.SupportRuleError as exc:
        raise rule_error(exc) from exc
    out = await _one(ctx, ticket, now)
    await ctx.session.commit()
    return Written(data=out, message="Bilet kapandı. Teşekkürler.")


# --- Destek oturumu (84) -------------------------------------------------------------


class SupportSessionOut(BaseModel):
    id: uuid.UUID
    site_slug: str
    site_name: str
    reason: str
    hours: int
    requested_by: str
    requested_at: dt.datetime
    status: SupportSessionStatus
    decided_by: str | None
    decided_at: dt.datetime | None
    ends_at: dt.datetime | None
    reject_reason: str | None
    ended_by: str | None = Field(description="süre dolmadan bitiren tesis yöneticisi")


def session_out(row: SupportSession, site: Site, now: dt.datetime) -> SupportSessionOut:
    return SupportSessionOut(
        id=row.id,
        site_slug=site.slug,
        site_name=site.name,
        reason=row.reason,
        hours=row.hours,
        requested_by=row.requested_by_name,
        requested_at=row.created_at,
        status=session_effective_status(SupportSessionStatus(row.status), row.ends_at, now),
        decided_by=row.decided_by_name,
        decided_at=row.decided_at,
        ends_at=row.ends_at,
        reject_reason=row.reject_reason,
        ended_by=row.ended_by_name,
    )


class SupportSessionRejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=REASON_MAX)


async def session_decider(ctx: SiteContextDep) -> SiteContext:
    if not ctx.access.can(Permission.MEMBERS_MANAGE):
        raise ForbiddenError("Destek oturumunu tesis yöneticisi onaylar.")
    return ctx


SessionDecider = Annotated[SiteContext, Depends(session_decider)]


@router.get("/support-sessions", summary="Destek oturumu talepleri")
async def list_sessions(ctx: TicketAccess, paging: Paging, now: NowDep) -> Page[SupportSessionOut]:
    total = await ctx.session.scalar(select(func.count(SupportSession.id)))
    rows = await ctx.session.scalars(
        select(SupportSession)
        .order_by(SupportSession.created_at.desc(), SupportSession.id.desc())
        .offset(paging.offset)
        .limit(paging.page_size)
    )
    return Page(
        items=[session_out(r, ctx.site, now) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total or 0,
    )


@router.get(
    "/support-sessions/active",
    summary="Aktif destek oturumu (bant) — yoksa null",
)
async def active_session(ctx: SiteContextDep, now: NowDep) -> SupportSessionOut | None:
    row = await svc.active_session(ctx.session, now)
    return session_out(row, ctx.site, now) if row is not None else None


async def _session(ctx: SiteContext, session_id: uuid.UUID) -> SupportSession:
    row = await svc.get_session(ctx.session, session_id)
    if row is None:
        raise NotFoundError("Oturum bulunamadı.")
    return row


@router.post("/support-sessions/{session_id}/approve", summary="Destek oturumunu onayla")
async def approve_session(
    session_id: uuid.UUID, ctx: SessionDecider, current: CurrentUserDep, now: NowDep
) -> Written[SupportSessionOut]:
    row = await _session(ctx, session_id)
    try:
        await svc.approve_session(
            ctx.session, row, user_id=current.user.id, user_name=current.user.full_name, now=now
        )
    except svc.SupportRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=session_out(row, ctx.site, now),
        message=(
            f"Destek oturumu {row.hours} saat için açıldı (salt okunur); işlemler denetim "
            'kaydına "destek oturumu" olarak yazılır.'
        ),
    )


@router.post("/support-sessions/{session_id}/reject", summary="Destek oturumunu reddet")
async def reject_session(
    session_id: uuid.UUID,
    ctx: SessionDecider,
    current: CurrentUserDep,
    now: NowDep,
    body: SupportSessionRejectIn | None = None,
) -> Written[SupportSessionOut]:
    row = await _session(ctx, session_id)
    reason = (body.reason or "").strip() or None if body else None
    try:
        await svc.reject_session(
            ctx.session,
            row,
            user_id=current.user.id,
            user_name=current.user.full_name,
            reason=reason,
            now=now,
        )
    except svc.SupportRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(
        data=session_out(row, ctx.site, now), message="Oturum talebi reddedildi; oturum açılmadı."
    )


@router.post("/support-sessions/{session_id}/end", summary="Aktif destek oturumunu bitir")
async def end_session(
    session_id: uuid.UUID, ctx: SessionDecider, current: CurrentUserDep, now: NowDep
) -> Written[SupportSessionOut]:
    row = await _session(ctx, session_id)
    try:
        await svc.end_session(ctx.session, row, user_name=current.user.full_name, now=now)
    except svc.SupportRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(data=session_out(row, ctx.site, now), message="Destek oturumu sonlandırıldı.")


# --- Sakin olarak görüntüle (PLT-12) --------------------------------------------------


class ViewAsIn(BaseModel):
    reason: str = Field(min_length=VIEW_AS_REASON_MIN, max_length=REASON_MAX)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ViewAsSessionOut(BaseModel):
    id: uuid.UUID
    person_id: uuid.UUID
    person_name: str
    viewer_name: str
    reason: str
    started_at: dt.datetime
    ends_at: dt.datetime
    status: ViewAsStatus
    resident_notified: bool = Field(description="sakine bildirim gitti (tesis ayarı)")


def view_as_out(row: ViewAsSession, person: Person, now: dt.datetime) -> ViewAsSessionOut:
    return ViewAsSessionOut(
        id=row.id,
        person_id=row.person_id,
        person_name=person.full_name,
        viewer_name=row.viewer_name,
        reason=row.reason,
        started_at=row.created_at,
        ends_at=row.ends_at,
        status=view_as_effective_status(ViewAsStatus(row.status), row.ends_at, now),
        resident_notified=row.resident_notified,
    )


@router.post(
    "/people/{person_id}/view-as-sessions",
    status_code=status.HTTP_201_CREATED,
    summary="Sakin olarak görüntüle (30 dk, salt okunur)",
)
async def start_view_as(
    person_id: uuid.UUID, body: ViewAsIn, ctx: SiteContextDep, current: CurrentUserDep, now: NowDep
) -> Written[ViewAsSessionOut]:
    if ctx.access.mode is AccessMode.VIEW_AS or not ctx.access.can(
        Permission.SUPPORT_VIEW_AS_RESIDENT
    ):
        raise ForbiddenError("Sakin olarak görüntüleme yetkiniz yok.")
    person = await ctx.session.scalar(
        select(Person).where(Person.id == person_id, Person.merged_into_id.is_(None))
    )
    if person is None:
        raise NotFoundError("Kişi bulunamadı.")
    support_session = (
        ctx.access.session_id if ctx.access.mode is AccessMode.SUPPORT_SESSION else None
    )
    row = await svc.start_view_as(
        ctx.session,
        person,
        viewer_id=current.user.id,
        viewer_name=current.user.full_name,
        reason=body.reason,
        support_session_id=support_session,
        now=now,
    )
    out = view_as_out(row, person, now)
    await ctx.session.commit()
    notified = " Sakine bildirim gönderildi." if row.resident_notified else ""
    return Written(
        data=out,
        message=(
            f"{person.full_name} olarak 30 dakika salt okunur görüntülüyorsunuz; açtığınız "
            f"ekranlar denetim kaydına yazılır.{notified}"
        ),
    )


@router.delete("/view-as-sessions/{session_id}", summary="Sakin olarak görüntülemeden çık")
async def end_view_as(
    session_id: uuid.UUID, ctx: SiteContextDep, current: CurrentUserDep, now: NowDep
) -> Written[ViewAsSessionOut]:
    row = await svc.find_view_as(ctx.session, session_id)
    if row is None or row.viewer_id != current.user.id:
        raise NotFoundError("Görüntüleme oturumu bulunamadı.")
    person = await ctx.session.get(Person, row.person_id)
    if person is None:  # pragma: no cover - bileşik yabancı anahtar
        raise NotFoundError
    await svc.end_view_as(ctx.session, row, now=now)
    out = view_as_out(row, person, now)
    await ctx.session.commit()
    return Written(data=out, message="Sakin olarak görüntüleme kapatıldı.")


class ViewAsSettingsIn(BaseModel):
    notify_resident: bool


class ViewAsSettingsOut(BaseModel):
    notify_resident: bool = Field(description='"sakine bildir" (varsayılan: açık)')


ManageMembers = Annotated[SiteContext, Depends(require_permission(Permission.MEMBERS_MANAGE))]


@router.get("/settings/view-as", summary="Sakin olarak görüntüle ayarı")
async def get_view_as_settings(ctx: ManageMembers) -> ViewAsSettingsOut:
    return ViewAsSettingsOut(notify_resident=await svc.notify_setting(ctx.session))


@router.put("/settings/view-as", summary="Sakin olarak görüntüle ayarını kaydet")
async def put_view_as_settings(
    body: ViewAsSettingsIn, ctx: ManageMembers
) -> Written[ViewAsSettingsOut]:
    await svc.set_notify_setting(ctx.session, notify_resident=body.notify_resident)
    await ctx.session.commit()
    return Written(
        data=ViewAsSettingsOut(notify_resident=body.notify_resident), message="Ayar kaydedildi."
    )
