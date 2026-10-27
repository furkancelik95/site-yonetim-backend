"""Site yapısı servisleri: blok, bölüm, kişi, bölüm–kişi ilişkisi (docs/03 §3–§5).

Hepsi **açık site kapsamında** çağrılır (uç noktada `site_context`); filtre ve damga kiracı
katmanından gelir. Silme yok: bölüm `is_active=False`, ilişki `end_date` ile biter.
Toplama ve sayma veritabanında; sayfa başına sabit sayıda sorgu (N+1 yok, docs/08 §3).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.finance import PeriodStatus
from site_yonetim.domain.structure import (
    FULL_SHARE,
    SHARE_TOLERANCE,
    VERSIONED_FIELDS,
    PartyRole,
    StructureRuleError,
    accounts_to_open,
    check_owner_share,
    check_party_end,
    earliest_effective_date,
    most_common_count,
    reference_base,
    reference_code,
)
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import (
    Block,
    CommonArea,
    LedgerAccount,
    Period,
    Person,
    Unit,
    UnitAttributeChange,
    UnitParty,
    UnitType,
)


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --- Blok ---------------------------------------------------------------------


async def get_block(session: AsyncSession, block_id: uuid.UUID) -> Block | None:
    block: Block | None = await session.scalar(select(Block).where(Block.id == block_id))
    return block


async def ensure_block_name_free(
    session: AsyncSession, name: str, *, except_id: uuid.UUID | None = None
) -> None:
    # PostgreSQL lower() Türkçe I/İ'yi bilmez; sitedeki blok sayısı küçük, Python'da karşılaştır.
    key = tr_lower(" ".join(name.split()))
    rows = await session.execute(select(Block.id, Block.name))
    if any(tr_lower(" ".join(n.split())) == key and i != except_id for i, n in rows):
        raise StructureRuleError(
            "block_already_exists", "name", "Bu sitede aynı adlı bir blok zaten var."
        )


# --- Bölüm --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentParties:
    owners: list[Person]
    tenants: list[Person]


async def current_parties(
    session: AsyncSession, unit_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, CurrentParties]:
    """Verilen bölümlerin bugünkü malik ve kiracıları — tek sorgu."""
    result: dict[uuid.UUID, CurrentParties] = defaultdict(lambda: CurrentParties([], []))
    if not unit_ids:
        return result
    rows = await session.execute(
        select(UnitParty.unit_id, UnitParty.role, Person)
        .join(Person, and_(Person.id == UnitParty.person_id, Person.site_id == UnitParty.site_id))
        .where(
            UnitParty.unit_id.in_(unit_ids),
            UnitParty.role.in_([PartyRole.OWNER.value, PartyRole.TENANT.value]),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
        .order_by(Person.last_name, Person.first_name)
    )
    for unit_id, role, person in rows:
        bucket = result[unit_id]
        (bucket.owners if role == PartyRole.OWNER.value else bucket.tenants).append(person)
    return result


def units_query(
    *, block_id: uuid.UUID | None, is_active: bool | None, q: str | None
) -> Select[Unit, Block, UnitType]:
    query = (
        select(Unit, Block, UnitType)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .outerjoin(
            UnitType, and_(UnitType.id == Unit.unit_type_id, UnitType.site_id == Unit.site_id)
        )
    )
    if block_id is not None:
        query = query.where(Unit.block_id == block_id)
    if is_active is not None:
        query = query.where(Unit.is_active.is_(is_active))
    if q:
        term = q.strip()
        compact = term.replace("-", "").replace(" ", "")
        query = query.where(
            or_(
                Unit.number.ilike(_like(term)),
                func.concat(Block.name, Unit.number).ilike(_like(compact)),
                Unit.commercial_title.ilike(_like(term)),
            )
        )
    # Doğal sıra: blok sırası, sonra "2" < "10"
    return query.order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)


async def ensure_unit_number_free(
    session: AsyncSession, block_id: uuid.UUID, number: str, *, except_id: uuid.UUID | None = None
) -> None:
    query = select(Unit.id).where(Unit.block_id == block_id, Unit.number == number)
    if except_id is not None:
        query = query.where(Unit.id != except_id)
    if await session.scalar(query):
        raise StructureRuleError(
            "unit_already_exists", "number", "Bu blokta aynı numaralı bir bölüm zaten var."
        )


async def require_block(session: AsyncSession, block_id: uuid.UUID) -> Block:
    block = await get_block(session, block_id)
    if block is None:  # başka sitenin bloğu da burada "yok"tur
        raise StructureRuleError("block_not_found", "block_id", "Seçilen blok bulunamadı.")
    return block


async def require_unit_type(session: AsyncSession, unit_type_id: uuid.UUID) -> UnitType:
    unit_type = await session.scalar(select(UnitType).where(UnitType.id == unit_type_id))
    if unit_type is None:
        raise StructureRuleError(
            "unit_type_not_found", "unit_type_id", "Seçilen daire tipi bulunamadı."
        )
    return unit_type


def check_land_share(numerator: int | None, denominator: int | None) -> None:
    if (numerator is None) != (denominator is None):
        field = "land_share_denominator" if denominator is None else "land_share_numerator"
        raise StructureRuleError(
            "land_share_incomplete", field, "Arsa payı için pay ve payda birlikte girilmeli."
        )
    if numerator is not None and denominator is not None and not 0 < numerator <= denominator:
        raise StructureRuleError(
            "land_share_invalid",
            "land_share_numerator",
            "Arsa payı, paydadan büyük olamaz ve sıfırdan büyük olmalı.",
        )


# --- Kişi ---------------------------------------------------------------------


def people_query(q: str | None) -> Select[Person]:
    query = select(Person).where(Person.merged_into_id.is_(None))  # birleşen kişi görünmez
    if q:
        query = query.where(Person.search_name.like(_like(tr_lower(" ".join(q.split())))))
    return query.order_by(Person.last_name, Person.first_name, Person.id)


async def require_person(session: AsyncSession, person_id: uuid.UUID) -> Person:
    person = await session.scalar(
        select(Person).where(Person.id == person_id, Person.merged_into_id.is_(None))
    )
    if person is None:
        raise StructureRuleError("person_not_found", "person_id", "Seçilen kişi bulunamadı.")
    return person


# --- Bölüm–kişi ilişkisi -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AddedParty:
    party: UnitParty
    opened_accounts: list[LedgerAccount]


async def _active_parties(
    session: AsyncSession, unit_id: uuid.UUID, role: PartyRole, day: date
) -> list[UnitParty]:
    rows = await session.scalars(
        select(UnitParty).where(
            UnitParty.unit_id == unit_id,
            UnitParty.role == role.value,
            UnitParty.start_date <= day,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= day),
        )
    )
    return list(rows)


async def add_party(
    session: AsyncSession,
    *,
    unit: Unit,
    block: Block,
    person: Person,
    role: PartyRole,
    start_date: date,
    share_percent: Decimal = FULL_SHARE,
) -> AddedParty:
    """Bölüme malik/kiracı/oturan/vekil ekler ve gereken cari hesapları açar (docs/03 §5)."""
    if role is PartyRole.OWNER:
        owners = await _active_parties(session, unit.id, PartyRole.OWNER, start_date)
        if any(p.person_id == person.id for p in owners):
            raise StructureRuleError(
                "party_already_active", "person_id", "Bu kişi zaten bu bölümün maliki."
            )
        check_owner_share((p.share_percent for p in owners), share_percent)
    elif any(
        p.person_id == person.id for p in await _active_parties(session, unit.id, role, start_date)
    ):
        raise StructureRuleError(
            "party_already_active", "person_id", "Bu kişinin bu bölümde aynı rolde etkin kaydı var."
        )

    party = UnitParty(
        unit_id=unit.id,
        person_id=person.id,
        role=role.value,
        share_percent=share_percent if role is PartyRole.OWNER else FULL_SHARE,
        start_date=start_date,
    )
    session.add(party)

    has_tenant = bool(await _active_parties(session, unit.id, PartyRole.TENANT, start_date))
    base = reference_base(block.name, unit.number)
    taken = set(
        await session.scalars(
            select(LedgerAccount.reference_code).where(
                LedgerAccount.reference_code.like(f"{base}-%")
            )
        )
    )
    existing = set(
        await session.scalars(
            select(LedgerAccount.kind).where(
                LedgerAccount.unit_id == unit.id, LedgerAccount.person_id == person.id
            )
        )
    )
    opened: list[LedgerAccount] = []
    for kind, letter in accounts_to_open(role, unit_has_active_tenant=has_tenant):
        if kind.value in existing:  # aynı kişi yeniden eklendiyse hesabı zaten var
            continue
        code = reference_code(base, letter, taken)
        taken.add(code)
        account = LedgerAccount(
            unit_id=unit.id, person_id=person.id, kind=kind.value, reference_code=code
        )
        session.add(account)
        opened.append(account)
    await session.flush()
    return AddedParty(party, opened)


async def end_party(session: AsyncSession, party: UnitParty, end_date: date) -> UnitParty:
    """Kiracı çıkışı vb.: kayıt silinmez, bitiş tarihi girilir (docs/03 §4)."""
    check_party_end(party.start_date, party.end_date, end_date)
    party.end_date = end_date
    await session.flush()
    return party


async def unit_accounts(session: AsyncSession, unit_id: uuid.UUID) -> list[LedgerAccount]:
    rows = await session.scalars(
        select(LedgerAccount)
        .where(LedgerAccount.unit_id == unit_id)
        .order_by(LedgerAccount.reference_code)
    )
    return list(rows)


# --- Ad tekilliği (ortak alan, bölüm tipi) --------------------------------------------


def name_key(name: str) -> str:
    return tr_lower(" ".join(name.split()))


async def ensure_unit_type_name_free(
    session: AsyncSession, name: str, *, except_id: uuid.UUID | None = None
) -> None:
    rows = await session.execute(select(UnitType.id, UnitType.name))
    if any(name_key(n) == name_key(name) and i != except_id for i, n in rows):
        raise StructureRuleError("unit_type_exists", "name", "Bu adla bir bölüm tipi var.")


async def ensure_common_area_name_free(
    session: AsyncSession, name: str, *, except_id: uuid.UUID | None = None
) -> None:
    rows = await session.execute(select(CommonArea.id, CommonArea.name))
    if any(name_key(n) == name_key(name) and i != except_id for i, n in rows):
        raise StructureRuleError("common_area_exists", "name", "Bu adla bir ortak alan var.")


# --- Yürürlük tarihli öznitelikler (YAP-02 S3) ----------------------------------------


async def closed_months(session: AsyncSession) -> list[tuple[int, int]]:
    rows = await session.execute(
        select(Period.year, Period.month).where(Period.status == PeriodStatus.CLOSED.value)
    )
    return [(y, m) for y, m in rows]


async def earliest_effective(session: AsyncSession, today: date) -> date:
    return earliest_effective_date(today, await closed_months(session))


def _attr_text(name: str, value: Any) -> Any:
    """Değeri geçmişte saklanacak JSON biçimine çevirir (m² metin, kimlik metin)."""
    if value is None:
        return None
    if name in ("gross_area", "net_area"):
        return f"{Decimal(value):.2f}"
    if name == "unit_type_id":
        return str(value)
    return int(value)


def _attr_value(name: str, value: Any) -> Any:
    """Geçmişteki JSON değeri bölüm sütununa yazılacak tipe çevirir."""
    if value is None:
        return None
    if name in ("gross_area", "net_area"):
        return Decimal(value)
    if name == "unit_type_id":
        return uuid.UUID(value)
    return int(value)


def attribute_changes(unit: Unit, requested: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Gönderilen ve gerçekten değişen alanlar: `{alan: {"from": eski, "to": yeni}}`."""
    changes: dict[str, dict[str, Any]] = {}
    for name in VERSIONED_FIELDS:
        if name not in requested:
            continue
        old = _attr_text(name, getattr(unit, name))
        new = _attr_text(name, requested[name])
        if old != new:
            changes[name] = {"from": old, "to": new}
    return changes


