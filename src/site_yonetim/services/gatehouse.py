"""Kapılar, güvenlik ayarları, kargo teslim yetkilisi, uzun bekleyen kargo ve bildirim alıcıları
(servis istekleri 40, 44). Site kapsamında; transaction'ı çağıran yönetir.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.access import Permission
from site_yonetim.domain.gatehouse import (
    DELEGATE_LIMIT,
    ParcelMethod,
    SecuritySettingsData,
    UnannouncedMethod,
    VisitorFields,
    check_gate,
    check_person_name,
)
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import PackageStatus
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import (
    Block,
    Gate,
    Package,
    ParcelDelegate,
    SecuritySettings,
    Site,
    SiteMembership,
    UnitParty,
)
from site_yonetim.services import roles as roles_svc
from site_yonetim.services.security import unit_names

# --- Ayarlar -------------------------------------------------------------------------------


def _to_data(row: SecuritySettings) -> SecuritySettingsData:
    fields = row.visitor_fields or {}
    return SecuritySettingsData(
        unannounced_method=UnannouncedMethod(row.unannounced_method),
        approval_timeout_seconds=row.approval_timeout_seconds,
        gate_methods={k: UnannouncedMethod(v) for k, v in (row.gate_methods or {}).items()},
        parcel_method=ParcelMethod(row.parcel_method),
        visitor_fields=VisitorFields(
            plate=bool(fields.get("plate", True)),
            phone=bool(fields.get("phone", False)),
            id_seen=bool(fields.get("id_seen", False)),
        ),
        retention_months=row.retention_months,
        auto_anonymize=row.auto_anonymize,
        parcel_long_wait_days=row.parcel_long_wait_days,
        guest_parking_capacity=row.guest_parking_capacity,
    )


@dataclass(frozen=True, slots=True)
class SettingsState:
    data: SecuritySettingsData
    version: int  # 0: hiç kaydedilmemiş (varsayılanlar)
    updated_at: datetime | None
    updated_by: str | None


async def load_settings(session: AsyncSession) -> SettingsState:
    row = await session.scalar(select(SecuritySettings))
    if row is None:
        return SettingsState(SecuritySettingsData(), 0, None, None)
    return SettingsState(
        _to_data(row), row.version, row.updated_at or row.created_at, row.updated_by
    )


def _values(data: SecuritySettingsData) -> dict[str, Any]:
    return {
        "unannounced_method": data.unannounced_method.value,
        "approval_timeout_seconds": data.approval_timeout_seconds,
        "gate_methods": {k: v.value for k, v in data.gate_methods.items()},
        "parcel_method": data.parcel_method.value,
        "visitor_fields": {
            "plate": data.visitor_fields.plate,
            "phone": data.visitor_fields.phone,
            "id_seen": data.visitor_fields.id_seen,
        },
        "retention_months": data.retention_months,
        "auto_anonymize": data.auto_anonymize,
        "parcel_long_wait_days": data.parcel_long_wait_days,
        "guest_parking_capacity": data.guest_parking_capacity,
    }


async def save_settings(
    session: AsyncSession, data: SecuritySettingsData, *, actor: str
) -> SettingsState:
    """Ayarı yazar; her kayıtta sürüm artar (önceki değerler denetim kaydında)."""
    row = await session.scalar(select(SecuritySettings).with_for_update())
    values = _values(data)
    if row is None:
        row = SecuritySettings(**values, version=1, updated_by=actor)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.version += 1
        row.updated_by = actor
    await session.flush()
    await session.refresh(row)
    return SettingsState(_to_data(row), row.version, row.updated_at or row.created_at, actor)


async def gate_ids(session: AsyncSession) -> set[str]:
    return {str(g) for g in await session.scalars(select(Gate.id))}


# --- Kapılar -------------------------------------------------------------------------------


async def list_gates(session: AsyncSession) -> list[Gate]:
    return list(await session.scalars(select(Gate).order_by(Gate.name_key)))


async def _taken(session: AsyncSession, exclude: uuid.UUID | None) -> set[str]:
    query = select(Gate.name_key)
    if exclude is not None:
        query = query.where(Gate.id != exclude)
    return set(await session.scalars(query))


async def _blocks(session: AsyncSession, block_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unique = list(dict.fromkeys(block_ids))
    if not unique:
        return []
    found = set(await session.scalars(select(Block.id).where(Block.id.in_(unique))))
    if len(found) != len(unique):
        raise OperationRuleError("block_not_found", "Seçilen blok bulunamadı.", field="block_ids")
    return unique


@dataclass(frozen=True, slots=True)
class GateInput:
    name: str | None
    kind: str | None
    block_ids: list[uuid.UUID]
    always_open: bool
    is_active: bool


async def save_gate(session: AsyncSession, gate: Gate | None, data: GateInput) -> Gate:
    name, kind = check_gate(data.name, data.kind, await _taken(session, gate.id if gate else None))
    blocks = await _blocks(session, data.block_ids)
    if gate is None:
        gate = Gate(
            name=name,
            name_key=tr_lower(name),
            kind=kind.value,
            block_ids=blocks,
            always_open=data.always_open,
            is_active=True,
        )
        session.add(gate)
    else:
        gate.name, gate.name_key, gate.kind = name, tr_lower(name), kind.value
        gate.block_ids = blocks
        gate.always_open = data.always_open
        gate.is_active = data.is_active
    await session.flush()
    return gate


async def get_gate(session: AsyncSession, gate_id: uuid.UUID) -> Gate | None:
    row: Gate | None = await session.scalar(select(Gate).where(Gate.id == gate_id))
    return row


async def require_gate(session: AsyncSession, gate_id: uuid.UUID | None) -> None:
    if gate_id is not None and await get_gate(session, gate_id) is None:
        raise OperationRuleError("gate_not_found", "Seçilen kapı bulunamadı.", field="gate_id")


# --- Bildirim alıcıları -------------------------------------------------------------------


def _active_party(today: date) -> Any:
    return (
        UnitParty.start_date <= today,
        or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        UnitParty.role != PartyRole.PROXY.value,
    )


async def unit_user_ids(
    session: AsyncSession, unit_ids: set[uuid.UUID], today: date
) -> set[uuid.UUID]:
    """Bölümün bugünkü malik/kiracı/oturanlarının giriş hesapları (etkin sakin üyeliği)."""
    if not unit_ids:
        return set()
    persons = select(UnitParty.person_id).where(
        UnitParty.unit_id.in_(unit_ids), *_active_party(today)
    )
    rows = await session.scalars(
        select(SiteMembership.user_id).where(
            SiteMembership.person_id.in_(persons), SiteMembership.is_active
        )
    )
    return set(rows)


async def manager_user_ids(session: AsyncSession, site: Site) -> set[uuid.UUID]:
    """Site yöneticileri (modül yönetme izni olanlar): açık ve şirketten gelen üyelikler."""
    return await roles_svc.permission_holders(session, site, Permission.MODULES_MANAGE)


# --- Kargo teslim yetkilisi ---------------------------------------------------------------


async def delegates(session: AsyncSession, unit_ids: set[uuid.UUID]) -> list[ParcelDelegate]:
    if not unit_ids:
        return []
    return list(
        await session.scalars(
            select(ParcelDelegate)
            .where(ParcelDelegate.unit_id.in_(unit_ids), ParcelDelegate.is_active)
            .order_by(ParcelDelegate.created_at, ParcelDelegate.id)
        )
    )


async def add_delegate(
    session: AsyncSession, *, unit_id: uuid.UUID, name: str | None, person_id: uuid.UUID, actor: str
) -> ParcelDelegate:
    clean = check_person_name(name, "name", "Yetkili kişinin adını yazın.")
    # Aynı bölüme eşzamanlı eklemeler sınırı aşmasın: bölümün yetkilileri kilitlenir.
    await session.execute(
        select(ParcelDelegate.id)
        .where(ParcelDelegate.unit_id == unit_id, ParcelDelegate.is_active)
        .with_for_update()
    )
    count = await session.scalar(
        select(func.count())
        .select_from(ParcelDelegate)
        .where(ParcelDelegate.unit_id == unit_id, ParcelDelegate.is_active)
    )
    if (count or 0) >= DELEGATE_LIMIT:
        raise OperationRuleError(
            "delegate_limit", "En fazla 3 teslim yetkilisi tanımlanabilir.", conflict=True
        )
    row = ParcelDelegate(
        unit_id=unit_id, name=clean, created_by_person_id=person_id, created_by_name=actor
    )
    session.add(row)
    await session.flush()
    return row


async def remove_delegate(
    session: AsyncSession, row: ParcelDelegate, *, now: datetime, actor: str
) -> ParcelDelegate:
    if not row.is_active:
        raise OperationRuleError(
            "delegate_removed", "Bu kişinin yetkisi zaten kaldırılmış.", conflict=True
        )
    row.is_active = False
    row.removed_at = now
    row.removed_by = actor
    await session.flush()
    return row


# --- Uzun bekleyen kargo ------------------------------------------------------------------

LONG_WAIT_LIMIT = 200


@dataclass(frozen=True, slots=True)
class LongWaiting:
    package: Package
    unit_name: str
    waiting_days: int
    delegates: list[str]


async def long_waiting(session: AsyncSession, *, days: int, now: datetime) -> list[LongWaiting]:
    """Ayardaki günden uzun teslim bekleyen kargolar (en eski üstte)."""
    rows = list(
        await session.scalars(
            select(Package)
            .where(
                Package.status == PackageStatus.WAITING.value,
                Package.received_at < now - timedelta(days=days),
            )
            .order_by(Package.received_at, Package.id)
            .limit(LONG_WAIT_LIMIT)
        )
    )
    units = {p.unit_id for p in rows}
    names = await unit_names(session, units)
    by_unit: dict[uuid.UUID, list[str]] = {}
    for d in await delegates(session, units):
        by_unit.setdefault(d.unit_id, []).append(d.name)
    return [
        LongWaiting(
            p,
            names.get(p.unit_id, "—"),
            int((now - p.received_at).total_seconds() // 86400),
            by_unit.get(p.unit_id, []),
        )
        for p in rows
    ]
