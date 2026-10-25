"""Kişiler: mükerrer önerisi, birleştirme, malik değişimi (servis isteği 21, YAP-04).

- **Mükerrer önerisi** aynı telefon (E.164) ya da aynı e-postayla; otomatik birleştirme yok.
- **Birleştirme** geri alınamaz: birleşen kişiye bağlı her kayıt (ilişkiler, cari hesaplar,
  başvurular, talepler, kargolar, araçlar, hane üyelikleri, giriş hesabı…) kalan kişiye taşınır;
  birleşen kişi `merged_into_id` ile işaretlenir, listelerde görünmez, satırı silinmez.
  Kişiye bağlanan tablolar şema üzerinden bulunur — yeni modülün tablosu da kendiliğinden taşınır.
- **Malik değişimi** (satış / veraset) tek işlemde: eski malikler devirden bir gün önce kapanır
  (bitiş günü dahil sayılır), yeni malikler devir günü başlar; hisse toplamı tam %100.
  Devir öncesi borç eski malikte kalır (K10); yeni malike yeni cari açılır.
  Onay kuralı tanımlıysa işlem onaya gider (`ownership_transfer`, 202).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from site_yonetim.db.base import Base
from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.approvals import ActionType
from site_yonetim.domain.structure import (
    PartyRole,
    StructureRuleError,
    check_effective_date,
    check_full_owner_shares,
)
from site_yonetim.models import (
    ApprovalRequest,
    Block,
    CustomFieldValue,
    LedgerAccount,
    Person,
    Unit,
    UnitParty,
)
from site_yonetim.services import structure
from site_yonetim.services.approvals import ExecutionError, register_executor
from site_yonetim.services.charging import BUSINESS_TZ

DUPLICATE_GROUP_MAX = 200


class PeopleRuleError(Exception):
    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


def active_people() -> Select[Person]:
    return select(Person).where(Person.merged_into_id.is_(None))


# --- Mükerrer önerisi ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    key: str
    kind: str  # phone · email
    people: list[Person]


async def duplicates(session: AsyncSession) -> list[DuplicateGroup]:
    """Aynı telefon ya da e-postayı paylaşan etkin kişiler (en fazla 200 grup)."""
    groups: list[DuplicateGroup] = []
    for kind, column in (("phone", Person.phone), ("email", Person.email)):
        keys = list(
            await session.scalars(
                select(column)
                .where(column.is_not(None), Person.merged_into_id.is_(None))
                .group_by(column)
                .having(func.count() > 1)
                .order_by(column)
                .limit(DUPLICATE_GROUP_MAX)
            )
        )
        if not keys:
            continue
        people: dict[str, list[Person]] = defaultdict(list)
        for person in await session.scalars(
            active_people().where(column.in_(keys)).order_by(Person.created_at)
        ):
            value = getattr(person, kind)
            if value is not None:
                people[value].append(person)
        groups.extend(DuplicateGroup(k, kind, people[k]) for k in keys if k and len(people[k]) > 1)
    return groups[:DUPLICATE_GROUP_MAX]


# --- Birleştirme -------------------------------------------------------------------------


def _person_references() -> list[tuple[type[Any], Any]]:
    """Kişiye bağlanan (model, sütun) çiftleri — bileşik `(site_id, x) → persons` anahtarları."""
    found: list[tuple[type[Any], Any]] = []
    for mapper in Base.registry.mappers:
        model = mapper.class_
        table = mapper.local_table
        for fk in getattr(table, "foreign_key_constraints", ()):
            if fk.referred_table.name != "persons":
                continue
            for column in fk.columns:
                if column.name != "site_id":
                    found.append((model, getattr(model, column.key)))
    return found


@dataclass(frozen=True, slots=True)
class Merged:
    kept: Person
    merged: Person
    moved: dict[str, int]


async def merge(
    session: AsyncSession, *, keep_id: uuid.UUID, merge_id: uuid.UUID, now: datetime
) -> Merged:
    if keep_id == merge_id:
        raise PeopleRuleError(422, "validation", "Birleştirilecek iki ayrı kişi seçin.")
    rows = {
        p.id: p
        for p in await session.scalars(
            active_people().where(Person.id.in_([keep_id, merge_id])).with_for_update()
        )
    }
    keep, drop = rows.get(keep_id), rows.get(merge_id)
    if keep is None or drop is None:
        raise PeopleRuleError(404, "not_found", "Kişi bulunamadı.")
    if keep.user_id and drop.user_id and keep.user_id != drop.user_id:
        raise PeopleRuleError(
            409,
            "two_logins",
            "İki kişinin de ayrı giriş hesabı var; birleştirmeden önce birinin sitedeki "
            "erişimini kapatın (Kullanıcılar).",
        )
    owned = aliased(LedgerAccount)
    clash = await session.scalar(
        select(func.count())
        .select_from(LedgerAccount)
        .where(
            LedgerAccount.person_id == drop.id,
            select(owned.id)
            .where(
                owned.person_id == keep.id,
                owned.unit_id == LedgerAccount.unit_id,
                owned.kind == LedgerAccount.kind,
            )
            .correlate(LedgerAccount)
            .exists(),
        )
    )
    if clash:
        raise PeopleRuleError(
            409,
            "accounts_conflict",
            "İki kişinin aynı bölümde aynı türde cari hesabı var; birleştirme hesapları "
            "karıştırır. Önce birinin bölümle ilişkisini sona erdirip hesabını kapatın.",
        )

    moved: dict[str, int] = {}
    for model, column in _person_references():
        if model is Person:
            stmt = update(Person).where(Person.merged_into_id == drop.id)
            result = await session.execute(
                stmt.values(merged_into_id=keep.id).execution_options(synchronize_session=False)
            )
        else:
            stmt = update(model).where(column == drop.id)
            for clause in _unique_guards(model, column, keep.id):
                stmt = stmt.where(clause)
            result = await session.execute(
                stmt.values({column.key: keep.id}).execution_options(synchronize_session=False)
            )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            moved[model.__tablename__] = count
    # Özel alan değerleri: kalan kişide aynı alan doluysa onunki geçerli.
    kept_fields = select(CustomFieldValue.field_id).where(CustomFieldValue.entity_id == keep.id)
    result = await session.execute(
        update(CustomFieldValue)
        .where(CustomFieldValue.entity_id == drop.id, CustomFieldValue.field_id.not_in(kept_fields))
        .values(entity_id=keep.id)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0):
        moved["custom_field_values"] = int(getattr(result, "rowcount", 0))

    if drop.user_id and not keep.user_id:
        keep.user_id, drop.user_id = drop.user_id, None
    keep.phone = keep.phone or drop.phone
    keep.email = keep.email or drop.email
    drop.merged_into_id = keep.id
    drop.merged_at = now
    await session.flush()
    return Merged(keep, drop, moved)


def _unique_guards(model: type[Any], column: Any, keep_id: uuid.UUID) -> list[Any]:
    """Kişi sütununu içeren benzersiz kısıt varsa (ör. duyuru teslimi: duyuru + kişi + kanal),
    kalan kişide aynı satır zaten olanlar taşınmaz (birleşen kişide geçmiş olarak kalır)."""
    from sqlalchemy import UniqueConstraint

    guards: list[Any] = []
    table = model.__table__
    for constraint in table.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        names = {c.name for c in constraint.columns}
        if column.name not in names or names <= {"site_id", "id"}:
            continue
        other = aliased(model)
        conditions = [getattr(other, column.key) == keep_id]
        for name in names - {column.name}:
            conditions.append(getattr(other, name) == getattr(model, name))
        guards.append(~exists().where(and_(*conditions)).correlate(model))
    return guards


# --- Malik değişimi (satış / veraset) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewOwner:
    share_percent: Decimal
    person_id: uuid.UUID | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    email: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "share_percent": f"{self.share_percent:.2f}",
            "person_id": str(self.person_id) if self.person_id else None,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "phone": self.phone,
            "email": self.email,
        }

    @classmethod
    def of(cls, data: dict[str, Any]) -> NewOwner:
        return cls(
            share_percent=Decimal(data["share_percent"]),
            person_id=uuid.UUID(data["person_id"]) if data.get("person_id") else None,
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            phone=data.get("phone"),
            email=data.get("email"),
        )


REASON_LABELS = {"sale": "satış", "inheritance": "veraset"}


async def check_transfer(
    session: AsyncSession, owners: list[NewOwner], transfer_date: date, today: date
) -> None:
    """Gövde kuralları — onaya göndermeden önce de çalışır."""
    if not owners:
        raise PeopleRuleError(
            422, "validation", "En az bir yeni malik girin.", {"new_owners": "En az bir malik."}
        )
    try:
        check_effective_date(
            transfer_date, await structure.earliest_effective(session, today), "transfer_date"
        )
        check_full_owner_shares(o.share_percent for o in owners)
    except StructureRuleError as exc:
        raise PeopleRuleError(422, exc.code, exc.message, {exc.field or "": exc.message}) from exc
    seen: set[uuid.UUID] = set()
    for index, owner in enumerate(owners):
        if owner.person_id is not None:
            if owner.person_id in seen:
                raise PeopleRuleError(
                    422, "validation", "Aynı kişi iki kez malik olarak girilmiş.",
                    {"new_owners": "Aynı kişi iki kez girilmiş."},
                )  # fmt: skip
            seen.add(owner.person_id)
            found = await session.scalar(
                active_people().where(Person.id == owner.person_id).with_only_columns(Person.id)
            )
            if found is None:
                raise PeopleRuleError(404, "not_found", "Kişi bulunamadı.")
            continue
        matches = [Person.phone == owner.phone] if owner.phone else []
        if owner.email:
            matches.append(Person.email == owner.email)
        if matches:
            existing = await session.scalar(
                active_people().where(or_(*matches)).order_by(Person.created_at).limit(1)
            )
            if existing is not None:
                message = (
                    f"Bu telefon ya da e-postayla kayıtlı bir kişi var: {existing.full_name}. "
                    "Yeni kişi yerine kayıtlı kişiyi seçin."
                )
                raise PeopleRuleError(
                    409,
                    "person_exists",
                    message,
                    {f"new_owners.{index}": message, "existing_person_id": str(existing.id)},
                )


async def transfer(
    session: AsyncSession,
    unit: Unit,
    block: Block,
    *,
    owners: list[NewOwner],
    transfer_date: date,
    today: date,
) -> int:
    """Atomik devir; çağıran commit eder (hata olursa hiçbir ilişki değişmez). Kapanan eski
    malik ilişkisi sayısını döner."""
    await check_transfer(session, owners, transfer_date, today)
    previous_day = transfer_date - timedelta(days=1)
    current = list(
        await session.scalars(
            select(UnitParty)
            .where(
                UnitParty.unit_id == unit.id,
                UnitParty.role == PartyRole.OWNER.value,
                UnitParty.start_date <= transfer_date,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date >= transfer_date),
            )
            .with_for_update()
        )
    )
    for party in current:
        if party.start_date > previous_day:
            raise PeopleRuleError(
                409,
                "owner_starts_on_transfer",
                "Mevcut malik ilişkisi devir tarihinde başlıyor; devir tarihini ya da kaydı "
                "kontrol edin.",
            )
        party.end_date = previous_day
    await session.flush()
    for owner in owners:
        if owner.person_id is not None:
            person = await structure.require_person(session, owner.person_id)
        else:
            person = Person(
                first_name=owner.first_name or "",
                last_name=owner.last_name or "",
                phone=owner.phone,
                email=owner.email,
            )
            session.add(person)
            await session.flush()
        try:
            await structure.add_party(
                session,
                unit=unit,
                block=block,
                person=person,
                role=PartyRole.OWNER,
                start_date=transfer_date,
                share_percent=owner.share_percent,
            )
        except StructureRuleError as exc:
            raise PeopleRuleError(409, exc.code, exc.message) from exc
    return len(current)


async def _execute_transfer(session: AsyncSession, request: ApprovalRequest) -> None:
    """Onaylanan malik değişimini uygular; o arada kural bozulduysa yürütülemez."""
    payload = request.payload or {}
    row = (
        await session.execute(
            select(Unit, Block)
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(Unit.id == uuid.UUID(payload["unit_id"]))
        )
    ).first()
    if row is None or current_scope() is None:
        raise ExecutionError("Bölüm bulunamadı.")
    # Dönem kuralı talep günündeki duruma göre: onay ay dönümünden sonra gelse de devir
    # tarihi o gün geçerliyse uygulanır (kapanmış dönem yine engeller).
    requested = request.created_at.astimezone(BUSINESS_TZ).date()
    try:
        await transfer(
            session,
            row[0],
            row[1],
            owners=[NewOwner.of(o) for o in payload.get("new_owners", [])],
            transfer_date=date.fromisoformat(payload["transfer_date"]),
            today=requested,
        )
    except PeopleRuleError as exc:
        raise ExecutionError(f"Malik değişimi artık uygulanamıyor: {exc.message}") from exc


register_executor(ActionType.OWNERSHIP_TRANSFER, _execute_transfer)