def apply_changes(unit: Unit, changes: dict[str, dict[str, Any]]) -> None:
    for name, change in changes.items():
        if name in VERSIONED_FIELDS:
            setattr(unit, name, _attr_value(name, change["to"]))


async def record_attribute_change(
    session: AsyncSession,
    unit: Unit,
    changes: dict[str, dict[str, Any]],
    *,
    effective_from: date,
    today: date,
    now: datetime,
    actor_name: str | None,
    user_id: uuid.UUID | None,
) -> UnitAttributeChange:
    """Geçmiş satırı yazar; yürürlük tarihi bugün ya da önceyse (açık ay içi) hemen uygular."""
    applied = effective_from <= today
    change = UnitAttributeChange(
        unit_id=unit.id,
        effective_from=effective_from,
        changes=changes,
        changed_by_name=actor_name,
        user_id=user_id,
        applied=applied,
        applied_at=now if applied else None,
    )
    session.add(change)
    if applied:
        apply_changes(unit, changes)
    await session.flush()
    return change


async def apply_due_attribute_changes(session: AsyncSession, today: date, now: datetime) -> int:
    """Tarihi gelen ileri tarihli değişiklikleri sırasıyla uygular (zamanlanmış iş ve tahakkuk
    öncesi). Uygulanan satır sayısını döner."""
    pending = list(
        await session.scalars(
            select(UnitAttributeChange)
            .where(
                UnitAttributeChange.applied.is_(False),
                UnitAttributeChange.effective_from <= today,
            )
            .order_by(UnitAttributeChange.effective_from, UnitAttributeChange.created_at)
        )
    )
    if not pending:
        return 0
    unit_ids = {c.unit_id for c in pending}
    units = {u.id: u for u in await session.scalars(select(Unit).where(Unit.id.in_(unit_ids)))}
    for change in pending:
        unit = units.get(change.unit_id)
        if unit is not None:
            apply_changes(unit, change.changes)
        change.applied = True
        change.applied_at = now
    await session.flush()
    return len(pending)


