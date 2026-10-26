"""Vardiya şablonları ve haftalık plan — servis isteği 36 (Yazılım Dokümanı IK-02). Açık site
kapsamında; transaction'ı çağıran yönetir.

- Şablon silinmez, kapatılır; kapalı şablon yeni atamada kullanılamaz. Ad tekil.
- Çakışma (aynı kişiye çakışan vardiya, onaylı izin, < 11 saat dinlenme) atamayı ve yayını
  **engellemez**; yayın `acknowledge_conflicts` ister.
- Aynı kişi + gün + şablon bir kez. Puantajı kesinleşmiş günde atama eklenmez/kaldırılmaz.
- Yayından sonra eklenen atama `changed_after_publish`; eklenen/kaldırılan atama bağlı hesabı
  olan personele (K29) uygulama içi bildirim olarak gider (SMS/e-posta K5'e bağlı).
"""

import datetime as dt
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.staffing import (
    BREAK_MAX,
    TEMPLATE_NAME_MAX,
    TEMPLATE_TEXT_MAX,
    Conflict,
    LeaveDays,
    LeaveStatus,
    LeaveType,
    PlannedShift,
    Span,
    find_conflicts,
    format_time,
    parse_time,
    span,
    week_start,
)
from site_yonetim.models import (
    AttendanceEvent,
    LeaveRequest,
    ShiftAssignment,
    ShiftTemplate,
    ShiftWeek,
    StaffMember,
    TimesheetDay,
    TimesheetFix,
)
from site_yonetim.services import notifications as notify_svc

PLAN_DAYS_MAX = 42


