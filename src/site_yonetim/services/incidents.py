"""Güvenlik olay kaydı ve kayıp eşya — frontend servis istekleri 09, 10. Açık site kapsamında;
transaction'ı çağıran yönetir.

- Numara site içinde artan: işlem kilidi (advisory lock) altında `max + 1`, `(site, number)`
  benzersizliği ikinci güvence.
- Kayıtlar silinmez, içerikleri değişmez. Olay yalnız kapatılır (not zorunlu); kayıp eşya
  yalnız bekliyorken teslim edilir ya da elden çıkarılır. Değişiklikler denetim kaydında.
- Olay açıklamasına kişisel veri yazılabilir; saklama süresi açık karar K8.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import IncidentKind, IncidentStatus, LostItemStatus
from site_yonetim.models import Incident, LostItem, Unit

LOCATION_MAX, DESCRIPTION_MAX, NOTE_MAX, NAME_MAX = 200, 2000, 1000, 100
_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def _clean(value: str | None, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


async def _next_number(session: AsyncSession, model: type[Incident] | type[LostItem]) -> int:
    scope = current_scope()
    await session.execute(
        _NUMBER_LOCK, {"key": f"{model.__tablename__}:{scope.site_id if scope else ''}"}
    )
    current = await session.scalar(select(func.max(model.number)))
    return (current or 0) + 1


# --- Olay ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewIncident:
    kind: str | None
    location: str | None
    description: str | None
    occurred_at: datetime | None
    unit_id: uuid.UUID | None


async def create_incident(
    session: AsyncSession, data: NewIncident, *, now: datetime, recorded_by: str
) -> Incident:
    if data.kind not in {k.value for k in IncidentKind}:
        raise OperationRuleError("invalid_kind", "Olay türünü seçin.", field="kind")
    location = _clean(data.location, LOCATION_MAX)
    if not location:
        raise OperationRuleError("location_required", "Olayın yerini yazın.", field="location")
    description = _clean(data.description, DESCRIPTION_MAX)
    if len(description) < 5:
        raise OperationRuleError(
            "description_required", "Ne olduğunu kısaca yazın.", field="description"
        )
    if (
        data.unit_id is not None
        and await session.scalar(select(Unit.id).where(Unit.id == data.unit_id)) is None
    ):
        raise OperationRuleError("unit_not_found", "Seçilen bölüm bulunamadı.", field="unit_id")
    incident = Incident(
        id=uuid.uuid7(),
        number=await _next_number(session, Incident),
        kind=data.kind,
        location=location,
        description=description,
        occurred_at=data.occurred_at or now,
        unit_id=data.unit_id,
        status=IncidentStatus.OPEN.value,
        recorded_by=recorded_by,
    )
    session.add(incident)
    await session.flush()
    return incident


async def get_incident(
    session: AsyncSession, incident_id: uuid.UUID, *, lock: bool = False
) -> Incident | None:
    query = select(Incident).where(Incident.id == incident_id)
    found: Incident | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def close_incident(
    session: AsyncSession, incident: Incident, *, note: str | None, closed_by: str, now: datetime
) -> Incident:
    if incident.status == IncidentStatus.CLOSED.value:
        raise OperationRuleError("already_closed", "Olay zaten kapatıldı.", conflict=True)
    text_ = _clean(note, NOTE_MAX)
    if not text_:
        raise OperationRuleError("note_required", "Ne yapıldığını yazın.", field="note")
    incident.status = IncidentStatus.CLOSED.value
    incident.closed_note = text_
    incident.closed_at = now
    incident.closed_by = closed_by
    await session.flush()
    return incident


def incidents_query(status: IncidentStatus | None) -> Select[Incident]:
    query = select(Incident)
    if status is not None:
        query = query.where(Incident.status == status.value)
    return query.order_by(Incident.occurred_at.desc(), Incident.number.desc())


# --- Kayıp eşya ---------------------------------------------------------------------


async def create_lost_item(
    session: AsyncSession,
    *,
    description: str | None,
    location: str | None,
    found_by: str | None,
    found_at: datetime | None,
    now: datetime,
    recorded_by: str,
) -> LostItem:
    text_ = _clean(description, LOCATION_MAX)
    if not text_:
        raise OperationRuleError("description_required", "Eşyayı tarif edin.", field="description")
    place = _clean(location, LOCATION_MAX)
    if not place:
        raise OperationRuleError("location_required", "Bulunduğu yeri yazın.", field="location")
    item = LostItem(
        id=uuid.uuid7(),
        number=await _next_number(session, LostItem),
        description=text_,
        location=place,
        found_by=_clean(found_by, NAME_MAX) or None,
        found_at=found_at or now,
        status=LostItemStatus.WAITING.value,
        recorded_by=recorded_by,
    )
    session.add(item)
    await session.flush()
    return item


async def get_lost_item(
    session: AsyncSession, item_id: uuid.UUID, *, lock: bool = False
) -> LostItem | None:
    query = select(LostItem).where(LostItem.id == item_id)
    found: LostItem | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def settle_lost_item(
    session: AsyncSession,
    item: LostItem,
    *,
    returned_to: str | None,
    disposed: bool,
    now: datetime,
) -> LostItem:
    """Teslim (`returned_to`) ya da elden çıkarma (`disposed`)."""
    if item.status != LostItemStatus.WAITING.value:
        raise OperationRuleError(
            "not_waiting",
            "Bu eşya zaten teslim edildi ya da elden çıkarıldı.",
            conflict=True,
        )
    if disposed:
        item.status = LostItemStatus.DISPOSED.value
    else:
        name = _clean(returned_to, NAME_MAX)
        if not name:
            raise OperationRuleError(
                "returned_to_required", "Teslim alanın adını yazın.", field="returned_to"
            )
        item.status = LostItemStatus.RETURNED.value
        item.returned_to = name
    item.returned_at = now
    await session.flush()
    return item


def lost_items_query(status: LostItemStatus | None) -> Select[LostItem]:
    query = select(LostItem)
    if status is not None:
        query = query.where(LostItem.status == status.value)
    return query.order_by(LostItem.found_at.desc(), LostItem.number.desc())
