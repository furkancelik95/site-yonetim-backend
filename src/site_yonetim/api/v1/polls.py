"""Anket — frontend servis isteği 15 (backend #43), Yazılım Dokümanı ILT-03, PM kararı K31.

Modül `surveys` (kapalıysa 404). Personel okuma `announcements.read`, açma/kapatma ve isimli
döküm `polls.manage` (Yönetici). Sakin uçları bölüm bağıyla.

Gizlilik anket bazında (K31): anonim ankette hiçbir uç kimin neye yanıt verdiğini döndürmez;
isimli ankette `GET …/polls/{id}/responses` yalnız `polls.manage` (kişisel veri; açılışı denetim
kaydına yazılır). Sakin, yanıt vermeden ve anket açıkken sonuç alanlarını `null` alır.
Anket resmî oylama yerine geçmez (`notice`).
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
from site_yonetim.domain.management import (
    POLL_NOTICE,
    PollAudience,
    PollQuestionKind,
    PollStatus,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Poll, PollQuestion
from site_yonetim.models.audit import AuditAction, record
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


class PollQuestionIn(BaseModel):
    text: str | None = Field(default=None, max_length=600)
    kind: str | None = Field(
        default=None, max_length=20, description="single · multiple · rating · text"
    )
    options: list[str] = Field(default_factory=list, max_length=20)
    required: bool = True


class PollIn(BaseModel):
    question: str | None = Field(default=None, max_length=600, description="anketin başlığı")
    description: str | None = Field(default=None, max_length=2000)
    options: list[str] = Field(
        default_factory=list, max_length=20, description="eski biçim: tek sorunun seçenekleri"
    )
    questions: list[PollQuestionIn] | None = Field(
        default=None, max_length=30, description="çoklu soru; verilirse `options` kullanılmaz"
    )
    audience: str | None = Field(default=None, max_length=20)
    block_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    ends_on: dt.date | None = None
    is_anonymous: bool = Field(default=True, description="anonim: kim yanıtladı tutulmaz")
    one_per_unit: bool = Field(default=True, description="bölüm başına tek yanıt")


class OptionOut(BaseModel):
    id: uuid.UUID
    label: str
    votes: int | None = Field(description="sakinde oy vermeden ve anket açıkken null")


class PollRatingOut(BaseModel):
    average: str | None = Field(description='ortalama puan, metin (ör. "4.25")')
    counts: list[int] = Field(description="1–5 puan sayıları")


class PollQuestionOut(BaseModel):
    id: uuid.UUID
    order: int
    text: str
    kind: PollQuestionKind
    required: bool
    options: list[OptionOut]
    answered: int | None = Field(description="bu soruyu yanıtlayan sayısı")
    rating: PollRatingOut | None = Field(description="puan sorusunda sonuç")
    texts: list[str] | None = Field(description="metin yanıtları — yalnız anket yöneticisine")


class PollOut(BaseModel):
    id: uuid.UUID
    question: str
    description: str | None
    options: list[OptionOut] = Field(description="ilk sorunun seçenekleri (eski biçim)")
    questions: list[PollQuestionOut]
    audience: PollAudience
    block_ids: list[uuid.UUID]
    is_anonymous: bool
    one_per_unit: bool
    ends_on: dt.date
    status: PollStatus
    total_votes: int | None = Field(description="yanıt veren bölüm (ya da kişi) sayısı")
    created_by: str | None
    created_at: dt.datetime
    notice: str = Field(description="resmî oylama değildir uyarısı")


class MyVote(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    option_id: uuid.UUID | None = Field(description="bu bölüm adına ilk sorudaki seçim; yoksa null")
    voted: bool = Field(description="bu bölüm adına yanıt verildi mi")


class ResidentPollOut(PollOut):
    my_votes: list[MyVote] = Field(description="bölüm başına ankette: yanıt verebileceğim bölümler")
    can_respond: bool = Field(description="şu an yanıt verebilir miyim")
    responded: bool = Field(description="yanıt verdim mi (bölüm başına ankette: bir bölüm adına)")


def _rating(result: svc.QuestionResult) -> PollRatingOut:
    counts = [result.ratings.get(i, 0) for i in range(1, 6)]
    total = sum(counts)
    average = sum(i * c for i, c in enumerate(counts, start=1)) / total if total else None
    return PollRatingOut(average=f"{average:.2f}" if average is not None else None, counts=counts)


async def _out(
    ctx: SiteContext, rows: list[Poll], today: dt.date, *, texts: bool = False
) -> list[PollOut]:
    ids = [p.id for p in rows]
    results = await svc.results(ctx.session, ids)
    answers = await svc.text_answers(ctx.session, ids) if texts else {}
    found = []
    for p in rows:
        r = results[p.id]
        questions = [
            PollQuestionOut(
                id=q.question.id,
                order=q.question.order,
                text=q.question.text,
                kind=PollQuestionKind(q.question.kind),
                required=q.question.required,
                options=[
                    OptionOut(id=o.id, label=o.label, votes=q.option_votes.get(o.id, 0))
                    for o in q.options
                ],
                answered=q.answered,
                rating=_rating(q) if q.question.kind == PollQuestionKind.RATING.value else None,
                texts=answers.get(q.question.id, [])
                if texts and q.question.kind == PollQuestionKind.TEXT.value
                else None,
            )
            for q in r.questions
        ]
        found.append(
            PollOut(
                id=p.id,
                question=p.question,
                description=p.description,
                options=questions[0].options if questions else [],
                questions=questions,
                audience=PollAudience(p.audience),
                block_ids=list(p.block_ids or []),
                is_anonymous=p.is_anonymous,
                one_per_unit=p.one_per_unit,
                ends_on=p.ends_on,
                status=svc.status_of(p, today),
                total_votes=r.ballots,
                created_by=p.created_by_name,
                created_at=p.created_at,
                notice=POLL_NOTICE,
            )
        )
    return found


def _hidden(out: PollOut) -> PollOut:
    """Sakin: yanıt vermeden ve anket açıkken sonuç alanları null."""

    def strip(o: OptionOut) -> OptionOut:
        return o.model_copy(update={"votes": None})

    questions = [
        q.model_copy(
            update={"options": [strip(o) for o in q.options], "answered": None, "rating": None}
        )
        for q in out.questions
    ]
    return out.model_copy(
        update={
            "total_votes": None,
            "options": [strip(o) for o in out.options],
            "questions": questions,
        }
    )


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
        items=await _out(ctx, rows, today, texts=ctx.access.can(Permission.POLLS_MANAGE)),
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
            questions=[
                svc.QuestionInput(q.text, q.kind, q.options, q.required) for q in body.questions
            ]
            if body.questions
            else None,
            is_anonymous=body.is_anonymous,
            one_per_unit=body.one_per_unit,
            block_ids=body.block_ids,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    notified = await svc.notify_audience(ctx.session, poll, today)
    await ctx.session.refresh(poll, ["created_at"])
    [out] = await _out(ctx, [poll], today, texts=True)
    await ctx.session.commit()
    privacy = "anonim" if poll.is_anonymous else "isimli (yanıt verenler kayda geçer)"
    return Written(
        data=out,
        message=f"Anket açıldı ({privacy}); {notified} kişiye bildirim gitti.",
    )


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
    [out] = await _out(ctx, [poll], today, texts=True)
    await ctx.session.commit()
    return Written(data=out, message="Anket kapatıldı; sonuç sakinlere açıldı.")


class PollNamedAnswer(BaseModel):
    question_id: uuid.UUID
    option_ids: list[uuid.UUID]
    rating: int | None
    text: str | None


class PollNamedResponse(BaseModel):
    person_name: str
    unit_name: str | None
    at: dt.datetime
    answers: list[PollNamedAnswer]


@router.get("/{poll_id}/responses", summary="İsimli anket: kim ne yanıtladı (yalnız yetkili)")
async def named_responses(poll_id: uuid.UUID, ctx: Manager) -> list[PollNamedResponse]:
    poll = await svc.get(ctx.session, poll_id)
    if poll is None:
        raise NotFoundError("Anket bulunamadı.")
    try:
        rows = await svc.named_responses(ctx.session, poll)
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    names = await unit_names(ctx.session, {r.unit_id for r in rows if r.unit_id})
    # Kişisel veri dökümü açıldı (docs/09 §6).
    record(ctx.session, AuditAction.EXPORT, "polls", poll.id, {"named_responses": len(rows)})
    await ctx.session.commit()
    return [
        PollNamedResponse(
            person_name=r.person_name,
            unit_name=names.get(r.unit_id) if r.unit_id else None,
            at=r.at,
            answers=[
                PollNamedAnswer(
                    question_id=a.question_id, option_ids=a.option_ids, rating=a.rating, text=a.text
                )
                for a in r.answers
            ],
        )
        for r in rows
    ]


# --- Sakin --------------------------------------------------------------------------


class PollAnswerIn(BaseModel):
    question_id: uuid.UUID
    option_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    rating: int | None = None
    text: str | None = Field(default=None, max_length=2000)


class VoteIn(BaseModel):
    unit_id: uuid.UUID | None = Field(default=None, description="bölüm başına ankette zorunlu")
    option_id: uuid.UUID | None = Field(default=None, description="eski biçim: ilk sorunun seçimi")
    answers: list[PollAnswerIn] | None = Field(default=None, max_length=30)


class VoteOut(BaseModel):
    option_id: uuid.UUID | None


def _person(ctx: SiteContext) -> uuid.UUID:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı zaten engeller
        raise ForbiddenError
    return person


@resident_router.get("", summary="Açık anketler ve son 30 günde kapananlar")
async def my_polls(ctx: Resident, today: TodayDep, now: NowDep) -> list[ResidentPollOut]:
    person = _person(ctx)
    units = await svc.person_units(ctx.session, person, today)
    rows = list(await ctx.session.scalars(svc.resident_query(today, now).limit(100)))
    names = await unit_names(ctx.session, set(units))
    ids = [p.id for p in rows]
    votes = await svc.unit_votes(ctx.session, ids, list(units))
    responded_polls = await svc.person_responded(ctx.session, ids, person)
    found: list[ResidentPollOut] = []
    for poll, out in zip(rows, await _out(ctx, rows, today), strict=True):
        eligible = svc.eligible_units(poll, units)
        mine = (
            [
                MyVote(
                    unit_id=u,
                    unit_name=names.get(u, "—"),
                    option_id=votes.get((poll.id, u)),
                    voted=(poll.id, u) in votes,
                )
                for u in sorted(eligible, key=lambda u: names.get(u, ""))
            ]
            if poll.one_per_unit
            else []
        )
        responded = any(v.voted for v in mine) if poll.one_per_unit else poll.id in responded_polls
        is_open = out.status is PollStatus.OPEN
        can_respond = is_open and (
            any(not v.voted for v in mine)
            if poll.one_per_unit
            else bool(eligible) and not responded
        )
        if not (out.status is PollStatus.CLOSED or responded):
            out = _hidden(out)
        found.append(
            ResidentPollOut(
                **out.model_dump(), my_votes=mine, can_respond=can_respond, responded=responded
            )
        )
    return found


@resident_router.post("/{poll_id}/vote", summary="Yanıt ver / oy ver (değiştirilemez)")
async def vote(
    poll_id: uuid.UUID, body: VoteIn, ctx: Resident, today: TodayDep
) -> Written[VoteOut]:
    poll = await svc.get(ctx.session, poll_id)
    if poll is None:
        raise NotFoundError("Anket bulunamadı.")
    legacy = body.answers is None
    if legacy:
        first = await ctx.session.scalar(
            select(PollQuestion.id).where(PollQuestion.poll_id == poll.id, PollQuestion.order == 1)
        )
        answers = (
            [svc.AnswerInput(first, [body.option_id] if body.option_id else [])] if first else []
        )
    else:
        answers = [
            svc.AnswerInput(a.question_id, a.option_ids, a.rating, a.text)
            for a in body.answers or []
        ]
    try:
        await svc.vote(
            ctx.session,
            poll,
            person_id=_person(ctx),
            unit_id=body.unit_id,
            answers=answers,
            today=today,
            legacy=legacy,
        )
    except OperationRuleError as exc:
        raise _conflict(exc) from exc
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except PermissionError as exc:
        raise ApiError(HTTPStatus.FORBIDDEN, "not_eligible", str(exc)) from exc
    except FormFieldError as exc:
        first_message = next(iter(exc.fields.values()))
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", first_message, exc.fields
        ) from exc
    await ctx.session.commit()
    chosen = (
        body.option_id
        if legacy
        else next((a.option_ids[0] for a in body.answers or [] if a.option_ids), None)
    )
    return Written(data=VoteOut(option_id=chosen), message="Oyunuz kaydedildi.")
