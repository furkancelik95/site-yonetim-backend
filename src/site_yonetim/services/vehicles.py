"""Araçlar (servis isteği 22, YAP-05) ve yapı ayarları satırı.

- Plaka normalize saklanır; biçim `^(01–81)[A-Z]{1,3}\\d{2,5}$`.
- Bölüm başına **kalıcı** plaka sınırı ayardan (1–10, varsayılan 2); misafir aracı sınıra
  girmez ama geçerlilik tarihi ister.
- Aynı plaka başka bölümde etkinse 409; personel `force` ile yine ekler, sakin ekleyemez.
- Sakin kaydı, ayar açıksa `pending` olur ve yönetim onaylar.
- Kaldırma/pasif silmez: `inactive` geçmişte kalır, kapı ekranında görünmez.
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import tr_lower
from site_yonetim.domain.vehicles import (
    PLATE_FORMAT_MESSAGE,
    VehicleKind,
    VehicleStatus,
    display_plate,
    is_valid_plate,
    normalize_plate,
)
from site_yonetim.models import Block, Person, StructureSettings, Unit, UnitParty, Vehicle


class VehicleRuleError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


async def settings(session: AsyncSession) -> StructureSettings:
    """Sitenin yapı ayarları satırı; yoksa varsayılanlarla açılır."""
    found: StructureSettings | None = await session.scalar(select(StructureSettings))
    if found is None:
        found = StructureSettings()
        session.add(found)
        await session.flush()
    return found


async def read_settings(session: AsyncSession) -> StructureSettings:
    """Okuma: satır yoksa yazmadan varsayılanlar (GET yan etkisiz)."""
    found: StructureSettings | None = await session.scalar(select(StructureSettings))
    return (
        found
        if found is not None
        else StructureSettings(
            plate_limit_per_unit=2,
            vehicle_approval_required=False,
            invite_enabled=True,
            qr_enabled=True,
            code_enabled=True,
            owner_can_approve_tenant=False,
            owner_document_required=False,
            invite_valid_days=7,
            kvkk_published=False,
        )
    )


@dataclass(frozen=True, slots=True)
class VehicleRow:
    vehicle: Vehicle
    unit_name: str
    owner_name: str | None


def list_query(
    *, unit_id: uuid.UUID | None, status: VehicleStatus | None, q: str | None
) -> Select[Vehicle, str, str, str | None, str | None]:
    query = (
        select(Vehicle, Unit.number, Block.name, Person.first_name, Person.last_name)
        .join(Unit, and_(Unit.id == Vehicle.unit_id, Unit.site_id == Vehicle.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .outerjoin(
            Person,
            and_(Person.id == Vehicle.owner_person_id, Person.site_id == Vehicle.site_id),
        )
    )
    if unit_id is not None:
        query = query.where(Vehicle.unit_id == unit_id)
    if status is not None:
        query = query.where(Vehicle.status == status.value)
    if q and q.strip():
        term = " ".join(q.split())
        plate = normalize_plate(term)
        compact = term.replace("-", "").replace(" ", "")
        like = f"%{_escape(compact)}%"
        conditions = [
            func.concat(Block.name, Unit.number).ilike(like),
            Person.search_name.like(f"%{_escape(tr_lower(term))}%"),
        ]
        if plate:
            conditions.append(Vehicle.plate.like(f"%{_escape(plate)}%"))
        query = query.where(or_(*conditions))
    return query.order_by(Vehicle.created_at.desc(), Vehicle.id.desc())


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def to_row(values: tuple[Vehicle, str, str, str | None, str | None]) -> VehicleRow:
    vehicle, number, block, first, last = values
    return VehicleRow(
        vehicle,
        f"{block}-{number}" if block else number,
        f"{first} {last}" if first and last else None,
    )


async def get_row(session: AsyncSession, vehicle_id: uuid.UUID) -> VehicleRow | None:
    row = (
        await session.execute(
            list_query(unit_id=None, status=None, q=None).where(Vehicle.id == vehicle_id)
        )
    ).first()
    return to_row(tuple(row)) if row else None  # type: ignore[arg-type]


async def default_owner(session: AsyncSession, unit_id: uuid.UUID, today: date) -> uuid.UUID | None:
    """Sahip belirtilmezse: bugünkü kiracı/oturan, yoksa malik."""
    rows = await session.execute(
        select(UnitParty.person_id, UnitParty.role)
        .where(
            UnitParty.unit_id == unit_id,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
            UnitParty.role != PartyRole.PROXY.value,
        )
        .order_by(UnitParty.start_date)
    )
    order = {PartyRole.TENANT.value: 0, PartyRole.RESIDENT.value: 1, PartyRole.OWNER.value: 2}
    candidates = sorted(rows, key=lambda r: order.get(r[1], 9))
    return candidates[0][0] if candidates else None


@dataclass(frozen=True, slots=True)
class NewVehicle:
    plate: str
    kind: VehicleKind
    brand_model: str | None = None
    color: str | None = None
    valid_until: date | None = None
    owner_person_id: uuid.UUID | None = None


async def add(
    session: AsyncSession,
    unit: Unit,
    data: NewVehicle,
    *,
    by_resident: bool,
    force: bool,
    created_by: str | None,
    today: date,
    import_batch_id: uuid.UUID | None = None,
) -> Vehicle:
    plate = normalize_plate(data.plate)
    if not is_valid_plate(plate):
        raise VehicleRuleError(422, "validation", PLATE_FORMAT_MESSAGE, "plate")
    if data.kind is VehicleKind.GUEST and data.valid_until is None:
        raise VehicleRuleError(
            422, "validation", "Misafir aracı için geçerlilik tarihi zorunlu.", "valid_until"
        )
    if data.valid_until is not None and data.valid_until < today:
        raise VehicleRuleError(
            422, "validation", "Geçerlilik tarihi bugünden önce olamaz.", "valid_until"
        )
    config = await read_settings(session)
    open_here = list(
        await session.scalars(
            select(Vehicle)
            .where(Vehicle.unit_id == unit.id, Vehicle.status != VehicleStatus.INACTIVE.value)
            .with_for_update()
        )
    )
    if any(v.plate == plate for v in open_here):
        raise VehicleRuleError(409, "already_registered", "Bu plaka bu bölümde zaten kayıtlı.")
    permanent = sum(1 for v in open_here if v.kind == VehicleKind.PERMANENT.value)
    if data.kind is VehicleKind.PERMANENT and permanent >= config.plate_limit_per_unit:
        limit = config.plate_limit_per_unit
        message = (
            f"Bölümünüz için plaka sınırına ulaştınız ({limit})."
            if by_resident
            else f"Bu bölüm plaka sınırına ulaştı ({limit}); önce bir aracı pasif yapın."
        )
        raise VehicleRuleError(422, "plate_limit", message)
    elsewhere = (
        await session.execute(
            select(Unit.number, Block.name)
            .join(Vehicle, and_(Vehicle.unit_id == Unit.id, Vehicle.site_id == Unit.site_id))
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(
                Vehicle.plate == plate,
                Vehicle.unit_id != unit.id,
                Vehicle.status != VehicleStatus.INACTIVE.value,
            )
            .limit(1)
        )
    ).first()
    if elsewhere is not None and (by_resident or not force):
        other = f"{elsewhere[1]}-{elsewhere[0]}" if elsewhere[1] else elsewhere[0]
        hint = " Site yönetimine başvurun." if by_resident else " Yine de eklemek için onaylayın."
        raise VehicleRuleError(
            409,
            "plate_active_elsewhere",
            f"{display_plate(plate)} plakası {other} bölümünde kayıtlı.{hint}",
        )
    status = (
        VehicleStatus.PENDING
        if by_resident and config.vehicle_approval_required
        else VehicleStatus.ACTIVE
    )
    vehicle = Vehicle(
        id=uuid.uuid7(),
        unit_id=unit.id,
        plate=plate,
        brand_model=_clean(data.brand_model),
        color=_clean(data.color),
        kind=data.kind.value,
        valid_until=data.valid_until,
        status=status.value,
        owner_person_id=data.owner_person_id or await default_owner(session, unit.id, today),
        created_by_name=created_by,
        import_batch_id=import_batch_id,
    )
    session.add(vehicle)
    await session.flush()
    return vehicle


def _clean(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    return text[:60] or None


async def active_count(session: AsyncSession, unit_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(Vehicle)
            .where(Vehicle.unit_id == unit_id, Vehicle.status != VehicleStatus.INACTIVE.value)
        )
        or 0
    )
