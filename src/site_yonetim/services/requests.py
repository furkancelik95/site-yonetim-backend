"""Talep servisleri — docs/03 §9, docs/06 §2.10. Açık site kapsamında; commit çağırana ait.

- Numara site içinde artan: site başına işlem kilidi (advisory lock) altında `max + 1`;
  `(site_id, number)` benzersizliği ikinci güvence.
- Kategori site tanımlıdır (D-26): öncelik verilmezse kategorinin varsayılanı (sakinin aciliyet
  beyanı bilgi amaçlıdır, HIZ-01 "Alanlar"); kategorinin ekibi varsa talep o departmana gider.
- SLA kural anlık görüntüsü talebe yazılır (HIZ-03 adım 4); bitiş ve %80 anı açılışta hesaplanır.
- Durum akışı dokümandaki geçiş tablosuyla (K18, `domain/operations.TRANSITIONS`). Her değişiklik
  `request_events`'e yazılır (değişmez geçmiş); talebi açan sakine bildirim gider (HIZ-01 adım 6).
- Durum değişikliği talep satırı kilitlenerek yapılır (eşzamanlı iki değişiklik sırayla işler).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import (
    ACTIVE_STATUSES,
    FINAL_STATUSES,
    RESIDENT_CANCELLABLE,
    STATUS_LABELS,
    OperationRuleError,
    RequestEventKind,
    RequestPriority,
    RequestState,
    RequestStatus,
    check_status_change,
    clean_reason,
)
from site_yonetim.domain.service_desk import (
    WARNING_RATIO,
    HourType,
    SlaRule,
    SlaStatus,
    calendar_json,
    due_at,
    parse_calendar,
    sla_status,
)
from site_yonetim.domain.structure import PartyRole
from site_yonetim.models import (
    Block,
    Person,
    Request,
    RequestCategory,
    RequestEvent,
    SiteMembership,
    Unit,
    UnitParty,
)
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import request_setup
from site_yonetim.services.charging import BUSINESS_TZ

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def _clean(value: str | None) -> str | None:
    return (" ".join(value.split()) or None) if value is not None else None


async def _next_number(session: AsyncSession) -> int:
    scope = current_scope()
    await session.execute(_NUMBER_LOCK, {"key": f"requests:{scope.site_id if scope else ''}"})
    current = await session.scalar(select(func.max(Request.number)))
    return (current or 0) + 1


async def person_unit_ids(
    session: AsyncSession, person_id: uuid.UUID, today: date
) -> set[uuid.UUID]:
    """Kişinin bugün malik/kiracı/oturan olduğu bölümler — sakin yalnız bunlar için talep açar."""
    rows = await session.scalars(
        select(UnitParty.unit_id).where(
            UnitParty.person_id == person_id,
            UnitParty.role.in_(
                [PartyRole.OWNER.value, PartyRole.TENANT.value, PartyRole.RESIDENT.value]
            ),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    return set(rows)


def event(
    request: Request,
    kind: RequestEventKind,
    description: str,
    actor: str,
    status: RequestStatus | None = None,
    at: datetime | None = None,
) -> RequestEvent:
    item = RequestEvent(
        request_id=request.id,
        kind=kind.value,
        description=description,
        actor_name=actor,
        new_status=status.value if status else None,
    )
    if at is not None:  # verilmezse veritabanı saati (geçmiş değişmez; sonradan düzeltilemez)
        item.created_at = at
    return item


# --- Bildirim ------------------------------------------------------------------------------


async def person_user_ids(session: AsyncSession, person_ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Kişilerin bu sitedeki etkin kullanıcı hesapları (sakin bildirimi için)."""
    if not person_ids:
        return set()
    rows = await session.scalars(
        select(SiteMembership.user_id).where(
            SiteMembership.person_id.in_(person_ids), SiteMembership.is_active
        )
    )
    return set(rows)


async def notify_reporter(
    session: AsyncSession, request: Request, title: str, *, key: str, body: str | None = None
) -> None:
    """Talebi açan sakine uygulama içi bildirim. İç not içeriği buraya **hiç** girmez (K26)."""
    if request.reported_by_person_id is None:
        return
    users = await person_user_ids(session, {request.reported_by_person_id})
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="request.status",
            title=title,
            body=body or f"#{request.number} {request.title}",
            link_entity="requests",
            link_id=request.id,
        ),
        dedupe_key=f"request:{request.id}:{key}",
    )


# --- Açma ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewRequest:
    title: str
    description: str | None
    category: str
    priority: RequestPriority | None  # None → kategorinin varsayılanı
    unit_id: uuid.UUID | None
    location: str | None
    reported_by_person_id: uuid.UUID | None


