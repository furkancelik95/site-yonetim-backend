"""Park yerleri ve tarihçeli tahsis (PM kararı K30; Yazılım Dokümanı OTP-01). Site kapsamında.

Bir park yeri aynı tarihte tek bölüme tahsislidir: tahsis yazılırken yerin satırı kilitlenir,
çakışan tahsis varsa 409 `space_taken`. Tahsis silinmez, bitirilir.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.parking import (
    ParkingKind,
    SpaceForm,
    check_allocation,
    check_end,
    overlaps,
    taken_error,
)
from site_yonetim.models import ParkingAllocation, ParkingSpace
from site_yonetim.services.security import require_unit, unit_names


def spaces_query(
    *, kind: ParkingKind | None, q: str | None, unit_id: uuid.UUID | None, today: date
) -> Select[ParkingSpace]:
    query = select(ParkingSpace)
    if kind is not None:
        query = query.where(ParkingSpace.kind == kind.value)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(ParkingSpace.code.ilike(f"%{escaped}%"))
    if unit_id is not None:
        query = query.where(
            ParkingSpace.id.in_(
                select(ParkingAllocation.space_id).where(
                    ParkingAllocation.unit_id == unit_id, *current(today)
                )
            )
        )
    return query.order_by(ParkingSpace.code, ParkingSpace.id)


def current(today: date) -> tuple[ColumnElement[bool], ...]:
    return (
        ParkingAllocation.start_date <= today,
        or_(ParkingAllocation.end_date.is_(None), ParkingAllocation.end_date >= today),
    )


async def taken_codes(session: AsyncSession, exclude: uuid.UUID | None) -> set[str]:
    query = select(ParkingSpace.code)
    if exclude is not None:
        query = query.where(ParkingSpace.id != exclude)
    return set(await session.scalars(query))


async def save_space(
    session: AsyncSession, space: ParkingSpace | None, form: SpaceForm, *, is_active: bool
) -> ParkingSpace:
    if space is None:
        space = ParkingSpace(is_active=True)
        session.add(space)
    else:
        space.is_active = is_active
    space.code, space.kind, space.location = form.code, form.kind.value, form.location
    space.is_paid, space.note = form.is_paid, form.note
    await session.flush()
    return space


async def get_space(
    session: AsyncSession, space_id: uuid.UUID, *, lock: bool = False
) -> ParkingSpace | None:
    query = select(ParkingSpace).where(ParkingSpace.id == space_id)
    row: ParkingSpace | None = await session.scalar(query.with_for_update() if lock else query)
    return row


@dataclass(frozen=True, slots=True)
class CurrentHolder:
    allocation_id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    start_date: date
    end_date: date | None


async def holders(
    session: AsyncSession, space_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, CurrentHolder]:
    if not space_ids:
        return {}
    rows = list(
        await session.scalars(
            select(ParkingAllocation).where(
                ParkingAllocation.space_id.in_(space_ids), *current(today)
            )
        )
    )
    names = await unit_names(session, {a.unit_id for a in rows})
    return {
        a.space_id: CurrentHolder(
            a.id, a.unit_id, names.get(a.unit_id, "—"), a.start_date, a.end_date
        )
        for a in rows
    }


async def allocate(
    session: AsyncSession,
    space: ParkingSpace,
    *,
    unit_id: uuid.UUID,
    start: date | None,
    end: date | None,
    note: str | None,
    actor: str,
) -> ParkingAllocation:
    begin = check_allocation(start, end)
    await require_unit(session, unit_id)
    if not space.is_active:
        raise taken_error(space.code, "park yeri kullanım dışı")
    others = list(
        await session.scalars(
            select(ParkingAllocation).where(ParkingAllocation.space_id == space.id)
        )
    )
    clash = next((a for a in others if overlaps(begin, end, a.start_date, a.end_date)), None)
    if clash is not None:
        name = (await unit_names(session, {clash.unit_id})).get(clash.unit_id, "—")
        raise taken_error(space.code, name)
    row = ParkingAllocation(
        space_id=space.id,
        unit_id=unit_id,
        start_date=begin,
        end_date=end,
        note=" ".join((note or "").split())[:200] or None,
        created_by=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def get_allocation(
    session: AsyncSession, allocation_id: uuid.UUID
) -> ParkingAllocation | None:
    row: ParkingAllocation | None = await session.scalar(
        select(ParkingAllocation).where(ParkingAllocation.id == allocation_id).with_for_update()
    )
    return row


async def end_allocation(
    session: AsyncSession, row: ParkingAllocation, *, end: date | None, actor: str
) -> ParkingAllocation:
    row.end_date = check_end(row.start_date, row.end_date, end)
    row.ended_by = actor
    await session.flush()
    return row


def allocations_query(
    *, space_id: uuid.UUID | None, unit_id: uuid.UUID | None
) -> Select[ParkingAllocation]:
    query = select(ParkingAllocation)
    if space_id is not None:
        query = query.where(ParkingAllocation.space_id == space_id)
    if unit_id is not None:
        query = query.where(ParkingAllocation.unit_id == unit_id)
    return query.order_by(ParkingAllocation.start_date.desc(), ParkingAllocation.id.desc())
