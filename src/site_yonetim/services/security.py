"""Kapı işleri — kargo, ziyaretçi, güvenlik için daire arama (docs/06 §2.12). Site kapsamında.

- Teslim kodu `secrets` ile üretilir ve güvenliğe gösterilmez; sakin `resident/packages`
  üzerinden görür, teslimde güvenlik kodu sakinden alır.
- Daire araması yalnız bölüm adı ve oturan adlarını döner (borç, telefon yok — docs/05).
"""

import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import (
    PICKUP_CODE_MAX,
    PICKUP_CODE_MIN,
    PackageStatus,
    VisitorKind,
    VisitorStatus,
    check_delivery,
    check_enter,
    check_exit,
)
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import tr_lower, tr_upper
from site_yonetim.models import Block, Package, Person, Unit, UnitParty, Visitor

LOOKUP_LIMIT = 20
OCCUPANT_ROLES = (PartyRole.TENANT.value, PartyRole.RESIDENT.value)


def _clean(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def new_pickup_code() -> str:
    return str(secrets.randbelow(PICKUP_CODE_MAX - PICKUP_CODE_MIN + 1) + PICKUP_CODE_MIN)


async def _require_unit(session: AsyncSession, unit_id: uuid.UUID) -> None:
    if await session.scalar(select(Unit.id).where(Unit.id == unit_id)) is None:
        raise OperationRuleError("unit_not_found", "Seçilen bölüm bulunamadı.", field="unit_id")


async def _require_person(session: AsyncSession, person_id: uuid.UUID | None, field: str) -> None:
    if (
        person_id is not None
        and await session.scalar(select(Person.id).where(Person.id == person_id)) is None
    ):
        raise OperationRuleError("person_not_found", "Seçilen kişi bulunamadı.", field=field)


async def unit_names(session: AsyncSession, unit_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not unit_ids:
        return {}
    rows = await session.execute(
        select(Unit.id, Unit.number, Block.name)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(Unit.id.in_(unit_ids))
    )
    return {uid: f"{block}-{number}" if block else number for uid, number, block in rows}


# --- Kargo --------------------------------------------------------------------------


async def receive_package(
    session: AsyncSession,
    *,
    unit_id: uuid.UUID,
    person_id: uuid.UUID | None,
    carrier: str | None,
    note: str | None,
    now: datetime,
    received_by: str,
) -> Package:
    await _require_unit(session, unit_id)
    await _require_person(session, person_id, "person_id")
    package = Package(
        unit_id=unit_id,
        person_id=person_id,
        carrier=_clean(carrier, 60),
        pickup_code=new_pickup_code(),
        status=PackageStatus.WAITING.value,
        received_at=now,
        received_by=received_by,
        note=_clean(note, 500),
        notification_sent=False,  # K5: bildirim gönderilmiyor; sakin kodu kendi ekranında görür
    )
    session.add(package)
    await session.flush()
    return package


async def get_package(session: AsyncSession, package_id: uuid.UUID) -> Package | None:
    row: Package | None = await session.scalar(
        select(Package).where(Package.id == package_id).with_for_update()
    )
    return row


async def deliver_package(
    session: AsyncSession,
    package: Package,
    *,
    pickup_code: str,
    delivered_to: str,
    now: datetime,
    delivered_by: str,
) -> Package:
    check_delivery(PackageStatus(package.status), package.pickup_code, pickup_code)
    name = _clean(delivered_to, 80)
    if not name:
        raise OperationRuleError(
            "delivered_to_required", "Teslim alan kişinin adını yazın.", field="delivered_to"
        )
    package.status = PackageStatus.DELIVERED.value
    package.delivered_at = now
    package.delivered_by = delivered_by
    package.delivered_to = name
    await session.flush()
    return package


def packages_query(
    *, status: PackageStatus | None, unit_ids: set[uuid.UUID] | None = None
) -> Select[Package]:
    query = select(Package)
    if status is not None:
        query = query.where(Package.status == status.value)
    if unit_ids is not None:
        query = query.where(Package.unit_id.in_(unit_ids))
    return query.order_by(Package.received_at.desc(), Package.id.desc())


async def person_unit_ids(
    session: AsyncSession, person_id: uuid.UUID, today: date
) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(UnitParty.unit_id).where(
            UnitParty.person_id == person_id,
            UnitParty.role != PartyRole.PROXY.value,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    return set(rows)


# --- Ziyaretçi ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewVisitor:
    unit_id: uuid.UUID
    full_name: str
    kind: VisitorKind
    host_person_id: uuid.UUID | None = None
    phone: str | None = None
    plate_number: str | None = None
    expected_on: date | None = None
    note: str | None = None
    enter_now: bool = True


async def record_visitor(
    session: AsyncSession, data: NewVisitor, *, now: datetime, today: date, recorded_by: str
) -> Visitor:
    await _require_unit(session, data.unit_id)
    await _require_person(session, data.host_person_id, "host_person_id")
    name = _clean(data.full_name, 80)
    if not name or len(name) < 2:
        raise OperationRuleError(
            "invalid_name", "Ziyaretçinin adını yazın (en az 2 karakter).", field="full_name"
        )
    plate = tr_upper("".join((data.plate_number or "").split()))[:15] or None
    enter = data.enter_now and data.expected_on in (None, today)
    visitor = Visitor(
        unit_id=data.unit_id,
        host_person_id=data.host_person_id,
        full_name=name,
        phone=_clean(data.phone, 20),
        plate_number=plate,
        kind=data.kind.value,
        expected_on=data.expected_on,
        visit_date=data.expected_on or today,
        status=(VisitorStatus.ENTERED if enter else VisitorStatus.EXPECTED).value,
        entered_at=now if enter else None,
        recorded_by=recorded_by,
        note=_clean(data.note, 500),
    )
    session.add(visitor)
    await session.flush()
    return visitor


async def get_visitor(session: AsyncSession, visitor_id: uuid.UUID) -> Visitor | None:
    row: Visitor | None = await session.scalar(
        select(Visitor).where(Visitor.id == visitor_id).with_for_update()
    )
    return row


async def enter(session: AsyncSession, visitor: Visitor, now: datetime) -> Visitor:
    check_enter(VisitorStatus(visitor.status))
    visitor.status = VisitorStatus.ENTERED.value
    visitor.entered_at = now
    await session.flush()
    return visitor


async def exit_(session: AsyncSession, visitor: Visitor, now: datetime) -> Visitor:
    check_exit(VisitorStatus(visitor.status))
    visitor.status = VisitorStatus.EXITED.value
    visitor.exited_at = now
    await session.flush()
    return visitor


def visitors_query(day: date, status: VisitorStatus | None) -> Select[Visitor]:
    query = select(Visitor).where(Visitor.visit_date == day)
    if status is not None:
        query = query.where(Visitor.status == status.value)
    return query.order_by(Visitor.created_at.desc(), Visitor.id.desc())


# --- Daire arama (güvenlik) -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LookupRow:
    unit_id: uuid.UUID
    unit_name: str
    occupants: list[str]


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def lookup(session: AsyncSession, q: str, today: date) -> list[LookupRow]:
    """Bölüm (`A-12`, `A12`, `12`) ya da oturan adıyla arama. Oturan: kiracı/oturan, yoksa malik."""
    term = " ".join(q.split())
    compact = term.replace("-", "").replace(" ", "")
    active = and_(
        UnitParty.start_date <= today,
        or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        UnitParty.role != PartyRole.PROXY.value,
    )
    by_person = (
        select(UnitParty.unit_id)
        .join(Person, and_(Person.id == UnitParty.person_id, Person.site_id == UnitParty.site_id))
        .where(active, Person.search_name.like(_like(tr_lower(term))))
    )
    units = list(
        await session.execute(
            select(Unit.id, Unit.number, Block.name)
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(
                Unit.is_active,
                or_(
                    Unit.number.ilike(_like(term)),
                    func.concat(Block.name, Unit.number).ilike(_like(compact)),
                    Unit.id.in_(by_person),
                ),
            )
            .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
            .limit(LOOKUP_LIMIT)
        )
    )
    ids = [uid for uid, _, _ in units]
    parties: dict[uuid.UUID, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    if ids:
        for unit_id, role, first, last in await session.execute(
            select(UnitParty.unit_id, UnitParty.role, Person.first_name, Person.last_name)
            .join(
                Person, and_(Person.id == UnitParty.person_id, Person.site_id == UnitParty.site_id)
            )
            .where(UnitParty.unit_id.in_(ids), active)
            .order_by(Person.last_name, Person.first_name)
        ):
            kind = "occupant" if role in OCCUPANT_ROLES else "owner"
            parties[unit_id][kind].append(f"{first} {last}")
    return [
        LookupRow(
            uid,
            f"{block}-{number}" if block else number,
            parties[uid]["occupant"] or parties[uid]["owner"],
        )
        for uid, number, block in units
    ]