async def create(session: AsyncSession, data: NewRequest, *, actor: str, now: datetime) -> Request:
    """`now`: açılış anı (SLA bu andan sayılır; demo verisi geçmiş tarih verir)."""
    category = await request_setup.active_category(session, data.category)
    if (
        data.unit_id is not None
        and await session.scalar(select(Unit.id).where(Unit.id == data.unit_id)) is None
    ):
        raise OperationRuleError("unit_not_found", "Seçilen bölüm bulunamadı.", field="unit_id")
    if (
        data.reported_by_person_id is not None
        and await session.scalar(select(Person.id).where(Person.id == data.reported_by_person_id))
        is None
    ):
        raise OperationRuleError(
            "person_not_found", "Seçilen kişi bulunamadı.", field="reported_by_person_id"
        )
    priority = data.priority or RequestPriority(category.default_priority)
    settings = await request_setup.load_settings(session)
    rule = request_setup.sla_rule(category, priority, settings)
    request = Request(
        id=uuid.uuid7(),
        number=await _next_number(session),
        title=" ".join(data.title.split()),
        description=_clean(data.description),
        category=category.key,
        priority=priority.value,
        status=RequestStatus.OPEN.value,
        unit_id=data.unit_id,
        location=_clean(data.location),
        reported_by_person_id=data.reported_by_person_id,
        department_id=category.department_id,
        created_by_name=actor,
        sla_hour_type=rule.hour_type.value,
        sla_target_hours=rule.target_hours,
        sla_calendar=calendar_json(rule.calendar) if rule.calendar else None,
        due_at=due_at(rule, now, BUSINESS_TZ),
        sla_warn_at=due_at(rule, now, BUSINESS_TZ, ratio=WARNING_RATIO),
        created_at=now,
    )
    session.add(request)
    await session.flush()
    session.add(
        event(
            request,
            RequestEventKind.CREATED,
            f"Talep açıldı: {request.title}",
            actor,
            RequestStatus.OPEN,
            at=now,
        )
    )
    await session.flush()
    await notify_reporter(session, request, f"Talebiniz alındı: #{request.number}", key="created")
    return request