# --- Yapı özeti (istek 19; canlıya geçiş kontrol listesi buradan beslenir) -------------


@dataclass(frozen=True, slots=True)
class UnitRef:
    id: uuid.UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class ShareGap:
    id: uuid.UUID
    display_name: str
    total: Decimal


@dataclass(slots=True)
class StructureSummary:
    unit_count: int = 0
    active_unit_count: int = 0
    block_count: int = 0
    land_share_sum: int = 0
    land_share_denominator: int | None = None
    units_without_land_share: int = 0
    units_without_owner: list[UnitRef] = field(default_factory=list)
    units_shares_incomplete: list[ShareGap] = field(default_factory=list)
    common_area_count: int = 0

    @property
    def land_share_mismatch(self) -> bool:
        return (
            self.land_share_denominator is not None
            and self.land_share_sum != self.land_share_denominator
        )


def owner_share_totals(today: date) -> Select[uuid.UUID, Decimal]:
    """Bölüm başına bugünkü maliklerin hisse toplamı."""
    return (
        select(UnitParty.unit_id, func.sum(UnitParty.share_percent).label("total"))
        .where(
            UnitParty.role == PartyRole.OWNER.value,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
        .group_by(UnitParty.unit_id)
    )


async def structure_summary(session: AsyncSession, today: date) -> StructureSummary:
    """Etkin bölümler üzerinden; sayım ve toplamlar veritabanında (docs/08)."""
    summary = StructureSummary()
    summary.unit_count = await session.scalar(select(func.count()).select_from(Unit)) or 0
    summary.block_count = await session.scalar(select(func.count()).select_from(Block)) or 0
    summary.common_area_count = (
        await session.scalar(
            select(func.count()).select_from(CommonArea).where(CommonArea.is_active)
        )
        or 0
    )
    active, share_sum, without_share = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Unit.land_share_numerator), 0),
                func.count().filter(Unit.land_share_numerator.is_(None)),
            ).where(Unit.is_active)
        )
    ).one()
    summary.active_unit_count = int(active or 0)
    summary.land_share_sum = int(share_sum or 0)
    summary.units_without_land_share = int(without_share or 0)
    denominators = await session.execute(
        select(Unit.land_share_denominator, func.count())
        .where(Unit.is_active, Unit.land_share_denominator.is_not(None))
        .group_by(Unit.land_share_denominator)
    )
    summary.land_share_denominator = most_common_count(
        {int(d): int(n) for d, n in denominators if d is not None}
    )
    shares = owner_share_totals(today).subquery()
    rows = await session.execute(
        select(Unit.id, Unit.number, Block.name, shares.c.total)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .outerjoin(shares, shares.c.unit_id == Unit.id)
        .where(
            Unit.is_active,
            or_(
                shares.c.total.is_(None),
                func.abs(shares.c.total - FULL_SHARE) > SHARE_TOLERANCE,
            ),
        )
        .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
    )
    for unit_id, number, block_name, total in rows:
        name = f"{block_name}-{number}" if block_name else number
        if total is None:
            summary.units_without_owner.append(UnitRef(unit_id, name))
        else:
            summary.units_shares_incomplete.append(ShareGap(unit_id, name, Decimal(total)))
    return summary


# --- Toplu düzenleme (YAP-02: tip, kullanım, durum) ------------------------------------


@dataclass(frozen=True, slots=True)
class BulkFailure:
    unit_id: uuid.UUID
    message: str


async def bulk_update(
    session: AsyncSession, unit_ids: Iterable[uuid.UUID], changes: dict[str, Any]
) -> tuple[int, list[BulkFailure]]:
    """Satır satır uygular; bulunamayan bölüm `failed`'e düşer, diğerleri geri alınmaz."""
    ids = list(dict.fromkeys(unit_ids))
    units = {u.id: u for u in await session.scalars(select(Unit).where(Unit.id.in_(ids)))}
    failed: list[BulkFailure] = []
    updated = 0
    for unit_id in ids:
        unit = units.get(unit_id)
        if unit is None:  # başka sitenin bölümü de burada "yok"tur
            failed.append(BulkFailure(unit_id, "Bölüm bulunamadı."))
            continue
        for key, value in changes.items():
            setattr(unit, key, value)
        updated += 1
    await session.flush()
    return updated, failed
