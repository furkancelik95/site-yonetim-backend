"""Davetler ve hane üyeleri (servis istekleri 20 ve 23; YAP-04 S4, YAP-07).

- **Davet** telefona bağlı, tek kullanımlıktır; belirtecin yalnız özeti saklanır, bağlantı
  oluşturma ve yeniden gönderme yanıtında bir kez görünür. SMS yok (K5): bağlantı gönderilmez,
  yönetim kopyalayıp iletir.
- Kurulumdaki tesiste (YAP-01) ve davet yöntemi kapalıyken davet oluşturulmaz.
- **Kabul**: telefon davettekiyle tam eşleşmeli, bilgilendirme onayı zorunlu. Kişi telefonla
  eşleşirse mevcut kişi kullanılır; bölüme ilişkisi eklenir (kayıt başvurusu onayıyla aynı sonuç),
  sakin giriş hesabı açılır, geçici parola yalnız yanıtta bir kez döner. Giriş e-postayla
  olduğundan davette e-posta yoksa kabulde istenir.
- **Hane üyesi** daveti aynı akıştan geçer (`relation=household`): kabulde kişi bölüme
  "oturan" olarak eklenir — cari hesabı olmaz, borç/ekstre görmez. Bölüm başına en fazla 6
  etkin üye; bitirme silmez.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.security import hash_password
from site_yonetim.db.tenancy import all_sites_scope
from site_yonetim.domain.access import UserKind
from site_yonetim.domain.invitations import (
    HOUSEHOLD_LIMIT,
    OPEN_INVITATION,
    HouseholdStatus,
    InvitationRelation,
    InvitationStatus,
    SiteLifecycle,
    effective_status,
    new_token,
    split_full_name,
    token_hash,
)
from site_yonetim.domain.roles import RESIDENT_KEY
from site_yonetim.domain.structure import PartyRole, StructureRuleError
from site_yonetim.domain.text import normalize_person_name
from site_yonetim.models import (
    Block,
    HouseholdMember,
    Invitation,
    Person,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    Unit,
    UnitParty,
    User,
)
from site_yonetim.services import structure
from site_yonetim.services.vehicles import read_settings

TEMPORARY_PASSWORD_BYTES = 12
INVITE_PATH = "/davet/"


class InvitationRuleError(Exception):
    def __init__(
        self, status: int, code: str, message: str, fields: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields


def link_of(token: str) -> str:
    return f"{INVITE_PATH}{token}"


async def unit_with_block(session: AsyncSession, unit_id: uuid.UUID) -> tuple[Unit, Block] | None:
    row = (
        await session.execute(
            select(Unit, Block)
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(Unit.id == unit_id)
        )
    ).first()
    return (row[0], row[1]) if row else None


def unit_label(unit: Unit, block: Block) -> str:
    return unit.display_name(block.name)


# --- Davet (personel) ---------------------------------------------------------------------


async def _has_open_invitation(session: AsyncSession, phone: str, now: datetime) -> bool:
    rows = await session.execute(
        select(Invitation.status, Invitation.expires_at).where(
            Invitation.phone == phone,
            Invitation.status.in_([s.value for s in OPEN_INVITATION]),
            Invitation.relation != InvitationRelation.HOUSEHOLD.value,
        )
    )
    return any(effective_status(s, e, now) in OPEN_INVITATION for s, e in rows)


async def create_invitation(
    session: AsyncSession,
    site: Site,
    *,
    full_name: str,
    phone: str,
    email: str | None,
    unit_id: uuid.UUID,
    relation: InvitationRelation,
    sent_by: str | None,
    now: datetime,
) -> tuple[Invitation, str, str]:
    """(davet, belirteç, bölüm adı). Belirteç yalnız bu yanıtta döner."""
    config = await read_settings(session)
    if relation is not InvitationRelation.HOUSEHOLD:
        if site.lifecycle_status != SiteLifecycle.LIVE.value:
            raise InvitationRuleError(
                409,
                "site_not_live",
                "Tesis kurulum aşamasında; sakin daveti canlıya alındıktan sonra gönderilir "
                "(Kurulum › Canlıya al).",
            )
        if not config.invite_enabled:
            raise InvitationRuleError(
                409,
                "method_disabled",
                "Davet yöntemi kapalı; Ayarlar › Kayıt yöntemleri'nden açın.",
            )
    found = await unit_with_block(session, unit_id)
    if found is None:
        raise InvitationRuleError(
            422, "validation", "Bölüm bulunamadı.", {"unit_id": "Bölüm bulunamadı."}
        )
    if relation is not InvitationRelation.HOUSEHOLD and await _has_open_invitation(
        session, phone, now
    ):
        raise InvitationRuleError(
            409,
            "already_invited",
            "Bu telefona gönderilmiş açık bir davet var; yeniden gönderin ya da iptal edin.",
        )
    token = new_token()
    invitation = Invitation(
        id=uuid.uuid7(),
        full_name=full_name,
        phone=phone,
        email=email,
        unit_id=unit_id,
        relation=relation.value,
        status=InvitationStatus.SENT.value,
        token_hash=token_hash(token),
        sent_by_name=sent_by,
        sent_at=now,
        expires_at=now + timedelta(days=config.invite_valid_days),
    )
    session.add(invitation)
    await session.flush()
    return invitation, token, unit_label(*found)


async def resend(
    session: AsyncSession, invitation: Invitation, *, sent_by: str | None, now: datetime
) -> str:
    """Yeni belirteç üretir; eskisi hemen geçersiz. Tamamlanmış davet yenilenemez."""
    if invitation.status == InvitationStatus.COMPLETED.value:
        raise InvitationRuleError(409, "already_completed", "Bu davet tamamlanmış.")
    config = await read_settings(session)
    token = new_token()
    invitation.token_hash = token_hash(token)
    invitation.status = InvitationStatus.SENT.value
    invitation.sent_at = now
    invitation.sent_by_name = sent_by
    invitation.opened_at = None
    invitation.expires_at = now + timedelta(days=config.invite_valid_days)
    await session.flush()
    return token


async def cancel(session: AsyncSession, invitation: Invitation) -> None:
    if invitation.status == InvitationStatus.COMPLETED.value:
        raise InvitationRuleError(409, "already_completed", "Tamamlanmış davet iptal edilemez.")
    invitation.status = InvitationStatus.CANCELLED.value
    await session.flush()


async def expire_due(session: AsyncSession, now: datetime) -> int:
    """Gece işi: süresi dolan açık davetleri `expired` yazar (okumada zaten öyle görünür)."""
    rows = list(
        await session.scalars(
            select(Invitation).where(
                Invitation.status.in_([s.value for s in OPEN_INVITATION]),
                Invitation.expires_at <= now,
            )
        )
    )
    for invitation in rows:
        invitation.status = InvitationStatus.EXPIRED.value
    await session.flush()
    return len(rows)


# --- Herkese açık: davet bağlantısı -------------------------------------------------------


async def site_of_token(factory: async_sessionmaker[AsyncSession], token: str) -> Site | None:
    """Belirteç özetinden siteyi bulur (bilinçli "tüm siteler" kapsamı, yalnız kimlik)."""
    if not 16 <= len(token) <= 64:
        return None
    with all_sites_scope():
        async with factory() as session:
            site: Site | None = await session.scalar(
                select(Site)
                .join(Invitation, Invitation.site_id == Site.id)
                .where(Invitation.token_hash == token_hash(token))
            )
    return site


async def invitation_by_token(
    session: AsyncSession, token: str, *, lock: bool = False
) -> Invitation | None:
    query = select(Invitation).where(Invitation.token_hash == token_hash(token))
    found: Invitation | None = await session.scalar(query.with_for_update() if lock else query)
    return found


def check_usable(invitation: Invitation | None, now: datetime) -> Invitation:
    if invitation is None or effective_status(invitation.status, invitation.expires_at, now) in (
        InvitationStatus.CANCELLED,
        InvitationStatus.EXPIRED,
    ):
        raise InvitationRuleError(
            404,
            "not_found",
            "Davet bağlantısı geçersiz ya da süresi dolmuş. Site yönetiminden yeni davet isteyin.",
        )
    return invitation


@dataclass(frozen=True, slots=True)
class Accepted:
    unit_name: str
    login_email: str
    temporary_password: str | None  # mevcut hesaba bağlandıysa None


async def _person_for(session: AsyncSession, invitation: Invitation) -> Person:
    found: Person | None = await session.scalar(
        select(Person)
        .where(Person.phone == invitation.phone, Person.merged_into_id.is_(None))
        .order_by(Person.created_at)
        .limit(1)
    )
    if found is not None:
        return found
    first, last = split_full_name(invitation.full_name)
    first, last = normalize_person_name(first, last)
    person = Person(first_name=first, last_name=last, phone=invitation.phone)
    session.add(person)
    await session.flush()
    return person


async def _login(
    session: AsyncSession, person: Person, full_name: str, email: str | None
) -> tuple[str, str | None]:
    user: User | None = None
    if person.user_id is not None:
        user = await session.get(User, person.user_id)
    password = None
    if user is None:
        if not email:
            raise InvitationRuleError(
                422,
                "validation",
                "Giriş hesabınız için e-posta adresinizi yazın.",
                {"email": "Giriş için e-posta adresi gerekli."},
            )
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
            user = User(
                email=email,
                full_name=full_name,
                password_hash=hash_password(password),
                kind=UserKind.RESIDENT.value,
                must_change_password=True,
            )
            session.add(user)
            await session.flush()
    member = await session.scalar(select(SiteMembership).where(SiteMembership.user_id == user.id))
    if member is None:
        session.add(SiteMembership(user_id=user.id, person_id=person.id, is_active=True))
        session.add(
            SiteRoleAssignment(user_id=user.id, role_key=RESIDENT_KEY, assigned_by_name="Davet")
        )
    elif not member.is_active and member.person_id == person.id:
        member.is_active = True
    if person.user_id is None:
        person.user_id = user.id
    await session.flush()
    return user.email or user.phone or "", password


async def accept(
    session: AsyncSession,
    invitation: Invitation,
    *,
    phone: str,
    email: str | None,
    today: date,
    now: datetime,
) -> Accepted:
    if invitation.status == InvitationStatus.COMPLETED.value:
        raise InvitationRuleError(409, "already_completed", "Bu davet daha önce kullanılmış.")
    if phone != invitation.phone:
        message = (
            "Numara davetteki numarayla eşleşmiyor. Telefonunuz değiştiyse site yönetimine "
            "başvurun."
        )
        raise InvitationRuleError(422, "validation", message, {"phone": message})
    found = await unit_with_block(session, invitation.unit_id)
    if found is None:  # pragma: no cover — bileşik FK bölümün sitede olmasını garanti eder
        raise InvitationRuleError(404, "not_found", "Bölüm bulunamadı.")
    unit, block = found
    person = await _person_for(session, invitation)
    relation = InvitationRelation(invitation.relation)
    role = {
        InvitationRelation.OWNER: PartyRole.OWNER,
        InvitationRelation.TENANT: PartyRole.TENANT,
        InvitationRelation.HOUSEHOLD: PartyRole.RESIDENT,
    }[relation]
    active = await session.scalar(
        select(UnitParty.id).where(
            UnitParty.unit_id == unit.id,
            UnitParty.person_id == person.id,
            UnitParty.role == role.value,
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    if active is None:
        try:
            await structure.add_party(
                session, unit=unit, block=block, person=person, role=role, start_date=today
            )
        except StructureRuleError as exc:
            raise InvitationRuleError(
                409,
                exc.code,
                "Bölüm kaydınız tamamlanamadı (malik hisseleri dolu). Site yönetimine başvurun.",
            ) from exc
    login_email, password = await _login(
        session, person, invitation.full_name, email or invitation.email
    )
    if relation is InvitationRelation.HOUSEHOLD:
        member = await session.scalar(
            select(HouseholdMember).where(HouseholdMember.invitation_id == invitation.id)
        )
        if member is not None and member.status == HouseholdStatus.INVITED.value:
            member.status = HouseholdStatus.ACTIVE.value
            member.person_id = person.id
    invitation.status = InvitationStatus.COMPLETED.value
    invitation.completed_at = now
    invitation.person_id = person.id
    await session.flush()
    return Accepted(unit_label(unit, block), login_email, password)


# --- Hane üyeleri -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Permissions:
    visitors: bool = True
    packages: bool = True
    reservations: bool = False
    requests: bool = False


def household_query() -> object:
    return (
        select(HouseholdMember, Unit.number, Block.name)
        .join(
            Unit, and_(Unit.id == HouseholdMember.unit_id, Unit.site_id == HouseholdMember.site_id)
        )
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .order_by(HouseholdMember.created_at.desc())
    )


async def add_household(
    session: AsyncSession,
    site: Site,
    unit_id: uuid.UUID,
    *,
    full_name: str,
    phone: str,
    permissions: Permissions,
    invited_by: str | None,
    invited_by_user_id: uuid.UUID | None,
    now: datetime,
) -> tuple[HouseholdMember, str, str]:
    """(üye, belirteç, bölüm adı)."""
    open_members = list(
        await session.scalars(
            select(HouseholdMember)
            .where(
                HouseholdMember.unit_id == unit_id,
                HouseholdMember.status != HouseholdStatus.ENDED.value,
            )
            .with_for_update()
        )
    )
    if len(open_members) >= HOUSEHOLD_LIMIT:
        raise InvitationRuleError(
            409, "household_limit", f"Bu bölümde en fazla {HOUSEHOLD_LIMIT} hane üyesi olabilir."
        )
    if any(m.phone == phone for m in open_members):
        raise InvitationRuleError(409, "already_member", "Bu telefonla hane üyesi zaten var.")
    invitation, token, unit_name = await create_invitation(
        session,
        site,
        full_name=full_name,
        phone=phone,
        email=None,
        unit_id=unit_id,
        relation=InvitationRelation.HOUSEHOLD,
        sent_by=invited_by,
        now=now,
    )
    member = HouseholdMember(
        id=uuid.uuid7(),
        unit_id=unit_id,
        invitation_id=invitation.id,
        full_name=full_name,
        phone=phone,
        can_visitors=permissions.visitors,
        can_packages=permissions.packages,
        can_reservations=permissions.reservations,
        can_requests=permissions.requests,
        status=HouseholdStatus.INVITED.value,
        invited_by_name=invited_by,
        invited_by_user_id=invited_by_user_id,
    )
    session.add(member)
    await session.flush()
    return member, token, unit_name


async def end_household(
    session: AsyncSession, member: HouseholdMember, *, today: date, now: datetime
) -> None:
    """Üyelik biter (silinmez): açık davet iptal, oturan ilişkisi bugün kapanır; kişinin
    sitede başka etkin ilişkisi kalmadıysa giriş erişimi kapanır."""
    if member.status == HouseholdStatus.ENDED.value:
        raise InvitationRuleError(409, "already_ended", "Hane üyeliği zaten bitmiş.")
    member.status = HouseholdStatus.ENDED.value
    member.ended_at = now
    if member.invitation_id is not None:
        invitation = await session.get(Invitation, member.invitation_id)
        if invitation is not None and invitation.status in {s.value for s in OPEN_INVITATION}:
            invitation.status = InvitationStatus.CANCELLED.value
    if member.person_id is not None:
        parties = await session.scalars(
            select(UnitParty).where(
                UnitParty.unit_id == member.unit_id,
                UnitParty.person_id == member.person_id,
                UnitParty.role == PartyRole.RESIDENT.value,
                UnitParty.end_date.is_(None),
            )
        )
        for party in parties:
            party.end_date = max(today, party.start_date)
        await session.flush()
        still = await session.scalar(
            select(UnitParty.id).where(
                UnitParty.person_id == member.person_id,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date > today),
            )
        )
        if still is None:
            memberships = await session.scalars(
                select(SiteMembership).where(SiteMembership.person_id == member.person_id)
            )
            for membership in memberships:
                membership.is_active = False
    await session.flush()
