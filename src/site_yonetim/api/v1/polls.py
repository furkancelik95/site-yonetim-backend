"""Anket — frontend servis isteği 15 (backend #43).

Modül `surveys` (kapalıysa 404). Personel okuma `announcements.read`, açma/kapatma
`polls.manage` (Yönetici). Sakin uçları bölüm bağıyla: oy bölüm adına verilir.

Gizli oy: hiçbir uç kimin neye oy verdiğini döndürmez; personel yalnız toplamı görür. Sakin,
oy vermeden ve anket açıkken `votes`/`total_votes` alanlarını `null` alır.
"""

import datetime as dt
import uuid
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
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.resident import resident
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.management import PollAudience, PollStatus
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Poll
from site_yonetim.services import polls as svc
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}/polls", tags=["anket"])
resident_router = APIRouter(prefix="/sites/{slug}/resident/polls", tags=["sakin"])
Paging = Annotated[PageParams, Depends()]
Module = Annotated[SiteContext, Depends(require_module(ModuleKey.SURVEYS))]


async def _reader(ctx: Module) -> SiteContext:
    # Sakin rolünde de `announcements.read` var; ama personel ucu ara sonucu her zaman
    # gösterir. Sakin kendi ucunu kullanır (oy vermeden sonuç yok).
    if not ctx.access.can(Permission.ANNOUNCEMENTS_READ) or ctx.access.person_id is not None:
        raise ForbiddenError
    return ctx


async def _manager(ctx: Module) -> SiteContext:
    if not ctx.access.can(Permission.POLLS_MANAGE):
        raise ForbiddenError
    return ctx


async def _resident(ctx: Module) -> SiteContext:
    return await resident(ctx)


Reader = Annotated[SiteContext, Depends(_reader)]
Manager = Annotated[SiteContext, Depends(_manager)]
Resident = Annotated[SiteContext, Depends(_resident)]


def _conflict(exc: OperationRuleError) -> ApiError:
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


class PollIn(BaseModel):
    question: str | None = Field(default=None, max_length=600)
    description: str | None = Field(default=None, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=20)
    audience: str | None = Field(default=None, max_length=20)
    ends_on: dt.date | None = None


class OptionOut(BaseModel):
    id: uuid.UUID
    label: str
    votes: int | None = Field(description="sakinde oy vermeden ve anket açıkken null")


class PollOut(BaseModel):
    id: uuid.UUID
    question: str
    description: str | None
    options: list[OptionOut]
    audience: PollAudience
    ends_on: dt.date
    status: PollStatus
    total_votes: int | None = Field(description="oy veren bölüm sayısı")
    created_by: str | None
    created_at: dt.datetime


class MyVote(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    option_id: uuid.UUID | None = Field(description="bu bölüm adına verilen oy; yoksa null")


class ResidentPollOut(PollOut):
    my_votes: list[MyVote] = Field(description="oy verebileceğim bölümler")


async def _out(ctx: SiteContext, rows: list[Poll], today: dt.date) -> list[PollOut]:
    ids = [p.id for p in rows]
    options, counts = await svc.options(ctx.session, ids), await svc.counts(ctx.session, ids)
    return [
        PollOut(
            id=p.id,
            question=p.question,
            description=p.description,
            options=[
                OptionOut(id=o.id, label=o.label, votes=counts.get(o.id, 0)) for o in options[p.id]
            ],
            audience=PollAudience(p.audience),
            ends_on=p.ends_on,
            status=svc.status_of(p, today),
            total_votes=sum(counts.get(o.id, 0) for o in options[p.id]),
            created_by=p.created_by_name,
            created_at=p.created_at,
        )
        for p in rows
    ]


# --- Personel -----------------------------------------------------------------------


@router.get("", summary="Anketler (yeni üstte)")
async def list_polls(
    ctx: Reader,
    paging: Paging,
    today: TodayDep,
    status_: Annotated[PollStatus | None, Query(alias="status")] = None,
) -> Page[PollOut]:
    query = svc.list_query(status_, today)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _out(ctx, rows, today),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="Anket aç")
async def create_poll(
    body: PollIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[PollOut]:
    try:
        poll = await svc.create(
            ctx.session,
            question=body.question,
            description=body.description,
            options=body.options,
            audience=body.audience,
            ends_on=body.ends_on,
            today=today,
            created_by=current.user.full_name,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    await ctx.session.refresh(poll, ["created_at"])
    [out] = await _out(ctx, [poll], today)
    await ctx.session.commit()
    return Written(data=out, message="Anket açıldı; sakinler kendi ekranlarında görüyor.")


@router.post("/{poll_id}/close", summary="Anketi erken kapat")
async def close_poll(
    poll_id: uuid.UUID, ctx: Manager, today: TodayDep, now: NowDep
) -> Written[PollOut]:
    poll = await svc.get(ctx.session, poll_id, lock=True)
    if poll is None:
        raise NotFoundError("Anket bulunamadı.")
    try:
        await svc.close(ctx.session, poll, today=today, now=now)
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    [out] = await _out(ctx, [poll], today)
    await ctx.session.commit()
    return Written(data=out, message="Anket kapatıldı; sonuç sakinlere açıldı.")


# --- Sakin --------------------------------------------------------------------------


class VoteIn(BaseModel):
    unit_id: uuid.UUID
    option_id: uuid.UUID


class VoteOut(BaseModel):
    option_id: uuid.UUID


def _person(ctx: SiteContext) -> uuid.UUID:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı zaten engeller
        raise ForbiddenError
    return person


@resident_router.get("", summary="Açık anketler ve son 30 günde kapananlar")
async def my_polls(ctx: Resident, today: TodayDep, now: NowDep) -> list[ResidentPollOut]:
    units = await svc.person_units(ctx.session, _person(ctx), today)
    rows = list(await ctx.session.scalars(svc.resident_query(today, now).limit(100)))
    names = await unit_names(ctx.session, set(units))
    votes = await svc.unit_votes(ctx.session, [p.id for p in rows], list(units))
    found: list[ResidentPollOut] = []
    for poll, out in zip(rows, await _out(ctx, rows, today), strict=True):
        mine = [
            MyVote(unit_id=u, unit_name=names.get(u, "—"), option_id=votes.get((poll.id, u)))
            for u in sorted(svc.eligible_units(poll, units), key=lambda u: names.get(u, ""))
        ]
        visible = out.status is PollStatus.CLOSED or any(v.option_id for v in mine)
        if not visible:
            out.total_votes = None
            out.options = [o.model_copy(update={"votes": None}) for o in out.options]
        found.append(ResidentPollOut(**out.model_dump(), my_votes=mine))
    return found


@resident_router.post("/{poll_id}/vote", summary="Bölüm adına oy ver (değiştirilemez)")
async def vote(
    poll_id: uuid.UUID, body: VoteIn, ctx: Resident, today: TodayDep
) -> Written[VoteOut]:
    poll = await svc.get(ctx.session, poll_id)
    if poll is None:
        raise NotFoundError("Anket bulunamadı.")
    try:
        await svc.vote(
            ctx.session,
            poll,
            person_id=_person(ctx),
            unit_id=body.unit_id,
            option_id=body.option_id,
            today=today,
        )
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except PermissionError as exc:
        raise ApiError(HTTPStatus.FORBIDDEN, "not_eligible", str(exc)) from exc
    except FormFieldError as exc:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", exc.fields["option_id"], exc.fields
        ) from exc
    await ctx.session.commit()
    return Written(data=VoteOut(option_id=body.option_id), message="Oyunuz kaydedildi.")