class ShiftRuleError(Exception):
    """409 kuralları: `code` makine için, mesaj Türkçe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clean(value: str | None, limit: int) -> str | None:
    return " ".join((value or "").split())[:limit] or None


def to_time(minutes: int) -> dt.time:
    return dt.time(minutes // 60, minutes % 60)


def minutes_of(value: dt.time) -> int:
    return value.hour * 60 + value.minute


def template_span(t: ShiftTemplate) -> Span:
    return span(minutes_of(t.start_time), minutes_of(t.end_time))


def template_label(t: ShiftTemplate) -> str:
    return f"{t.name} {format_time(minutes_of(t.start_time))}–{format_time(minutes_of(t.end_time))}"


# --- Şablonlar ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TemplateData:
    name: str | None
    start: str | None
    end: str | None
    break_minutes: int | None
    position: str | None
    location: str | None
    is_active: bool | None


async def save_template(
    session: AsyncSession, data: TemplateData, *, item: ShiftTemplate | None
) -> ShiftTemplate:
    errors: dict[str, str] = {}
    name = _clean(data.name, TEMPLATE_NAME_MAX)
    if name is None:
        errors["name"] = "Ad yazın."
    else:
        taken = await session.scalar(
            select(ShiftTemplate.id).where(
                func.lower(ShiftTemplate.name) == name.lower(),
                ShiftTemplate.id != (item.id if item else uuid.UUID(int=0)),
            )
        )
        if taken is not None:
            errors["name"] = "Bu adla şablon var."
    start, end = parse_time(data.start), parse_time(data.end)
    if start is None:
        errors["start"] = "Başlangıç saatini girin (SS:DD)."
    if end is None:
        errors["end"] = "Bitiş saatini girin (SS:DD)."
    if start is not None and start == end:
        errors["end"] = "Bitiş başlangıçla aynı olamaz."
    breaks = data.break_minutes if data.break_minutes is not None else 0
    if not 0 <= breaks <= BREAK_MAX:
        errors["break_minutes"] = f"Mola 0–{BREAK_MAX} dakika."
    elif start is not None and end is not None and breaks >= span(start, end).length:
        errors["break_minutes"] = "Mola vardiya süresinden kısa olmalı."
    if errors or start is None or end is None or name is None:
        raise FormFieldError(errors)
    target = item or ShiftTemplate(id=uuid.uuid7())
    target.name = name
    target.start_time = to_time(start)
    target.end_time = to_time(end)
    target.break_minutes = breaks
    target.position = _clean(data.position, TEMPLATE_TEXT_MAX)
    target.location = _clean(data.location, TEMPLATE_TEXT_MAX)
    target.is_active = True if data.is_active is None else data.is_active
    if item is None:
        session.add(target)
    await session.flush()
    return target


async def templates(session: AsyncSession) -> list[ShiftTemplate]:
    return list(
        await session.scalars(
            select(ShiftTemplate).order_by(ShiftTemplate.start_time, ShiftTemplate.name)
        )
    )


# --- Plan okuma ve çakışma --------------------------------------------------------------------


async def final_days(session: AsyncSession, first: date, last: date) -> set[date]:
    return set(
        await session.scalars(
            select(TimesheetDay.work_date).where(TimesheetDay.work_date.between(first, last))
        )
    )


async def approved_leaves(session: AsyncSession, first: date, last: date) -> list[LeaveRequest]:
    return list(
        await session.scalars(
            select(LeaveRequest)
            .where(
                LeaveRequest.status == LeaveStatus.APPROVED.value,
                LeaveRequest.date_from <= last,
                LeaveRequest.date_to >= first,
            )
            .order_by(LeaveRequest.date_from)
        )
    )


def leave_days(leaves: list[LeaveRequest]) -> list[LeaveDays]:
    return [LeaveDays(x.staff_id, x.date_from, x.date_to, LeaveType(x.leave_type)) for x in leaves]


async def _assignments(
    session: AsyncSession, first: date, last: date, staff_id: uuid.UUID | None = None
) -> list[tuple[ShiftAssignment, ShiftTemplate]]:
    query = (
        select(ShiftAssignment, ShiftTemplate)
        .join(ShiftTemplate, ShiftTemplate.id == ShiftAssignment.template_id)
        .where(ShiftAssignment.work_date.between(first, last))
    )
    if staff_id is not None:
        query = query.where(ShiftAssignment.staff_id == staff_id)
    rows = await session.execute(
        query.order_by(ShiftAssignment.work_date, ShiftTemplate.start_time, ShiftAssignment.id)
    )
    return [(a, t) for a, t in rows.all()]


async def conflicts_for(
    session: AsyncSession, first: date, last: date, staff_id: uuid.UUID | None = None
) -> list[Conflict]:
    """[first, last] aralığındaki atamaların çakışmaları; komşu günler (gece vardiyası,
    dinlenme) hesaba katılır, sonuç aralıktaki atamalarla sınırlanır."""
    rows = await _assignments(
        session, first - timedelta(days=1), last + timedelta(days=1), staff_id
    )
    leaves = leave_days(await approved_leaves(session, first, last))
    shifts = [PlannedShift(a.id, a.staff_id, a.work_date, template_span(t)) for a, t in rows]
    inside = {a.id for a, _ in rows if first <= a.work_date <= last}
    return [c for c in find_conflicts(shifts, leaves) if c.assignment_id in inside]


@dataclass(frozen=True, slots=True)
class Plan:
    first: date
    last: date
    staff: list[StaffMember]
    templates: list[ShiftTemplate]
    assignments: list[ShiftAssignment]
    leaves: list[LeaveRequest]
    conflicts: list[Conflict]
    week: ShiftWeek | None
    changed_after_publish: int
    final_days: set[date] = field(default_factory=set)


def check_range(first: date | None, last: date | None, today: date) -> tuple[date, date]:
    start = first or week_start(today)
    end = last or start + timedelta(days=6)
    if end < start or (end - start).days >= PLAN_DAYS_MAX:
        raise FormFieldError({"to": f"Aralık en çok {PLAN_DAYS_MAX} gün olmalı."})
    return start, end


async def plan(session: AsyncSession, first: date, last: date) -> Plan:
    staff = list(
        await session.scalars(
            select(StaffMember)
            .where(
                StaffMember.start_date <= last,
                or_(StaffMember.end_date.is_(None), StaffMember.end_date >= first),
            )
            .order_by(StaffMember.full_name, StaffMember.id)
        )
    )
    rows = await _assignments(session, first, last)
    week = await session.scalar(select(ShiftWeek).where(ShiftWeek.week_start == week_start(first)))
    assignments = [a for a, _ in rows]
    changed = sum(1 for a in assignments if a.changed_after_publish)
    if week is not None:
        changed += week.removed_after_publish
    return Plan(
        first=first,
        last=last,
        staff=staff,
        templates=await templates(session),
        assignments=assignments,
        leaves=await approved_leaves(session, first, last),
        conflicts=await conflicts_for(session, first, last),
        week=week,
        changed_after_publish=changed,
        final_days=await final_days(session, first, last),
    )


# --- Atama --------------------------------------------------------------------------------


async def _notify_staff(
    session: AsyncSession, staff: StaffMember, kind: str, title: str, *, key: str
) -> None:
    if staff.user_id is None:
        return
    await notify_svc.notify(
        session,
        [staff.user_id],
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            link_entity="shift_assignments",
        ),
        dedupe_key=key,
    )


def _day_text(day: date) -> str:
    return day.strftime("%d.%m.%Y")


async def assign(
    session: AsyncSession,
    *,
    staff_id: uuid.UUID | None,
    work_date: date | None,
    template_id: uuid.UUID | None,
    actor_name: str,
) -> tuple[ShiftAssignment, list[Conflict]]:
    errors: dict[str, str] = {}
    staff = await session.get(StaffMember, staff_id) if staff_id else None
    template = await session.get(ShiftTemplate, template_id) if template_id else None
    if staff is None:
        errors["staff_id"] = "Personel seçin."
    elif work_date is not None and (
        work_date < staff.start_date or (staff.end_date is not None and work_date > staff.end_date)
    ):
        errors["staff_id"] = "Personel bu tarihte çalışmıyor (giriş/ayrılış tarihi)."
    if work_date is None:
        errors["date"] = "Gün seçin."
    if template is None or not template.is_active:
        errors["template_id"] = "Vardiya seçin."
    if errors or staff is None or template is None or work_date is None:
        raise FormFieldError(errors)
    if await final_days(session, work_date, work_date):
        raise ShiftRuleError("timesheet_final", "Bu günün puantajı kesinleşti; vardiya eklenemez.")
    duplicate = await session.scalar(
        select(ShiftAssignment.id).where(
            ShiftAssignment.staff_id == staff.id,
            ShiftAssignment.work_date == work_date,
            ShiftAssignment.template_id == template.id,
        )
    )
    if duplicate is not None:
        raise ShiftRuleError("duplicate", "Bu vardiya o gün bu kişiye zaten atanmış.")
    published = await session.scalar(
        select(ShiftWeek.id).where(ShiftWeek.week_start == week_start(work_date))
    )
    item = ShiftAssignment(
        id=uuid.uuid7(),
        staff_id=staff.id,
        template_id=template.id,
        work_date=work_date,
        changed_after_publish=published is not None,
        created_by_name=actor_name,
    )
    session.add(item)
    await session.flush()
    if published is not None:
        await _notify_staff(
            session,
            staff,
            "shift.changed",
            f"Vardiyanız değişti: {_day_text(work_date)} {template_label(template)} eklendi",
            key=f"shift:add:{item.id}",
        )
    found = [
        c
        for c in await conflicts_for(session, work_date, work_date, staff.id)
        if c.assignment_id == item.id
    ]
    return item, found


async def unassign(session: AsyncSession, item: ShiftAssignment) -> None:
    if await final_days(session, item.work_date, item.work_date):
        raise ShiftRuleError("timesheet_final", "Bu günün puantajı kesinleşti; atama kaldırılamaz.")
    has_events = await session.scalar(
        select(AttendanceEvent.id).where(AttendanceEvent.assignment_id == item.id).limit(1)
    )
    if has_events is not None:
        raise ShiftRuleError(
            "has_attendance", "Bu vardiyada giriş-çıkış kaydı var; atama kaldırılamaz."
        )
    week = await session.scalar(
        select(ShiftWeek)
        .where(ShiftWeek.week_start == week_start(item.work_date))
        .with_for_update()
    )
    staff = await session.get(StaffMember, item.staff_id)
    template = await session.get(ShiftTemplate, item.template_id)
    fix = await session.scalar(select(TimesheetFix).where(TimesheetFix.assignment_id == item.id))
    if fix is not None:
        await session.delete(fix)
    await session.delete(item)
    if week is not None and not item.changed_after_publish:
        week.removed_after_publish += 1
    await session.flush()
    if week is not None and staff is not None and template is not None:
        await _notify_staff(
            session,
            staff,
            "shift.changed",
            f"Vardiyanız değişti: {_day_text(item.work_date)} "
            f"{template_label(template)} kaldırıldı",
            key=f"shift:remove:{item.id}",
        )


async def copy_week(
    session: AsyncSession, *, from_week: date, to_week: date, actor_name: str
) -> int:
    """Kaynak haftanın atamalarını hedefe kopyalar; var olanı korur, yalnız eksikleri ekler.
    Hedef günde çalışmayan personel, kapalı şablon ve kesinleşmiş gün atlanır."""
    shift = to_week - from_week
    source = await _assignments(session, from_week, from_week + timedelta(days=6))
    target_last = to_week + timedelta(days=6)
    existing = {
        (a.staff_id, a.work_date, a.template_id)
        for a, _ in await _assignments(session, to_week, target_last)
    }
    closed = await final_days(session, to_week, target_last)
    published = await session.scalar(select(ShiftWeek.id).where(ShiftWeek.week_start == to_week))
    staff = {s.id: s for s in await session.scalars(select(StaffMember))}
    created = 0
    for a, t in source:
        day = a.work_date + shift
        person = staff.get(a.staff_id)
        if (
            (a.staff_id, day, a.template_id) in existing
            or not t.is_active
            or day in closed
            or person is None
            or day < person.start_date
            or (person.end_date is not None and day > person.end_date)
        ):
            continue
        session.add(
            ShiftAssignment(
                id=uuid.uuid7(),
                staff_id=a.staff_id,
                template_id=a.template_id,
                work_date=day,
                changed_after_publish=published is not None,
                created_by_name=actor_name,
            )
        )
        created += 1
    await session.flush()
    return created


async def publish(
    session: AsyncSession, *, week: date, acknowledge: bool, actor_name: str, now: datetime
) -> int:
    last = week + timedelta(days=6)
    rows = await _assignments(session, week, last)
    if not rows:
        raise ShiftRuleError("empty_plan", "Bu haftada atama yok.")
    found = await conflicts_for(session, week, last)
    if found and not acknowledge:
        raise ShiftRuleError(
            "conflicts",
            f"{len(found)} çakışma var. İnceleyip yine de yayınlamak için onaylayın.",
        )
    record = await session.scalar(
        select(ShiftWeek).where(ShiftWeek.week_start == week).with_for_update()
    )
    if record is None:
        record = ShiftWeek(week_start=week, published_at=now, published_by_name=actor_name)
        session.add(record)
    record.published_at = now
    record.published_by_name = actor_name
    record.removed_after_publish = 0
    for a, _ in rows:
        a.changed_after_publish = False
    await session.flush()
    staff_ids = {a.staff_id for a, _ in rows}
    users = list(
        await session.scalars(
            select(StaffMember.user_id).where(
                StaffMember.id.in_(staff_ids), StaffMember.user_id.is_not(None)
            )
        )
    )
    await notify_svc.notify(
        session,
        [u for u in users if u is not None],
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="shift.published",
            title=f"Vardiya planı yayınlandı: {_day_text(week)} – {_day_text(last)}",
            link_entity="shift_weeks",
            link_id=record.id,
        ),
        dedupe_key=f"shift:publish:{week.isoformat()}:{now.isoformat()}",
    )
    return len(rows)


# --- Personelin kendi vardiyası (K29) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnShift:
    assignment: ShiftAssignment
    template: ShiftTemplate


async def own_shifts(
    session: AsyncSession, staff_id: uuid.UUID, first: date, last: date
) -> list[OwnShift]:
    """ "Vardiyam": yalnız **yayınlanmış** haftaların atamaları (taslak görünmez)."""
    published = set(
        await session.scalars(
            select(ShiftWeek.week_start).where(
                ShiftWeek.week_start.between(week_start(first), last)
            )
        )
    )
    return [
        OwnShift(a, t)
        for a, t in await _assignments(session, first, last, staff_id)
        if week_start(a.work_date) in published
    ]
