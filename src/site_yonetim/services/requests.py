"""Talep servisleri — docs/03 §9, docs/06 §2.10. Açık site kapsamında; commit çağırana ait.

- Numara site içinde artan: site başına işlem kilidi (advisory lock) altında `max + 1`;
  `(site_id, number)` benzersizliği ikinci güvence.
- Her değişiklik `request_events`'e yazılır (değişmez geçmiş).
- Durum değişikliği talep satırı kilitlenerek yapılır (eşzamanlı iki değişiklik sırayla işler).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.operations import (
    OperationRuleError,
    RequestCategory,
    RequestEventKind,
    RequestPriority,
    RequestStatus,
    check_status_change,
)
from site_yonetim.domain.structure import PartyRole
from site_yonetim.models import Block, Person, Request, RequestEvent, Unit, UnitParty

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


def _event(
    request: Request,
    kind: RequestEventKind,
    description: str,
    actor: str,
    status: RequestStatus | None = None,
) -> RequestEvent:
    return RequestEvent(
        request_id=request.id,
        kind=kind.value,
        description=description,
        actor_name=actor,
        new_status=status.value if status else None,
    )


@dataclass(frozen=True, slots=True)
class NewRequest:
    title: str
    description: str | None
    category: RequestCategory
    priority: RequestPriority
    unit_id: uuid.UUID | None
    location: str | None
    reported_by_person_id: uuid.UUID | None


async def create(session: AsyncSession, data: NewRequest, *, actor: str) -> Request:
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
    request = Request(
        id=uuid.uuid7(),
        number=await _next_number(session),
        title=" ".join(data.title.split()),
        description=_clean(data.description),
        category=data.category.value,
        priority=data.priority.value,
        status=RequestStatus.OPEN.value,
        unit_id=data.unit_id,
        location=_clean(data.location),
        reported_by_person_id=data.reported_by_person_id,
        created_by_name=actor,
    )
    session.add(request)
    await session.flush()
    session.add(
        _event(
            request,
            RequestEventKind.CREATED,
            f"Talep açıldı: {request.title}",
            actor,
            RequestStatus.OPEN,
        )
    )
    await session.flush()
    return request


async def get(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> Request | None:
    query = select(Request).where(Request.id == request_id)
    if for_update:
        query = query.with_for_update()
    request: Request | None = await session.scalar(query)
    return request


async def change_status(
    session: AsyncSession,
    request: Request,
    new_status: RequestStatus,
    resolution: str | None,
    *,
    actor: str,
    now: datetime,
) -> Request:
    change = check_status_change(RequestStatus(request.status), new_status, resolution)
    request.status = new_status.value
    if change.resolved:
        request.resolution = _clean(resolution)
        request.resolved_at = now
    else:
        request.resolved_at = None
    session.add(_event(request, change.event, change.description, actor, new_status))
    await session.flush()
    return request


async def assign(session: AsyncSession, request: Request, assignee: str, *, actor: str) -> Request:
    name = " ".join(assignee.split())
    if not name:
        raise OperationRuleError("assignee_required", "Atanacak kişiyi yazın.", field="assignee")
    if name == request.assigned_to:
        raise OperationRuleError(
            "already_assigned", f"Talep zaten {name} kişisine atanmış.", conflict=True
        )
    request.assigned_to = name
    session.add(_event(request, RequestEventKind.ASSIGNED, f"Atandı: {name}", actor))
    await session.flush()
    return request


async def comment(
    session: AsyncSession, request: Request, body: str, *, actor: str
) -> RequestEvent:
    text_value = " ".join(body.split())
    if not text_value:
        raise OperationRuleError("comment_required", "Yorum boş olamaz.", field="body")
    event = _event(request, RequestEventKind.COMMENT, text_value, actor)
    session.add(event)
    await session.flush()
    return event


async def events(session: AsyncSession, request_id: uuid.UUID) -> list[RequestEvent]:
    rows = await session.scalars(
        select(RequestEvent).where(RequestEvent.request_id == request_id).order_by(RequestEvent.id)
    )
    return list(rows)


def list_query(
    *,
    status: RequestStatus | None,
    category: RequestCategory | None,
    priority: RequestPriority | None,
    reporter: uuid.UUID | None,
    department_id: uuid.UUID | None = None,
) -> Select[Request]:
    """Yeni talep üstte. `reporter` verilirse yalnız o kişinin talepleri (sakin)."""
    query = select(Request)
    if department_id is not None:
        query = query.where(Request.department_id == department_id)
    if status is not None:
        query = query.where(Request.status == status.value)
    if category is not None:
        query = query.where(Request.category == category.value)
    if priority is not None:
        query = query.where(Request.priority == priority.value)
    if reporter is not None:
        query = query.where(Request.reported_by_person_id == reporter)
    return query.order_by(Request.number.desc())


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
