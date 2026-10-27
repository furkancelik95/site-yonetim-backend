"""Görevli rutin görevleri — Yazılım Dokümanı OPS-06 (servis isteği 58), PM kararı K29.

Çekirdek (modül yok). Günün görevleri ve kapatma `requests.read`; şablon yönetimi
`requests.assign`. Personelin kendi görevleri (`/me/tasks`): giriş hesabı bir personel kartına
bağlı kullanıcı (K29, `staff_members.user_id`), yalnız kendi kartına atanmış şablonlar; izin
gerekmez — kartı yoksa 403.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.expenses import StoreDep
from site_yonetim.api.v1.ops_shared import fields_error, ops_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import (
    CompletionStatus,
    OpsError,
    TaskFrequency,
    TaskStatus,
    task_rate,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import StaffMember, TaskCompletion, TaskTemplate
from site_yonetim.services import routine_tasks as svc
from site_yonetim.services import staff as staff_svc
from site_yonetim.services.facility_common import PhotoSink

router = APIRouter(prefix="/sites/{slug}", tags=["rutin görevler"])
Reader = Annotated[SiteContext, Depends(require_permission(Permission.REQUESTS_READ))]
Assigner = Annotated[SiteContext, Depends(require_permission(Permission.REQUESTS_ASSIGN))]
TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


def _hm(value: dt.time) -> str:
    return f"{value:%H:%M}"


class TaskTemplateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    frequency: str | None = Field(
        default=None, max_length=10, description="daily · weekdays · monthly"
    )
    weekdays: list[int] | None = Field(default=None, max_length=7, description="0 = pazartesi")
    month_day: int | None = Field(default=None, description="1–28")
    time_from: dt.time | None = Field(default=None, description='"08:00"')
    time_to: dt.time | None = None
    location: str | None = Field(default=None, max_length=200)
    assignee: str | None = Field(default=None, max_length=200, description="rol ya da kişi")
    staff_member_id: uuid.UUID | None = Field(default=None, description="K29: personel kartı")
    photo_required: bool | None = None
    checklist_template_id: uuid.UUID | None = None
    is_active: bool | None = None


class TaskTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    frequency: TaskFrequency
    weekdays: list[int]
    month_day: int | None
    time_from: str = Field(description='"08:00"')
    time_to: str
    location: str | None
    assignee: str
    staff_member_id: uuid.UUID | None
    staff_name: str | None
    photo_required: bool
    checklist_template_id: uuid.UUID | None
    is_active: bool

    @classmethod
    def of(cls, t: TaskTemplate, staff: dict[uuid.UUID, str]) -> TaskTemplateOut:
        return cls(
            id=t.id,
            name=t.name,
            description=t.description,
            frequency=TaskFrequency(t.frequency),
            weekdays=list(t.weekdays),
            month_day=t.month_day,
            time_from=_hm(t.time_from),
            time_to=_hm(t.time_to),
            location=t.location,
            assignee=t.assignee,
            staff_member_id=t.staff_member_id,
            staff_name=staff.get(t.staff_member_id) if t.staff_member_id else None,
            photo_required=t.photo_required,
            checklist_template_id=t.checklist_template_id,
            is_active=t.is_active,
        )


class TaskCompletionOut(BaseModel):
    template_id: uuid.UUID
    date: dt.date
    status: CompletionStatus
    reason: str | None
    note: str | None
    photo_id: uuid.UUID | None = Field(description="GET …/operation-photos/{id}")
    by: str
    at: dt.datetime

    @classmethod
    def of(cls, c: TaskCompletion) -> TaskCompletionOut:
        return cls(
            template_id=c.template_id,
            date=c.task_date,
            status=CompletionStatus(c.status),
            reason=c.reason,
            note=c.note,
            photo_id=c.photo_file_id,
            by=c.by_name,
            at=c.created_at,
        )


class DayTaskOut(BaseModel):
    template: TaskTemplateOut
    done: TaskCompletionOut | None
    status: TaskStatus = Field(description="upcoming · due · late · done · failed")


async def _staff_names(ctx: SiteContext, templates: list[TaskTemplate]) -> dict[uuid.UUID, str]:
    ids = {t.staff_member_id for t in templates if t.staff_member_id}
    if not ids:
        return {}
    rows = await ctx.session.execute(
        select(StaffMember.id, StaffMember.full_name).where(StaffMember.id.in_(ids))
    )
    return {row[0]: row[1] for row in rows}


async def _day_out(ctx: SiteContext, tasks: list[svc.DayTask]) -> list[DayTaskOut]:
    names = await _staff_names(ctx, [t.template for t in tasks])
    return [
        DayTaskOut(
            template=TaskTemplateOut.of(t.template, names),
            done=TaskCompletionOut.of(t.done) if t.done else None,
            status=t.status,
        )
        for t in tasks
    ]


@router.get("/task-templates", summary="Görev şablonları (saate göre)")
async def list_templates(ctx: Reader) -> list[TaskTemplateOut]:
    rows = list(await ctx.session.scalars(svc.templates_query().limit(500)))
    names = await _staff_names(ctx, rows)
    return [TaskTemplateOut.of(t, names) for t in rows]


def _data(body: TaskTemplateIn, current: svc.TemplateData | None) -> svc.TemplateData:
    sent = body.model_fields_set
    values = {k: getattr(body, k) for k in TaskTemplateIn.model_fields}
    if current is None:
        return svc.TemplateData(**values)
    return svc.TemplateData(**{k: values[k] if k in sent else getattr(current, k) for k in values})


@router.post("/task-templates", status_code=status.HTTP_201_CREATED, summary="Görev şablonu ekle")
async def create_template(body: TaskTemplateIn, ctx: Assigner) -> Written[TaskTemplateOut]:
    try:
        template = await svc.save_template(ctx.session, _data(body, None), None)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = TaskTemplateOut.of(template, await _staff_names(ctx, [template]))
    await ctx.session.commit()
    return Written(data=out, message=f"{template.name} görevi eklendi.")


@router.patch("/task-templates/{template_id}", summary="Görev şablonunu düzenle / pasifleştir")
async def update_template(
    template_id: uuid.UUID, body: TaskTemplateIn, ctx: Assigner
) -> Written[TaskTemplateOut]:
    template = await svc.get_template(ctx.session, template_id)
    if template is None:
        raise NotFoundError("Görev şablonu bulunamadı.")
    try:
        await svc.save_template(ctx.session, _data(body, svc.current_template(template)), template)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = TaskTemplateOut.of(template, await _staff_names(ctx, [template]))
    await ctx.session.commit()
    return Written(data=out, message=f"{template.name} güncellendi.")


@router.get("/tasks", summary="Günün görevleri (şablonlardan; saat sırasıyla)")
async def list_tasks(
    ctx: Reader,
    today: TodayDep,
    now: NowDep,
    date: dt.date | None = None,
    staff_member_id: Annotated[uuid.UUID | None, Query(description="personel süzgeci")] = None,
) -> list[DayTaskOut]:
    tasks = await svc.day_tasks(
        ctx.session,
        date or today,
        today=today,
        now=svc.local_now(now),
        staff_member_id=staff_member_id,
    )
    return await _day_out(ctx, tasks)


class CompleteIn(BaseModel):
    date: dt.date | None = Field(default=None, description="boş: bugün")
    status: str | None = Field(
        default=CompletionStatus.DONE.value, max_length=10, description="done · failed"
    )
    reason: str | None = Field(default=None, max_length=400)
    note: str | None = Field(default=None, max_length=400)
    photo: str | None = Field(default=None, max_length=3_000_000, description="veri adresi")


async def _complete(
    ctx: SiteContext,
    template_id: uuid.UUID,
    body: CompleteIn,
    *,
    store: StoreDep,
    by: svc.Closer,
    today: dt.date,
    only_staff: uuid.UUID | None = None,
) -> Written[TaskCompletionOut]:
    template = await svc.get_template(ctx.session, template_id)
    if only_staff is not None and template is not None and template.staff_member_id != only_staff:
        template = None  # başkasının görevi "yok"tur
    sink = PhotoSink(store)
    try:
        record = await svc.complete(
            ctx.session,
            sink,
            template,
            day=body.date or today,
            status=body.status,
            reason=body.reason,
            note=body.note,
            photo=body.photo,
            by=by,
            today=today,
        )
        await ctx.session.refresh(record, ["created_at"])
        out = TaskCompletionOut.of(record)
        await ctx.session.commit()
    except FormFieldError as exc:
        sink.discard()
        raise fields_error(exc) from exc
    except OpsError as exc:
        sink.discard()
        raise ops_error(exc) from exc
    except BaseException:
        sink.discard()
        raise
    name = template.name if template else ""
    message = (
        f"{name} tamamlandı."
        if record.status == CompletionStatus.DONE.value
        else f"{name} tamamlanamadı olarak kaydedildi."
    )
    return Written(data=out, message=message)


@router.post(
    "/tasks/{template_id}/complete",
    status_code=status.HTTP_201_CREATED,
    summary="Görevi kapat (tamamlandı / tamamlanamadı)",
    responses={409: {"description": "`already_done`"}, 422: {"description": "`photo_required`"}},
)
async def complete_task(
    template_id: uuid.UUID,
    body: CompleteIn,
    ctx: Reader,
    current: CurrentUserDep,
    store: StoreDep,
    today: TodayDep,
) -> Written[TaskCompletionOut]:
    own = await staff_svc.own_card(ctx.session, current.user.id)
    return await _complete(
        ctx,
        template_id,
        body,
        store=store,
        by=svc.Closer(current.user.full_name, current.user.id, own.id if own else None),
        today=today,
    )


class TaskReportRowOut(BaseModel):
    template_id: uuid.UUID
    name: str
    assignee: str
    expected: int
    done: int
    failed: int
    rate: int | None = Field(description="tamamlanan / beklenen (%)")


class TaskReportOut(BaseModel):
    from_: dt.date = Field(serialization_alias="from")
    to: dt.date
    rows: list[TaskReportRowOut]
    rate: int | None


@router.get(
    "/tasks/report",
    summary="Son 7 gün tamamlanma oranı (görev bazında)",
)
async def task_report(ctx: Reader, today: TodayDep) -> TaskReportOut:
    first, last, rows = await svc.report(ctx.session, today=today)
    expected = sum(r.expected for r in rows)
    done = sum(r.done for r in rows)
    return TaskReportOut(
        from_=first,
        to=last,
        rows=[
            TaskReportRowOut(
                template_id=r.template.id,
                name=r.template.name,
                assignee=r.template.assignee,
                expected=r.expected,
                done=r.done,
                failed=r.failed,
                rate=r.rate,
            )
            for r in rows
        ],
        rate=task_rate(expected, done),
    )


# --- Personelin kendi görevleri (K29) -----------------------------------------------


async def _own_card(ctx: SiteContextDep, current: CurrentUserDep) -> StaffMember:
    staff = await staff_svc.own_card(ctx.session, current.user.id)
    if staff is None:
        raise ForbiddenError("Hesabınıza bağlı personel kartı yok; site yönetimine başvurun.")
    return staff


OwnCard = Annotated[StaffMember, Depends(_own_card)]


@router.get("/me/tasks", summary="Görevlerim — kartıma atanmış günün görevleri")
async def my_tasks(
    ctx: SiteContextDep, own: OwnCard, today: TodayDep, now: NowDep, date: dt.date | None = None
) -> list[DayTaskOut]:
    tasks = await svc.day_tasks(
        ctx.session, date or today, today=today, now=svc.local_now(now), staff_member_id=own.id
    )
    return await _day_out(ctx, tasks)


@router.post(
    "/me/tasks/{template_id}/complete",
    status_code=status.HTTP_201_CREATED,
    summary="Görevimi kapat",
)
async def complete_my_task(
    template_id: uuid.UUID,
    body: CompleteIn,
    ctx: SiteContextDep,
    own: OwnCard,
    current: CurrentUserDep,
    store: StoreDep,
    today: TodayDep,
) -> Written[TaskCompletionOut]:
    return await _complete(
        ctx,
        template_id,
        body,
        store=store,
        by=svc.Closer(current.user.full_name, current.user.id, own.id),
        today=today,
        only_staff=own.id,
    )
