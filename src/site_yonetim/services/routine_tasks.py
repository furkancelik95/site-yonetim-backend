"""Görevli rutin görevleri — Yazılım Dokümanı OPS-06 (servis isteği 58), PM kararı **K29**.
Açık site kapsamında; transaction'ı çağıran yönetir.

- Görevler şablondan **okumada üretilir** (gün × şablon); ayrı "görev" satırı yazılmaz, yalnız
  kapanış (`task_completions`, değişmez). Bir gün bir görev bir kez kapanır (benzersiz kısıt).
- Saat aralığı bitip kapatılmamışsa ya da günü geçmişse `late`. Fotoğraf zorunlu görev fotoğrafsız
  tamamlanamaz; tamamlanamadıysa neden zorunlu.
- K29: şablon personel kartına bağlanabilir (`staff_member_id`); personel kendi görevlerini
  `/me/tasks` uçlarından görür ve kapatır (kart, P05'in `staff_members.user_id` bağıyla bulunur).
- Gün sonu özeti (günlük iş): dünün gecikmiş/tamamlanamamış görevleri yöneticilere tek bildirim.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import (
    MONTH_DAY_MAX,
    CompletionStatus,
    OpsError,
    TaskFrequency,
    TaskStatus,
    clean,
    conflict,
    task_applies,
    task_rate,
    task_status,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import ChecklistTemplate, Site, StaffMember, TaskCompletion, TaskTemplate
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import notifications
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.facility_common import PhotoSink, holders, save_photo

REPORT_DAYS = 7
JOB_ACTOR = "Görev gün sonu özeti"


@dataclass(frozen=True, slots=True)
class TemplateData:
    name: str | None
    description: str | None
    frequency: str | None
    weekdays: list[int] | None
    month_day: int | None
    time_from: time | None
    time_to: time | None
    location: str | None
    assignee: str | None
    staff_member_id: uuid.UUID | None
    photo_required: bool | None
    checklist_template_id: uuid.UUID | None
    is_active: bool | None


def current_template(t: TaskTemplate) -> TemplateData:
    return TemplateData(
        name=t.name,
        description=t.description,
        frequency=t.frequency,
        weekdays=list(t.weekdays),
        month_day=t.month_day,
        time_from=t.time_from,
        time_to=t.time_to,
        location=t.location,
        assignee=t.assignee,
        staff_member_id=t.staff_member_id,
        photo_required=t.photo_required,
        checklist_template_id=t.checklist_template_id,
        is_active=t.is_active,
    )


async def save_template(
    session: AsyncSession, data: TemplateData, current: TaskTemplate | None
) -> TaskTemplate:
    errors: dict[str, str] = {}
    name = clean(data.name, 60)
    if len(name) < 2:
        errors["name"] = "Görev adını yazın."
    frequency = (
        TaskFrequency(data.frequency)
        if data.frequency in {f.value for f in TaskFrequency}
        else None
    )
    if frequency is None:
        errors["frequency"] = "Sıklık seçin."
    weekdays = sorted({d for d in (data.weekdays or []) if isinstance(d, int) and 0 <= d <= 6})
    if frequency is TaskFrequency.WEEKDAYS and not weekdays:
        errors["weekdays"] = "En az bir gün seçin."
    if frequency is TaskFrequency.MONTHLY and not (
        data.month_day is not None and 1 <= data.month_day <= MONTH_DAY_MAX
    ):
        errors["month_day"] = "Ayın 1–28. günü."
    if data.time_from is None or data.time_to is None or data.time_to <= data.time_from:
        errors["time_to"] = "Saat aralığını kontrol edin (bitiş başlangıçtan sonra)."
    staff: StaffMember | None = None
    if data.staff_member_id is not None:
        staff = await session.scalar(
            select(StaffMember).where(StaffMember.id == data.staff_member_id)
        )
        if staff is None:
            errors["staff_member_id"] = "Personel bulunamadı."
    assignee = clean(data.assignee, 80) or (staff.full_name if staff else "")
    if not assignee:
        errors["assignee"] = "Rol ya da kişi yazın."
    if data.checklist_template_id is not None and (
        await session.scalar(
            select(ChecklistTemplate.id).where(ChecklistTemplate.id == data.checklist_template_id)
        )
        is None
    ):
        errors["checklist_template_id"] = "Denetim şablonu bulunamadı."
    if errors or frequency is None or data.time_from is None or data.time_to is None:
        raise FormFieldError(errors)
    template = current or TaskTemplate(id=uuid.uuid7())
    template.name = name
    template.description = clean(data.description, 300) or None
    template.frequency = frequency.value
    template.weekdays = weekdays if frequency is TaskFrequency.WEEKDAYS else []
    template.month_day = data.month_day if frequency is TaskFrequency.MONTHLY else None
    template.time_from = data.time_from
    template.time_to = data.time_to
    template.location = clean(data.location, 80) or None
    template.assignee = assignee
    template.staff_member_id = data.staff_member_id
    template.photo_required = bool(data.photo_required)
    template.checklist_template_id = data.checklist_template_id
    template.is_active = True if data.is_active is None else data.is_active
    if current is None:
        session.add(template)
    await session.flush()
    return template


def templates_query() -> Select[TaskTemplate]:
    return select(TaskTemplate).order_by(TaskTemplate.time_from, TaskTemplate.name, TaskTemplate.id)


async def get_template(session: AsyncSession, template_id: uuid.UUID) -> TaskTemplate | None:
    found: TaskTemplate | None = await session.scalar(
        select(TaskTemplate).where(TaskTemplate.id == template_id)
    )
    return found


def _applies(t: TaskTemplate, day: date) -> bool:
    return task_applies(TaskFrequency(t.frequency), list(t.weekdays), t.month_day, day)


@dataclass(frozen=True, slots=True)
class DayTask:
    template: TaskTemplate
    done: TaskCompletion | None
    status: TaskStatus


async def day_tasks(
    session: AsyncSession,
    day: date,
    *,
    today: date,
    now: time,
    staff_member_id: uuid.UUID | None = None,
) -> list[DayTask]:
    query = templates_query().where(TaskTemplate.is_active)
    if staff_member_id is not None:
        query = query.where(TaskTemplate.staff_member_id == staff_member_id)
    templates = [t for t in await session.scalars(query) if _applies(t, day)]
    if not templates:
        return []
    closed = {
        c.template_id: c
        for c in await session.scalars(
            select(TaskCompletion).where(
                TaskCompletion.task_date == day,
                TaskCompletion.template_id.in_([t.id for t in templates]),
            )
        )
    }
    out: list[DayTask] = []
    for t in templates:
        done = closed.get(t.id)
        status = task_status(
            CompletionStatus(done.status) if done else None,
            day=day,
            today=today,
            now=now,
            time_from=t.time_from,
            time_to=t.time_to,
        )
        out.append(DayTask(t, done, status))
    return out


@dataclass(frozen=True, slots=True)
class Closer:
    name: str
    user_id: uuid.UUID | None
    staff_member_id: uuid.UUID | None


async def complete(
    session: AsyncSession,
    sink: PhotoSink,
    template: TaskTemplate | None,
    *,
    day: date,
    status: str | None,
    reason: str | None,
    note: str | None,
    photo: str | None,
    by: Closer,
    today: date,
) -> TaskCompletion:
    if template is None or not template.is_active or not _applies(template, day):
        raise OpsError(404, "not_found", "Bu gün için görev yok.")
    if day > today:
        raise conflict("future_task", "İleri tarihli görev kapatılamaz.")
    if await session.scalar(
        select(TaskCompletion.id).where(
            TaskCompletion.template_id == template.id, TaskCompletion.task_date == day
        )
    ):
        raise conflict("already_done", "Görev zaten kapatılmış.")
    outcome = (
        CompletionStatus.FAILED
        if status == CompletionStatus.FAILED.value
        else CompletionStatus.DONE
    )
    why = clean(reason, 200) or None
    if outcome is CompletionStatus.FAILED and (why is None or len(why) < 3):
        raise FormFieldError({"reason": "Tamamlanamama nedenini seçin."})
    if outcome is CompletionStatus.DONE and template.photo_required and not photo:
        raise OpsError(
            422,
            "photo_required",
            "Bu görev fotoğrafsız tamamlanamaz.",
            {"photo": "Fotoğraf ekleyin."},
        )
    photo_id = await save_photo(session, sink, photo, by=by.name, field_name="photo")
    record = TaskCompletion(
        id=uuid.uuid7(),
        template_id=template.id,
        task_date=day,
        status=outcome.value,
        reason=why if outcome is CompletionStatus.FAILED else None,
        note=clean(note, 200) or None,
        photo_file_id=photo_id,
        by_name=by.name,
        by_user_id=by.user_id,
        staff_member_id=by.staff_member_id,
    )
    session.add(record)
    await session.flush()
    return record


@dataclass(frozen=True, slots=True)
class ReportRow:
    template: TaskTemplate
    expected: int
    done: int
    failed: int

    @property
    def rate(self) -> int | None:
        return task_rate(self.expected, self.done)


async def report(session: AsyncSession, *, today: date) -> tuple[date, date, list[ReportRow]]:
    """Son 7 gün (bugün hariç): şablon başına beklenen / tamamlanan."""
    first, last = today - timedelta(days=REPORT_DAYS), today - timedelta(days=1)
    templates = list(await session.scalars(templates_query().where(TaskTemplate.is_active)))
    counts: dict[tuple[uuid.UUID, str], int] = {}
    for row in await session.execute(
        select(TaskCompletion.template_id, TaskCompletion.status, func.count())
        .where(TaskCompletion.task_date.between(first, last))
        .group_by(TaskCompletion.template_id, TaskCompletion.status)
    ):
        counts[(row[0], row[1])] = row[2]
    days = [first + timedelta(days=i) for i in range(REPORT_DAYS)]
    rows = [
        ReportRow(
            t,
            sum(1 for d in days if _applies(t, d) and d >= t.created_at.date()),
            counts.get((t.id, CompletionStatus.DONE.value), 0),
            counts.get((t.id, CompletionStatus.FAILED.value), 0),
        )
        for t in templates
    ]
    return first, last, rows


# --- Gün sonu özeti (günlük iş) ------------------------------------------------------


async def summarize_day(session: AsyncSession, site: Site, *, day: date) -> int:
    tasks = await day_tasks(session, day, today=day + timedelta(days=1), now=time(0, 0))
    late = [t for t in tasks if t.status is TaskStatus.LATE]
    failed = [t for t in tasks if t.status is TaskStatus.FAILED]
    if not late and not failed:
        return 0
    names = ", ".join(t.template.name for t in (late + failed)[:5])
    return await notifications.notify(
        session,
        await holders(session, site, Permission.REQUESTS_ASSIGN),
        notifications.Message(
            category=NotificationCategory.OPERATIONS,
            kind="tasks.daily_summary",
            title=(f"{day:%d.%m.%Y} görev özeti: {len(late)} gecikti, {len(failed)} tamamlanamadı"),
            body=names + ("…" if len(late) + len(failed) > 5 else ""),
            link_entity="task_templates",
        ),
        dedupe_key=f"tasks.summary:{day}",
    )


async def run_daily_summary(
    factory: async_sessionmaker[AsyncSession], *, today: date
) -> list[tuple[uuid.UUID, int]]:
    day = today - timedelta(days=1)
    with all_sites_scope():
        async with factory() as session:
            site_ids: list[uuid.UUID] = sorted(
                set(
                    await session.scalars(
                        select(func.distinct(TaskTemplate.site_id)).where(TaskTemplate.is_active)
                    )
                )
            )
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, int]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append((site_id, await summarize_day(session, site, day=day)))
    return results


def local_now(now: datetime) -> time:
    return now.astimezone(BUSINESS_TZ).time().replace(tzinfo=None)
