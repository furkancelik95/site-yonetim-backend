"""Toplantılar ve genel kurul — frontend servis isteği 14 (backend #42).

Modül `general-assembly` (kapalıysa 404). Okuma `meetings.read` (Yönetici, Yönetim Kurulu,
Denetçi); planlama, karar, iptal `meetings.manage` (Yönetici). Yazmalar denetim kaydında.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, require_module
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.management import AgendaResult, MeetingKind, MeetingStatus
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Meeting
from site_yonetim.services import meetings as svc

router = APIRouter(prefix="/sites/{slug}/meetings", tags=["toplantı"])
Paging = Annotated[PageParams, Depends()]
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.GENERAL_ASSEMBLY))]


async def _reader(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.MEETINGS_READ):
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.MEETINGS_MANAGE):
        raise ForbiddenError
    return ctx


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]


class MeetingIn(BaseModel):
    kind: str | None = Field(default=None, max_length=30)
    title: str | None = Field(default=None, max_length=400)
    scheduled_at: dt.datetime | None = None
    location: str | None = Field(default=None, max_length=400)
    agenda: list[str] = Field(default_factory=list, max_length=60)


class DecisionIn(BaseModel):
    id: uuid.UUID
    result: str | None = Field(default=None, max_length=20)
    decision: str | None = Field(default=None, max_length=4000)
    votes_for: int | None = Field(default=None, le=1_000_000)
    votes_against: int | None = Field(default=None, le=1_000_000)
    votes_abstain: int | None = Field(default=None, le=1_000_000)


class DecisionsIn(BaseModel):
    attendance_note: str | None = Field(default=None, max_length=1000)
    items: list[DecisionIn] = Field(default_factory=list, max_length=60)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class AgendaItemOut(BaseModel):
    id: uuid.UUID
    order: int
    title: str
    result: AgendaResult | None
    decision: str | None
    votes_for: int | None
    votes_against: int | None
    votes_abstain: int | None


class MeetingOut(BaseModel):
    id: uuid.UUID
    number: int
    kind: MeetingKind
    title: str
    scheduled_at: dt.datetime
    location: str
    status: MeetingStatus
    agenda: list[AgendaItemOut]
    attendance_note: str | None
    held_at: dt.datetime | None
    cancel_reason: str | None
    created_by: str | None
    created_at: dt.datetime


async def _out(ctx: SiteContext, rows: list[Meeting]) -> list[MeetingOut]:
    agenda = await svc.agenda(ctx.session, [m.id for m in rows])
    return [
        MeetingOut(
            id=m.id,
            number=m.number,
            kind=MeetingKind(m.kind),
            title=m.title,
            scheduled_at=m.scheduled_at,
            location=m.location,
            status=MeetingStatus(m.status),
            agenda=[
                AgendaItemOut(
                    id=a.id,
                    order=a.order,
                    title=a.title,
                    result=AgendaResult(a.result) if a.result else None,
                    decision=a.decision,
                    votes_for=a.votes_for,
                    votes_against=a.votes_against,
                    votes_abstain=a.votes_abstain,
                )
                for a in agenda[m.id]
            ],
            attendance_note=m.attendance_note,
            held_at=m.held_at,
            cancel_reason=m.cancel_reason,
            created_by=m.created_by_name,
            created_at=m.created_at,
        )
        for m in rows
    ]


def _conflict(exc: OperationRuleError) -> ApiError:
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


async def _found(ctx: SiteContext, meeting_id: uuid.UUID, *, lock: bool = False) -> Meeting:
    meeting = await svc.get(ctx.session, meeting_id, lock=lock)
    if meeting is None:
        raise NotFoundError("Toplantı bulunamadı.")
    return meeting


@router.get("", summary="Toplantılar (tarihe göre yeniden eskiye)")
async def list_meetings(
    ctx: Reader,
    paging: Paging,
    status_: Annotated[MeetingStatus | None, Query(alias="status")] = None,
) -> Page[MeetingOut]:
    query = svc.list_query(status_)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _out(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


@router.get("/{meeting_id}", summary="Toplantı ayrıntısı (gündem ve kararlarla)")
async def get_meeting(meeting_id: uuid.UUID, ctx: Reader) -> MeetingOut:
    [out] = await _out(ctx, [await _found(ctx, meeting_id)])
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="Toplantı planla")
async def create_meeting(
    body: MeetingIn, ctx: Manager, current: CurrentUserDep
) -> Written[MeetingOut]:
    try:
        meeting = await svc.create(
            ctx.session,
            kind=body.kind,
            title=body.title,
            scheduled_at=body.scheduled_at,
            location=body.location,
            agenda=body.agenda,
            created_by=current.user.full_name,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.refresh(meeting, ["created_at"])
    [out] = await _out(ctx, [meeting])
    await ctx.session.commit()
    return Written(data=out, message=f'"{meeting.title}" planlandı.')


@router.post("/{meeting_id}/decisions", summary="Kararları kaydet (tek seferlik; kayıt kilitlenir)")
async def record_decisions(
    meeting_id: uuid.UUID, body: DecisionsIn, ctx: Manager, now: NowDep
) -> Written[MeetingOut]:
    meeting = await _found(ctx, meeting_id, lock=True)
    try:
        await svc.decide(
            ctx.session,
            meeting,
            attendance_note=body.attendance_note,
            decisions=[svc.Decision(**i.model_dump()) for i in body.items],
            now=now,
        )
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    [out] = await _out(ctx, [meeting])
    await ctx.session.commit()
    return Written(
        data=out,
        message="Kararlar kaydedildi; toplantı yapıldı olarak işaretlendi. "
        "Kayıt artık değiştirilemez.",
    )


@router.post("/{meeting_id}/cancel", summary="Toplantıyı iptal et (gerekçe zorunlu)")
async def cancel_meeting(
    meeting_id: uuid.UUID, body: CancelIn, ctx: Manager
) -> Written[MeetingOut]:
    meeting = await _found(ctx, meeting_id, lock=True)
    try:
        await svc.cancel(ctx.session, meeting, reason=body.reason)
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    except FormFieldError as exc:
        raise form_error(exc) from exc
    [out] = await _out(ctx, [meeting])
    await ctx.session.commit()
    return Written(data=out, message=f'"{meeting.title}" iptal edildi.')
