"""Sakinin kendini kaydetmesi ve yönetici onayı — frontend servis isteği 13.

- **Kayıt bağlantısı** site başına bir koddur: tahmin edilemez (`secrets.token_urlsafe`, 11
  karakter); yenilenince eskisi hemen geçersiz; kayda kapatılabilir.
- **Başvuru** herkese açık uçtan gelir (oturum yok); kod çözümlemesi bilinçli "tüm siteler"
  kapsamında, yazım o sitenin kapsamında yapılır. Aynı telefonla bekleyen başvuru 409.
- **Onay** mevcut bölüme kişi ekleme mantığıyla (`structure.add_party`) tek işlemde: telefon ya
  da e-postayla eşleşen kişi varsa yeni kişi açılmaz. E-posta varsa sakin giriş hesabı açılır;
  geçici parola yalnız yanıtta döner — SMS/e-posta gönderimi açık karar K5 (o zamana kadar elden).
- Başvuru verisi kişiseldir; saklama süresi açık karar K8. Kararlar denetim kaydına yalnız
  numara ve sonuçla yazılır (kişisel veri kopyalanmaz).
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import all_sites_scope, current_scope
from site_yonetim.domain.access import SiteRole, UserKind
from site_yonetim.domain.members import Applicant, Relation
from site_yonetim.domain.structure import PartyRole
from site_yonetim.models import (
    Block,
    Person,
    Registration,
    RegistrationLink,
    Site,
    SiteMembership,
    Unit,
    User,
)
from site_yonetim.services import structure

CODE_BYTES = 8
TEMPORARY_PASSWORD_BYTES = 12
_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
RELATION_LABELS = {Relation.OWNER: "malik", Relation.TENANT: "kiracı"}


class RegistrationRuleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def reference(number: int) -> str:
    """reference(1) → "KB-0001" """
    return f"KB-{number:04d}"


# --- Bağlantı -----------------------------------------------------------------------


def _new_code() -> str:
    return secrets.token_urlsafe(CODE_BYTES)


async def link(session: AsyncSession) -> RegistrationLink:
    """Sitenin kayıt bağlantısı; yoksa açılır."""
    found: RegistrationLink | None = await session.scalar(select(RegistrationLink))
    if found is None:
        found = RegistrationLink(code=_new_code(), is_enabled=True)
        session.add(found)
        await session.flush()
    return found


async def rotate(session: AsyncSession) -> RegistrationLink:
    current = await link(session)
    current.code = _new_code()
    await session.flush()
    return current


@dataclass(frozen=True, slots=True)
class PublicSite:
    site_id: uuid.UUID
    name: str
    slug: str


async def site_by_code(factory: async_sessionmaker[AsyncSession], code: str) -> PublicSite | None:
    """Herkese açık uç: kod açık bir bağlantıya aitse site; değilse `None` (404)."""
    if not 8 <= len(code) <= 64:
        return None
    with all_sites_scope():
        async with factory() as session:
            row = (
                await session.execute(
                    select(Site.id, Site.name, Site.slug)
                    .join(RegistrationLink, RegistrationLink.site_id == Site.id)
                    .where(RegistrationLink.code == code, RegistrationLink.is_enabled)
                )
            ).first()
    return PublicSite(*row) if row else None


# --- Başvuru ------------------------------------------------------------------------


async def submit(
    session: AsyncSession,
    applicant: Applicant,
    *,
    explicit_consent: bool,
    now: datetime,
    ip: str | None,
) -> Registration:
    pending = await session.scalar(
        select(Registration.id).where(
            Registration.phone == applicant.phone, Registration.status == "pending"
        )
    )
    if pending is not None:
        raise _already_pending()
    scope = current_scope()
    await session.execute(_NUMBER_LOCK, {"key": f"registrations:{scope.site_id if scope else ''}"})
    number = (await session.scalar(select(func.max(Registration.number)))) or 0
    registration = Registration(
        id=uuid.uuid7(),
        number=number + 1,
        first_name=applicant.first_name,
        last_name=applicant.last_name,
        phone=applicant.phone,
        email=applicant.email,
        unit_text=applicant.unit_text,
        relation=applicant.relation.value,
        kvkk_ack_at=now,
        explicit_consent_at=now if explicit_consent else None,
        ip=ip,
        status="pending",
    )
    session.add(registration)
    try:
        await session.flush()
    except IntegrityError as exc:  # eşzamanlı iki başvuru aynı telefonla
        raise _already_pending() from exc
    return registration


def _already_pending() -> RegistrationRuleError:
    return RegistrationRuleError(
        "already_pending",
        "Bu telefonla bekleyen bir başvuru var; yönetim inceleyince size dönülecek.",
    )


def list_query(status: str | None) -> Select[Registration]:
    query = select(Registration)
    if status:
        query = query.where(Registration.status == status)
    return query.order_by(Registration.created_at.desc(), Registration.id.desc())


async def get(
    session: AsyncSession, registration_id: uuid.UUID, *, lock: bool = False
) -> Registration | None:
    query = select(Registration).where(Registration.id == registration_id)
    found: Registration | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def unit_names(session: AsyncSession, unit_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not unit_ids:
        return {}
    rows = await session.execute(
        select(Unit.id, Unit.number, Block.name)
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(Unit.id.in_(list(unit_ids)))
    )
    return {i: f"{block}-{number}" for i, number, block in rows}


# --- Karar --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approved:
    registration: Registration
    unit_name: str
    login: str  # created · linked · none
    temporary_password: str | None  # yalnız yeni hesapta; saklanmaz


def _undecided(registration: Registration) -> None:
    if registration.status != "pending":
        raise RegistrationRuleError("already_decided", "Bu başvuru zaten sonuçlandı.")


async def _person(session: AsyncSession, r: Registration) -> Person:
    """Telefon ya da e-postayla eşleşen kişi varsa o; yoksa yeni kişi (servis isteği 13)."""
    matches = [Person.phone == r.phone]
    if r.email:
        matches.append(Person.email == r.email)
    found: Person | None = await session.scalar(
        select(Person).where(or_(*matches)).order_by(Person.created_at).limit(1)
    )
    if found is not None:
        return found
    person = Person(first_name=r.first_name, last_name=r.last_name, phone=r.phone, email=r.email)
    session.add(person)
    await session.flush()
    return person


async def _login(session: AsyncSession, r: Registration, person: Person) -> tuple[str, str | None]:
    if not r.email:
        return "none", None
    user: User | None = await session.scalar(select(User).where(User.email == r.email))
    password = None
    login = "linked"
    if user is None:
        password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
        user = User(
            email=r.email,
            full_name=f"{r.first_name} {r.last_name}",
            password_hash=hash_password(password),
            kind=UserKind.RESIDENT.value,
            must_change_password=True,
        )
        session.add(user)
        await session.flush()
        login = "created"
    member = await session.scalar(
        select(SiteMembership.id).where(SiteMembership.user_id == user.id)
    )
    if member is None:
        session.add(
            SiteMembership(
                user_id=user.id, role=SiteRole.RESIDENT.value, person_id=person.id, is_active=True
            )
        )
    if person.user_id is None:
        person.user_id = user.id
    await session.flush()
    return login, password


async def approve(
    session: AsyncSession,
    registration: Registration,
    *,
    unit_id: uuid.UUID,
    start_date: date,
    decided_by: str,
    now: datetime,
) -> Approved:
    _undecided(registration)
    row = (
        await session.execute(
            select(Unit, Block)
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(Unit.id == unit_id)
        )
    ).first()
    if row is None:
        raise RegistrationRuleError("unit_not_found", "Bölüm seçin.")
    unit, block = row
    person = await _person(session, registration)
    relation = Relation(registration.relation)
    role = PartyRole.OWNER if relation is Relation.OWNER else PartyRole.TENANT
    await structure.add_party(
        session, unit=unit, block=block, person=person, role=role, start_date=start_date
    )
    login, password = await _login(session, registration, person)
    registration.status = "approved"
    registration.decided_at = now
    registration.decided_by_name = decided_by
    registration.unit_id = unit.id
    registration.person_id = person.id
    await session.flush()
    return Approved(registration, f"{block.name}-{unit.number}", login, password)


async def reject(
    session: AsyncSession,
    registration: Registration,
    *,
    reason: str,
    decided_by: str,
    now: datetime,
) -> Registration:
    _undecided(registration)
    text_ = " ".join(reason.split())
    if not 3 <= len(text_) <= 300:
        raise RegistrationRuleError("reason_required", "Gerekçe yazın (3–300 karakter).")
    registration.status = "rejected"
    registration.reject_reason = text_
    registration.decided_at = now
    registration.decided_by_name = decided_by
    await session.flush()
    return registration