async def get(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> Request | None:
    query = select(Request).where(Request.id == request_id)
    if for_update:
        query = query.with_for_update()
    request: Request | None = await session.scalar(query)
    return request


# --- Durum ---------------------------------------------------------------------------------


def state_of(request: Request) -> RequestState:
    return RequestState(
        status=RequestStatus(request.status),
        has_assignee=bool(request.assigned_to),
        awaits_resident=request.reported_by_person_id is not None,
    )


async def apply_status(
    session: AsyncSession,
    request: Request,
    new_status: RequestStatus,
    *,
    kind: RequestEventKind,
    description: str,
    actor: str,
    now: datetime,
    resolution: str | None = None,
) -> None:
    """Durumu yazar (geçiş kararı verilmiş olmalı): zaman alanları, olay, sakine bildirim."""
    request.status = new_status.value
    if new_status is RequestStatus.RESOLVED:
        request.resolution = resolution
        request.resolved_at = now
        days = (await request_setup.load_settings(session)).auto_close_days
        request.auto_close_at = now + timedelta(days=days)
    elif new_status in FINAL_STATUSES:
        request.closed_at = now
        request.auto_close_at = None
        if new_status is RequestStatus.CANCELLED:
            request.resolution = resolution
            request.resolved_at = None
    else:  # yeniden açıldı / ilerliyor
        request.resolved_at = None
        request.auto_close_at = None
    session.add(event(request, kind, description, actor, new_status, at=now))
    await session.flush()
    body = f"#{request.number} {request.title}"
    if new_status is RequestStatus.RESOLVED:
        body += ". Sorun çözüldüyse onaylayıp puan verin; çözülmediyse bildirin."
    await notify_reporter(
        session,
        request,
        f"Talebiniz: {STATUS_LABELS[new_status]}",
        key=f"status:{new_status.value}:{now.isoformat()}",
        body=body,
    )


async def change_status(
    session: AsyncSession,
    request: Request,
    new_status: RequestStatus,
    resolution: str | None,
    *,
    actor: str,
    now: datetime,
) -> Request:
    change = check_status_change(state_of(request), new_status, resolution)
    await apply_status(
        session,
        request,
        new_status,
        kind=change.event,
        description=change.description,
        actor=actor,
        now=now,
        resolution=clean_reason(resolution) if new_status in (
            RequestStatus.RESOLVED, RequestStatus.CANCELLED
        ) else request.resolution,
    )  # fmt: skip
    return request


async def cancel_by_resident(
    session: AsyncSession, request: Request, reason: str | None, *, actor: str, now: datetime
) -> Request:
    """Sakin kendi talebini iş başlamadan iptal eder (K18)."""
    if RequestStatus(request.status) not in RESIDENT_CANCELLABLE:
        raise OperationRuleError(
            "not_cancellable",
            "Üzerinde çalışılan ya da kapanmış talep iptal edilemez; yönetimle görüşün.",
            conflict=True,
        )
    text_value = clean_reason(reason) or "Sakin vazgeçti."
    await apply_status(
        session,
        request,
        RequestStatus.CANCELLED,
        kind=RequestEventKind.CANCELLED,
        description=f"Sakin talebi iptal etti. Gerekçe: {text_value}",
        actor=actor,
        now=now,
        resolution=text_value,
    )
    return request


async def assign(
    session: AsyncSession,
    request: Request,
    assignee: str,
    *,
    actor: str,
    now: datetime,
) -> Request:
    """Kişiye/firmaya atar; açık talep "Atandı" olur (HIZ-02 adım 2)."""
    name = " ".join(assignee.split())
    if not name:
        raise OperationRuleError("assignee_required", "Atanacak kişiyi yazın.", field="assignee")
    if RequestStatus(request.status) in FINAL_STATUSES:
        raise OperationRuleError(
            "request_finished", "Kapanmış ya da iptal edilmiş talep atanamaz.", conflict=True
        )
    if name == request.assigned_to:
        raise OperationRuleError(
            "already_assigned", f"Talep zaten {name} kişisine atanmış.", conflict=True
        )
    request.assigned_to = name
    session.add(event(request, RequestEventKind.ASSIGNED, f"Atandı: {name}", actor, at=now))
    if RequestStatus(request.status) is RequestStatus.OPEN:
        await apply_status(
            session,
            request,
            RequestStatus.ASSIGNED,
            kind=RequestEventKind.STATUS_CHANGED,
            description="Durum: Açık → Atandı",
            actor=actor,
            now=now + timedelta(microseconds=1),
        )
    await session.flush()
    return request


async def comment(
    session: AsyncSession, request: Request, body: str, *, actor: str
) -> RequestEvent:
    text_value = " ".join(body.split())
    if not text_value:
        raise OperationRuleError("comment_required", "Yorum boş olamaz.", field="body")
    item = event(request, RequestEventKind.COMMENT, text_value, actor)
    session.add(item)
    await session.flush()
    return item


async def events(session: AsyncSession, request_id: uuid.UUID) -> list[RequestEvent]:
    rows = await session.scalars(
        select(RequestEvent).where(RequestEvent.request_id == request_id).order_by(RequestEvent.id)
    )
    return list(rows)


def list_query(
    *,
    status: RequestStatus | None,
    category: str | None,
    priority: RequestPriority | None,
    reporter: uuid.UUID | None,
    department_id: uuid.UUID | None = None,
    sla_breached: bool | None = None,
    now: datetime | None = None,
    unit_id: uuid.UUID | None = None,
) -> Select[Request]:
    """Yeni talep üstte. `reporter` verilirse yalnız o kişinin talepleri (sakin)."""
    query = select(Request)
    if department_id is not None:
        query = query.where(Request.department_id == department_id)
    if unit_id is not None:
        query = query.where(Request.unit_id == unit_id)
    if status is not None:
        query = query.where(Request.status == status.value)
    if category is not None:
        query = query.where(Request.category == category)
    if priority is not None:
        query = query.where(Request.priority == priority.value)
    if reporter is not None:
        query = query.where(Request.reported_by_person_id == reporter)
    if sla_breached and now is not None:
        query = query.where(
            Request.status.in_([s.value for s in ACTIVE_STATUSES]), Request.due_at < now
        )
    return query.order_by(Request.number.desc())


# --- SLA -----------------------------------------------------------------------------------


def sla_of(request: Request, now: datetime) -> SlaStatus | None:
    """Talebin SLA durumu (anlık görüntüyle); kuralı olmayan eski talep ve iptalde None."""
    if (
        request.sla_hour_type is None
        or request.sla_target_hours is None
        or request.due_at is None
        or request.status == RequestStatus.CANCELLED.value
    ):
        return None
    hour_type = HourType(request.sla_hour_type)
    calendar = (
        parse_calendar(request.sla_calendar)
        if hour_type is HourType.BUSINESS and request.sla_calendar
        else None
    )
    rule = SlaRule(hour_type, request.sla_target_hours, calendar)
    return sla_status(
        rule,
        created_at=request.created_at,
        due=request.due_at,
        finished_at=request.resolved_at
        if request.status in (RequestStatus.RESOLVED.value, RequestStatus.CLOSED.value)
        else None,
        now=now,
        tz=BUSINESS_TZ,
    )


# --- Adlar ---------------------------------------------------------------------------------


async def unit_names(session: AsyncSession, unit_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not unit_ids:
        return {}
    rows = await session.execute(
        select(Unit.id, Unit.number, Block.name)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(Unit.id.in_(unit_ids))
    )
    return {uid: f"{block}-{number}" if block else number for uid, number, block in rows}


async def person_names(session: AsyncSession, person_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not person_ids:
        return {}
    rows = await session.execute(
        select(Person.id, Person.first_name, Person.last_name).where(Person.id.in_(person_ids))
    )
    return {pid: f"{first} {last}" for pid, first, last in rows}


async def category_names(session: AsyncSession, keys: set[str]) -> dict[str, str]:
    if not keys:
        return {}
    rows = await session.execute(
        select(RequestCategory.key, RequestCategory.name).where(RequestCategory.key.in_(keys))
    )
    names: dict[str, str] = {}
    for key, name in rows:
        names[key] = name
    return names
