"""Vardiya şablonları ve haftalık plan — servis isteği 36 (Yazılım Dokümanı IK-02).

Modül `staff` (kapalıysa 404). Okuma `people.read`, yazma `staff.manage`. Plandaki izin günleri
için izin **türü** yalnız `staff.manage`'e görünür (raporlu izin sağlık verisi olabilir); diğer
okuyucular türsüz "izinli" görür.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, TodayDep
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.staff import Manager, Reader
from site_yonetim.core.errors import ConflictError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.staffing import Conflict, ConflictKind, LeaveType, format_time, week_start
from site_yonetim.models import ShiftAssignment, ShiftTemplate
from site_yonetim.services import shifts as svc

router = APIRouter(prefix="/sites/{slug}", tags=["vardiya"])


def _rule(exc: svc.ShiftRuleError) -> ConflictError:
    return ConflictError(exc.code, exc.message)


# --- Şablonlar ------------------------------------------------------------------------------


class ShiftTemplateIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    start: str | None = Field(default=None, max_length=5, description='"08:00"')
    end: str | None = Field(default=None, max_length=5, description='"16:00"; ≤ start → ertesi gün')
    break_minutes: int | None = Field(default=None, ge=-1, le=10_000)
    position: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None


class ShiftTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    start: str
    end: str
    break_minutes: int
    position: str | None
    location: str | None
    is_active: bool
    overnight: bool = Field(description="bitiş ertesi güne taşar")

    @classmethod
    def of(cls, t: ShiftTemplate) -> ShiftTemplateOut:
        start, end = svc.minutes_of(t.start_time), svc.minutes_of(t.end_time)
        return cls(
            id=t.id,
            name=t.name,
            start=format_time(start),
            end=format_time(end),
            break_minutes=t.break_minutes,
            position=t.position,
            location=t.location,
            is_active=t.is_active,
            overnight=end <= start,
        )


@router.get("/shift-templates", summary="Vardiya şablonları (dizi)")
async def list_templates(ctx: Reader) -> list[ShiftTemplateOut]:
    return [ShiftTemplateOut.of(t) for t in await svc.templates(ctx.session)]


def _template_data(body: ShiftTemplateIn, current: ShiftTemplate | None) -> svc.TemplateData:
    base = ShiftTemplateOut.of(current).model_dump() if current else {}
    given = body.model_dump(exclude_unset=True)
    merged = {**base, **given}
    return svc.TemplateData(
        name=merged.get("name"),
        start=merged.get("start"),
        end=merged.get("end"),
        break_minutes=merged.get("break_minutes"),
        position=merged.get("position"),
        location=merged.get("location"),
        is_active=merged.get("is_active"),
    )


@router.post("/shift-templates", status_code=status.HTTP_201_CREATED, summary="Şablon ekle")
async def create_template(body: ShiftTemplateIn, ctx: Manager) -> Written[ShiftTemplateOut]:
    try:
        item = await svc.save_template(ctx.session, _template_data(body, None), item=None)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = ShiftTemplateOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.name} şablonu eklendi.")


@router.patch("/shift-templates/{template_id}", summary="Şablonu düzenle / kapat (kısmi)")
async def update_template(
    template_id: uuid.UUID, body: ShiftTemplateIn, ctx: Manager
) -> Written[ShiftTemplateOut]:
    item = await ctx.session.get(ShiftTemplate, template_id, with_for_update=True)
    if item is None:
        raise NotFoundError("Vardiya şablonu bulunamadı.")
    try:
        await svc.save_template(ctx.session, _template_data(body, item), item=item)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = ShiftTemplateOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message=f"{item.name} güncellendi.")


# --- Plan -----------------------------------------------------------------------------------


class ShiftPlanStaffOut(BaseModel):
    id: uuid.UUID
    full_name: str
    position: str


class ShiftAssignmentOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    date: dt.date
    template_id: uuid.UUID
    changed_after_publish: bool

    @classmethod
    def of(cls, a: ShiftAssignment) -> ShiftAssignmentOut:
        return cls(
            id=a.id,
            staff_id=a.staff_id,
            date=a.work_date,
            template_id=a.template_id,
            changed_after_publish=a.changed_after_publish,
        )


class ShiftConflictOut(BaseModel):
    assignment_id: uuid.UUID
    kind: ConflictKind
    message: str

    @classmethod
    def of(cls, c: Conflict) -> ShiftConflictOut:
        return cls(assignment_id=c.assignment_id, kind=c.kind, message=c.message)  # type: ignore[arg-type]


class ShiftPlanLeaveOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    staff_id: uuid.UUID
    from_: dt.date = Field(alias="from")
    to: dt.date
    type: LeaveType | None = Field(description="yalnız staff.manage'e; diğerlerine boş")


class ShiftWeekOut(BaseModel):
    week: dt.date
    at: dt.datetime
    by: str


class ShiftPlanOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: dt.date = Field(alias="from")
    to: dt.date
    staff: list[ShiftPlanStaffOut]
    templates: list[ShiftTemplateOut]
    assignments: list[ShiftAssignmentOut]
    leaves: list[ShiftPlanLeaveOut]
    conflicts: list[ShiftConflictOut]
    published: ShiftWeekOut | None
    changed_after_publish: int
    final_days: list[dt.date] = Field(description="puantajı kesinleşmiş günler (değişmez)")


@router.get("/shift-plans", summary="Plan: personel × gün, izinler, çakışmalar, yayın durumu")
async def get_plan(
    ctx: Reader,
    today: TodayDep,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: dt.date | None = None,
) -> ShiftPlanOut:
    try:
        first, last = svc.check_range(from_, to, today)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    plan = await svc.plan(ctx.session, first, last)
    show_type = ctx.access.can(Permission.STAFF_MANAGE)
    return ShiftPlanOut(
        from_=first,
        to=last,
        staff=[
            ShiftPlanStaffOut(id=s.id, full_name=s.full_name, position=s.position)
            for s in plan.staff
        ],
        templates=[ShiftTemplateOut.of(t) for t in plan.templates],
        assignments=[ShiftAssignmentOut.of(a) for a in plan.assignments],
        leaves=[
            ShiftPlanLeaveOut(
                staff_id=x.staff_id,
                from_=x.date_from,
                to=x.date_to,
                type=LeaveType(x.leave_type) if show_type else None,
            )
            for x in plan.leaves
        ],
        conflicts=[ShiftConflictOut.of(c) for c in plan.conflicts],
        published=ShiftWeekOut(
            week=plan.week.week_start, at=plan.week.published_at, by=plan.week.published_by_name
        )
        if plan.week
        else None,
        changed_after_publish=plan.changed_after_publish,
        final_days=sorted(plan.final_days),
    )


class ShiftAssignmentIn(BaseModel):
    staff_id: uuid.UUID | None = None
    date: dt.date | None = None
    template_id: uuid.UUID | None = None


class ShiftAssignedOut(ShiftAssignmentOut):
    conflicts: list[ShiftConflictOut]


@router.post("/shift-plans/assignments", status_code=status.HTTP_201_CREATED, summary="Vardiya ata")
async def create_assignment(
    body: ShiftAssignmentIn, ctx: Manager, current: CurrentUserDep
) -> Written[ShiftAssignedOut]:
    try:
        item, found = await svc.assign(
            ctx.session,
            staff_id=body.staff_id,
            work_date=body.date,
            template_id=body.template_id,
            actor_name=current.user.full_name,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.ShiftRuleError as exc:
        raise _rule(exc) from exc
    out = ShiftAssignedOut(
        **ShiftAssignmentOut.of(item).model_dump(),
        conflicts=[ShiftConflictOut.of(c) for c in found],
    )
    await ctx.session.commit()
    message = (
        f"Atandı — uyarı: {'; '.join(c.message for c in found)}" if found else "Vardiya atandı."
    )
    return Written(data=out, message=message)


@router.delete("/shift-plans/assignments/{assignment_id}", summary="Atamayı kaldır")
async def delete_assignment(assignment_id: uuid.UUID, ctx: Manager) -> Written[None]:
    item = await ctx.session.get(ShiftAssignment, assignment_id, with_for_update=True)
    if item is None:
        raise NotFoundError("Atama bulunamadı.")
    try:
        await svc.unassign(ctx.session, item)
    except svc.ShiftRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.commit()
    return Written(data=None, message="Atama kaldırıldı.")


class ShiftCopyWeekIn(BaseModel):
    from_week: dt.date | None = None
    to_week: dt.date | None = None


class ShiftCopiedOut(BaseModel):
    created: int


def _monday(value: dt.date | None, key: str, errors: dict[str, str]) -> dt.date:
    if value is None or week_start(value) != value:
        errors[key] = "Hafta başını (pazartesi) seçin."
        return dt.date.min
    return value


@router.post(
    "/shift-plans/copy-week", status_code=status.HTTP_201_CREATED, summary="Haftayı kopyala"
)
async def copy_week(
    body: ShiftCopyWeekIn, ctx: Manager, current: CurrentUserDep
) -> Written[ShiftCopiedOut]:
    errors: dict[str, str] = {}
    source = _monday(body.from_week, "from_week", errors)
    target = _monday(body.to_week, "to_week", errors)
    if not errors and source == target:
        errors["to_week"] = "Kaynak ve hedef hafta aynı olamaz."
    if errors:
        raise form_error(FormFieldError(errors))
    created = await svc.copy_week(
        ctx.session, from_week=source, to_week=target, actor_name=current.user.full_name
    )
    await ctx.session.commit()
    return Written(data=ShiftCopiedOut(created=created), message=f"{created} atama kopyalandı.")


class ShiftPublishIn(BaseModel):
    week: dt.date | None = None
    acknowledge_conflicts: bool = False


class ShiftPublishedOut(BaseModel):
    week: dt.date
    assignments: int


@router.post("/shift-plans/publish", summary="Haftayı yayınla (çakışma varsa onay ister)")
async def publish(
    body: ShiftPublishIn, ctx: Manager, current: CurrentUserDep, now: NowDep
) -> Written[ShiftPublishedOut]:
    errors: dict[str, str] = {}
    week = _monday(body.week, "week", errors)
    if errors:
        raise form_error(FormFieldError(errors))
    try:
        count = await svc.publish(
            ctx.session,
            week=week,
            acknowledge=body.acknowledge_conflicts,
            actor_name=current.user.full_name,
            now=now,
        )
    except svc.ShiftRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.commit()
    return Written(
        data=ShiftPublishedOut(week=week, assignments=count),
        message=f"Hafta planı yayınlandı ({count} vardiya); hesabı bağlı personele bildirim gitti.",
    )
