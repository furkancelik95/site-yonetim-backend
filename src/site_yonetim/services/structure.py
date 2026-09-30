"""Site yapısı servisleri: blok, bölüm, kişi, bölüm–kişi ilişkisi (docs/03 §3–§5).

Hepsi **açık site kapsamında** çağrılır (uç noktada `site_context`); filtre ve damga kiracı
katmanından gelir. Silme yok: bölüm `is_active=False`, ilişki `end_date` ile biter.
Toplama ve sayma veritabanında; sayfa başına sabit sayıda sorgu (N+1 yok, docs/08 §3).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.structure import (
    FULL_SHARE,
    PartyRole,
    StructureRuleError,
    accounts_to_open,
    check_owner_share,
    check_party_end,
    reference_base,
    reference_code,
)
from site_yonetim.domain.text import tr_lower
from site_yonetim.models import Block, LedgerAccount, Person, Unit, UnitParty, UnitType


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
    query = select(Person)
    if q:
        query = query.where(Person.search_name.like(_like(tr_lower(" ".join(q.split())))))
    return query.order_by(Person.last_name, Person.first_name, Person.id)


async def require_person(session: AsyncSession, person_id: uuid.UUID) -> Person:
    person = await session.scalar(select(Person).where(Person.id == person_id))
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
