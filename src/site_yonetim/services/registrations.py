"""Sakinin kendini kaydetmesi ve yönetici onayı — frontend servis isteği 13.

- **Kayıt bağlantısı** site başına bir koddur: tahmin edilemez (`secrets.token_urlsafe`, 11
  karakter); yenilenince eskisi hemen geçersiz; kayda kapatılabilir.
- **Başvuru** herkese açık uçtan gelir (oturum yok); kod çözümlemesi bilinçli "tüm siteler"
  kapsamında, yazım o sitenin kapsamında yapılır. Aynı telefonla bekleyen başvuru 409.
- **Onay** mevcut bölüme kişi ekleme mantığıyla (`structure.add_party`) tek işlemde: telefon ya
  da e-postayla eşleşen kişi varsa yeni kişi açılmaz. Sakin giriş hesabı **telefonla** açılır
  (P5, PLT-01: sakin telefon + SMS koduyla girer; parola yok, geçici parola yok); telefonu ya da
  e-postası eşleşen hesap varsa ona bağlanır. Kişiye onay SMS'i gider (gönderim katmanı).
- Başvuru verisi kişiseldir; saklama süresi açık karar K8. Kararlar denetim kaydına yalnız
  numara ve sonuçla yazılır (kişisel veri kopyalanmaz).
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import all_sites_scope, current_scope
from site_yonetim.domain.access import UserKind
from site_yonetim.domain.approvals import OPEN_STATUSES, ActionType
from site_yonetim.domain.members import Applicant, Relation
from site_yonetim.domain.notifications import NotificationCategory, NotificationChannel
from site_yonetim.domain.roles import RESIDENT_KEY
from site_yonetim.domain.structure import FULL_SHARE, PartyRole, StructureRuleError
from site_yonetim.models import (
    ApprovalRequest,
    Block,
    Person,
    Registration,
    RegistrationLink,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    Unit,
    User,
)
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import delivery, structure
from site_yonetim.services import privacy as privacy_svc
from site_yonetim.services.approvals import ExecutionError
from site_yonetim.services.approvals import Person as ApprovalPerson

CODE_BYTES = 8
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
    lifecycle_status: str = "live"


async def site_by_code(factory: async_sessionmaker[AsyncSession], code: str) -> PublicSite | None:
    """Herkese açık uç: kod açık bir bağlantıya aitse site; değilse `None` (404)."""
    if not 8 <= len(code) <= 64:
        return None
    with all_sites_scope():
        async with factory() as session:
            row = (
                await session.execute(
                    select(Site.id, Site.name, Site.slug, Site.lifecycle_status)
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
    unit_id: uuid.UUID | None = None,
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
        requested_unit_id=unit_id,
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


@dataclass(frozen=True, slots=True)
class PublicBlock:
    id: uuid.UUID
    name: str
    units: list[tuple[uuid.UUID, str]]


async def public_blocks(session: AsyncSession) -> list[PublicBlock]:
    """Herkese açık form için yalnız blok ve etkin bölüm adları — kişi bilgisi yok (KVKK)."""
    rows = await session.execute(
        select(Block.id, Block.name, Unit.id, Unit.number)
        .join(Unit, and_(Unit.block_id == Block.id, Unit.site_id == Block.site_id))
        .where(Unit.is_active)
        .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
    )
    blocks: dict[uuid.UUID, PublicBlock] = {}
    for block_id, block_name, unit_id, number in rows:
        block = blocks.setdefault(block_id, PublicBlock(block_id, block_name, []))
        block.units.append((unit_id, f"{block_name}-{number}" if block_name else number))
    return list(blocks.values())


# --- Karar --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Approved:
    registration: Registration
    unit_name: str
    login: str  # created · linked
    phone: str  # giriş telefonu (E.164)


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


async def _account(session: AsyncSession, r: Registration) -> tuple[User, str]:
    """Telefonla eşleşen hesap; yoksa e-postayla eşleşen (telefonu boşsa eklenir); yoksa yeni
    telefon hesabı (parolasız — sakin SMS koduyla girer)."""
    user: User | None = await session.scalar(select(User).where(User.phone == r.phone))
    if user is not None:
        return user, "linked"
    if r.email:
        user = await session.scalar(select(User).where(User.email == r.email))
        if user is not None:
            if user.phone is None:
                user.phone = r.phone
            return user, "linked"
    taken_email = (
        r.email is not None
        and await session.scalar(select(User.id).where(User.email == r.email)) is not None
    )
    user = User(
        phone=r.phone,
        email=None if taken_email else r.email,
        full_name=f"{r.first_name} {r.last_name}",
        password_hash=None,
        kind=UserKind.RESIDENT.value,
    )
    session.add(user)
    await session.flush()
    return user, "created"


async def _login(session: AsyncSession, r: Registration, person: Person) -> str:
    user, login = await _account(session, r)
    member = await session.scalar(
        select(SiteMembership.id).where(SiteMembership.user_id == user.id)
    )
    if member is None:
        session.add(SiteMembership(user_id=user.id, person_id=person.id, is_active=True))
        session.add(
            SiteRoleAssignment(
                user_id=user.id, role_key=RESIDENT_KEY, assigned_by_name="Kayıt başvurusu"
            )
        )
    if person.user_id is None:
        person.user_id = user.id
    await session.flush()
    # Başvurudaki bilgilendirme onayı ve açık rıza hesaba bağlanır (P6).
    await privacy_svc.link_registration(session, user.id, r)
    return login


async def approve(
    session: AsyncSession,
    registration: Registration,
    *,
    unit_id: uuid.UUID,
    start_date: date,
    decided_by: str,
    now: datetime,
    share_percent: Decimal | None = None,
) -> Approved:
    """`share_percent` yalnız malik başvurusunda: hisseli mülkiyette yeni malikin payı
    (verilmezse %100; mevcut maliklerle toplam %100'ü aşarsa owner_shares_exceed)."""
    _undecided(registration)
    relation = Relation(registration.relation)
    if share_percent is not None and relation is not Relation.OWNER:
        raise RegistrationRuleError(
            "share_only_for_owner", "Hisse oranı yalnız malik başvurusunda girilir."
        )
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
    role = PartyRole.OWNER if relation is Relation.OWNER else PartyRole.TENANT
    await structure.add_party(
        session,
        unit=unit,
        block=block,
        person=person,
        role=role,
        start_date=start_date,
        share_percent=share_percent if share_percent is not None else FULL_SHARE,
    )
    login = await _login(session, registration, person)
    registration.status = "approved"
    registration.decided_at = now
    registration.decided_by_name = decided_by
    registration.unit_id = unit.id
    registration.person_id = person.id
    await session.flush()
    return Approved(registration, f"{block.name}-{unit.number}", login, registration.phone)


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


# --- Onay kuralı (K21) --------------------------------------------------------------------


async def open_requests(
    session: AsyncSession, registration_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """Başvuru → açık onay talebi (varsa)."""
    if not registration_ids:
        return {}
    rows = await session.execute(
        select(ApprovalRequest.subject_id, ApprovalRequest.id).where(
            ApprovalRequest.action_type == ActionType.REGISTRATION.value,
            ApprovalRequest.subject_id.in_(registration_ids),
            ApprovalRequest.status.in_([s.value for s in OPEN_STATUSES]),
        )
    )
    return {subject: request for subject, request in rows if subject is not None}


async def ensure_not_in_approval(session: AsyncSession, registration: Registration) -> None:
    if registration.id in await open_requests(session, [registration.id]):
        raise RegistrationRuleError(
            "approval_pending",
            "Bu başvuru için onay talebi açık; talep sonuçlanana kadar işlem yapılamaz.",
        )


async def request_approval(
    session: AsyncSession,
    site: Site,
    registration: Registration,
    *,
    unit_id: uuid.UUID,
    start_date: date,
    share_percent: Decimal | None,
    requester: ApprovalPerson,
    today: date,
    now: datetime,
) -> ApprovalRequest | None:
    """Kayıt onayı kuralı varsa talep açar (kayıt `pending` kalır, işlem yapılmaz); yoksa None.
    Alan denetimi onay öncesi yapılır ki hatalı talep açılmasın."""
    version = await approvals_svc.match_rule(
        session, ActionType.REGISTRATION, amount=None, source=None, today=today
    )
    if version is None:
        return None
    _undecided(registration)
    await ensure_not_in_approval(session, registration)
    if share_percent is not None and Relation(registration.relation) is not Relation.OWNER:
        raise RegistrationRuleError(
            "share_only_for_owner", "Hisse oranı yalnız malik başvurusunda girilir."
        )
    names = await unit_names(session, {unit_id})
    if unit_id not in names:
        raise RegistrationRuleError("unit_not_found", "Bölüm seçin.")
    return await approvals_svc.request_approval(
        session,
        site,
        approvals_svc.NewRequest(
            action_type=ActionType.REGISTRATION,
            title=f"Kayıt başvurusu — {reference(registration.number)}, {names[unit_id]}",
            summary={
                "reference": reference(registration.number),
                "applicant": f"{registration.first_name} {registration.last_name}",
                "relation": registration.relation,
                "unit": names[unit_id],
            },
            payload={
                "registration_id": str(registration.id),
                "unit_id": str(unit_id),
                "start_date": start_date.isoformat(),
                "share_percent": str(share_percent) if share_percent is not None else None,
            },
            subject_type="registrations",
            subject_id=registration.id,
        ),
        requester=requester,
        today=today,
        now=now,
    )


async def queue_approved_sms(
    session: AsyncSession, approved: Approved, *, web_url: str, now: datetime
) -> None:
    await delivery.queue_direct(
        session,
        channel=NotificationChannel.SMS,
        to=approved.phone,
        body=f"Kayıt başvurunuz onaylandı ({approved.unit_name}). Telefon numaranızla, SMS "
        f"koduyla giriş yapabilirsiniz: {web_url}/giris",
        category=NotificationCategory.OPERATIONS,
        kind="registration.approved",
        now=now,
    )


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    payload = request.payload or {}
    registration = await get(session, uuid.UUID(payload["registration_id"]), lock=True)
    if registration is None or registration.status != "pending":
        raise ExecutionError("Başvuru artık onay beklemiyor.")
    now = request.decided_at or datetime.now().astimezone()
    share = Decimal(payload["share_percent"]) if payload.get("share_percent") else None
    try:
        approved = await approve(
            session,
            registration,
            unit_id=uuid.UUID(payload["unit_id"]),
            start_date=date.fromisoformat(payload["start_date"]),
            decided_by=request.requested_by_name,
            now=now,
            share_percent=share,
        )
    except (RegistrationRuleError, StructureRuleError) as exc:
        raise ExecutionError(f"Başvuru artık onaylanamıyor: {exc.message}") from exc
    record(session, AuditAction.UPDATE, "registrations", registration.id,
           {"reference": reference(registration.number), "status": "approved"})  # fmt: skip
    await queue_approved_sms(session, approved, web_url=get_settings().web_url, now=now)


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    """Talep reddedilirse başvuru reddedilir; geri çekilirse eski haline (bekliyor) döner."""
    if request.status != "rejected":
        return
    registration = await get(
        session, uuid.UUID((request.payload or {})["registration_id"]), lock=True
    )
    if registration is not None and registration.status == "pending":
        registration.status = "rejected"
        registration.reject_reason = f"Onay talebi reddedildi (#{request.number})."
        registration.decided_at = request.decided_at
        registration.decided_by_name = request.requested_by_name
        await session.flush()


approvals_svc.register_executor(ActionType.REGISTRATION, _execute)
approvals_svc.register_closer(ActionType.REGISTRATION, _close)
