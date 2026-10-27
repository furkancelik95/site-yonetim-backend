"""Site personeli — frontend servis istekleri 18 ve 35 (Yazılım Dokümanı IK-01). Açık site
kapsamında; transaction'ı çağıran yönetir.

- **KVKK veri minimizasyonu:** yalnız iş için gerekenler; T.C. kimlik no, maaş, adres, sağlık
  bilgisi tutulmaz. Bordro bordroyu yapan sistemde; burada yalnız **bordro sicil no** (harf,
  rakam, tire; ≤ 20; site içinde tekil — IK-01, §9.2).
- Silinmez; ayrılış tarihi girilir. Ayrılanın verisinin saklama süresi açık karar K8.
- **K29** (PM, 25.10.2026): kart, sitedeki bir giriş hesabına bağlanabilir. Ayrılış tarihi
  geçince o hesabın site üyeliği pasifleşir (gece işi `run-staff-jobs`); erişim çözümlemesi de
  ayrılanı bir sonraki istekte düşürür (`services/access.py`). Kartı yeniden açmak erişimi geri
  açmaz — Kullanıcılar ekranından açılır. Personel kaydı hesap açmaz.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.management import Employer
from site_yonetim.domain.members import FormFieldError, check_full_name
from site_yonetim.domain.roles import RESIDENT_KEY
from site_yonetim.domain.staffing import LeaveStatus, check_payroll_no
from site_yonetim.domain.validation import normalize_tr_mobile
from site_yonetim.models import (
    LeaveRequest,
    ShiftAssignment,
    SiteMembership,
    SiteRoleAssignment,
    StaffMember,
    User,
)
from site_yonetim.models.audit import Actor, ActorType, set_actor
from site_yonetim.services import roles as roles_svc

NAME_MAX = 60
JOB_ACTOR = "Zamanlanmış iş (personel ayrılışı)"
# Bu sitede personel (sakin olmayan) rolü olmuş kullanıcılar (P18: roller atamadadır)
_STAFF_USERS = select(SiteRoleAssignment.user_id).where(SiteRoleAssignment.role_key != RESIDENT_KEY)


def _clean(value: str | None, limit: int = 120) -> str:
    return " ".join((value or "").split())[:limit]


@dataclass(frozen=True, slots=True)
class StaffData:
    full_name: str | None
    position: str | None
    employer: str | None
    contractor_name: str | None
    phone: str | None
    start_date: date | None
    end_date: date | None
    shift: str | None
    payroll_no: str | None = None
    user_id: uuid.UUID | None = None


class StaffRuleError(Exception):
    """409: sicil no ya da giriş hesabı başka kartta."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check(data: StaffData) -> StaffData:
    errors: dict[str, str] = {}
    name = ""
    try:
        name = check_full_name(data.full_name)
    except ValueError as exc:
        errors["full_name"] = str(exc)
    if len(name) > NAME_MAX:
        errors["full_name"] = f"Ad soyad en çok {NAME_MAX} karakter olmalı."
    position = _clean(data.position)
    if len(position) < 2:
        errors["position"] = "Görevini yazın."
    if data.employer not in {e.value for e in Employer}:
        errors["employer"] = "Kadroyu seçin."
    contractor = _clean(data.contractor_name, 200) or None
    if data.employer == Employer.CONTRACTOR.value and contractor is None:
        errors["contractor_name"] = "Taşeron firmanın adını yazın."
    if data.employer == Employer.SITE.value:
        contractor = None
    phone = None
    if (data.phone or "").strip():
        try:
            phone = normalize_tr_mobile(data.phone or "")
        except ValueError as exc:
            errors["phone"] = str(exc)
    if data.start_date is None:
        errors["start_date"] = "İşe başlama tarihini seçin."
    elif data.end_date is not None and data.end_date < data.start_date:
        errors["end_date"] = "Ayrılış tarihi başlangıçtan önce olamaz."
    payroll = None
    try:
        payroll = check_payroll_no(data.payroll_no)
    except ValueError as exc:
        errors["payroll_no"] = str(exc)
    if errors:
        raise FormFieldError(errors)
    return StaffData(
        full_name=name,
        position=position,
        employer=data.employer,
        contractor_name=contractor,
        phone=phone,
        start_date=data.start_date,
        end_date=data.end_date,
        shift=_clean(data.shift) or None,
        payroll_no=payroll,
        user_id=data.user_id,
    )


def current(item: StaffMember) -> StaffData:
    return StaffData(
        full_name=item.full_name,
        position=item.position,
        employer=item.employer,
        contractor_name=item.contractor_name,
        phone=item.phone,
        start_date=item.start_date,
        end_date=item.end_date,
        shift=item.shift,
        payroll_no=item.payroll_no,
        user_id=item.user_id,
    )


def _apply(item: StaffMember, data: StaffData) -> None:
    item.full_name = data.full_name or ""
    item.position = data.position or ""
    item.employer = data.employer or ""
    item.contractor_name = data.contractor_name
    item.phone = data.phone
    item.start_date = data.start_date or date.min
    item.end_date = data.end_date
    item.shift = data.shift
    item.payroll_no = data.payroll_no
    item.user_id = data.user_id


async def _check_unique(session: AsyncSession, data: StaffData, item_id: uuid.UUID) -> None:
    if data.payroll_no is not None:
        taken = await session.scalar(
            select(StaffMember.id).where(
                StaffMember.payroll_no == data.payroll_no, StaffMember.id != item_id
            )
        )
        if taken is not None:
            raise StaffRuleError("payroll_no_taken", "Bu sicil no başka bir personelde kayıtlı.")
    if data.user_id is not None:
        membership = await session.scalar(
            select(SiteMembership.id).where(
                SiteMembership.user_id == data.user_id,
                SiteMembership.user_id.in_(_STAFF_USERS),
            )
        )
        if membership is None:
            raise FormFieldError({"user_id": "Bu sitede kullanıcı hesabı olan birini seçin."})
        linked = await session.scalar(
            select(StaffMember.id).where(
                StaffMember.user_id == data.user_id, StaffMember.id != item_id
            )
        )
        if linked is not None:
            raise StaffRuleError("user_taken", "Bu giriş hesabı başka bir personel kartına bağlı.")


async def close_access(session: AsyncSession, item: StaffMember, now: datetime) -> bool:
    """K29: ayrılan personelin bu sitedeki üyeliği pasifleşir (kayıt silinmez). Kapandıysa True.
    Sakin üyeliğine dokunulmaz (personel aynı sitede sakin de olabilir)."""
    if item.user_id is None or item.access_closed_at is not None:
        return False
    membership = await session.scalar(
        select(SiteMembership).where(SiteMembership.user_id == item.user_id).with_for_update()
    )
    is_staff = await session.scalar(
        select(SiteRoleAssignment.id)
        .where(
            SiteRoleAssignment.user_id == item.user_id,
            SiteRoleAssignment.role_key != RESIDENT_KEY,
        )
        .limit(1)
    )
    if membership is not None and is_staff is not None:
        membership.is_active = False
    item.access_closed_at = now
    await session.flush()
    return True


async def create(
    session: AsyncSession, data: StaffData, *, today: date, now: datetime
) -> StaffMember:
    item = StaffMember(id=uuid.uuid7())
    checked = check(data)
    await _check_unique(session, checked, item.id)
    _apply(item, checked)
    session.add(item)
    await session.flush()
    if item.end_date is not None and item.end_date < today:
        await close_access(session, item, now)
    return item


async def update(
    session: AsyncSession, item: StaffMember, data: StaffData, *, today: date, now: datetime
) -> StaffMember:
    checked = check(data)
    await _check_unique(session, checked, item.id)
    if checked.user_id != item.user_id:
        item.access_closed_at = None  # başka hesap: kapanış ona ayrıca uygulanır
    _apply(item, checked)
    await session.flush()
    if item.end_date is not None and item.end_date < today:
        await close_access(session, item, now)
    return item


async def get(session: AsyncSession, staff_id: uuid.UUID) -> StaffMember | None:
    found: StaffMember | None = await session.scalar(
        select(StaffMember).where(StaffMember.id == staff_id).with_for_update()
    )
    return found


def list_query(active: bool | None, today: date) -> Select[StaffMember]:
    query = select(StaffMember)
    working = or_(StaffMember.end_date.is_(None), StaffMember.end_date >= today)
    if active is True:
        query = query.where(working)
    elif active is False:
        query = query.where(StaffMember.end_date < today)
    return query.order_by(StaffMember.full_name, StaffMember.id)


@dataclass(frozen=True, slots=True)
class OpenItems:
    future_shifts: int
    first_shift: date | None
    pending_leaves: int


async def open_items(session: AsyncSession, staff_id: uuid.UUID, after: date) -> OpenItems:
    """Ayrılışta devredilmesi gerekenler (IK-01 adım 2): ileri tarihli vardiya ve bekleyen izin.
    Yalnız bilgi; ayrılış kaydı engellenmez."""
    count, first = (
        await session.execute(
            select(func.count(), func.min(ShiftAssignment.work_date)).where(
                ShiftAssignment.staff_id == staff_id, ShiftAssignment.work_date >= after
            )
        )
    ).one()
    pending = await session.scalar(
        select(func.count()).where(
            LeaveRequest.staff_id == staff_id, LeaveRequest.status == LeaveStatus.PENDING.value
        )
    )
    return OpenItems(int(count), first, int(pending or 0))


@dataclass(frozen=True, slots=True)
class Linkable:
    user_id: uuid.UUID
    full_name: str
    email: str
    role: str
    staff_id: uuid.UUID | None  # bağlı olduğu kart (varsa)


async def linkable_users(session: AsyncSession) -> list[Linkable]:
    """K29: karta bağlanabilecek hesaplar — bu sitede açık (sakin olmayan) etkin üyeliği olanlar."""
    rows = await session.execute(
        select(User, StaffMember.id)
        .join(SiteMembership, SiteMembership.user_id == User.id)
        .outerjoin(StaffMember, StaffMember.user_id == User.id)
        .where(SiteMembership.user_id.in_(_STAFF_USERS), SiteMembership.is_active)
        .order_by(User.full_name, User.id)
    )
    found = rows.all()
    catalog = await roles_svc.load_catalog(session)
    names: dict[uuid.UUID, list[tuple[int, str]]] = {}
    for a in await session.scalars(
        select(SiteRoleAssignment).where(
            SiteRoleAssignment.user_id.in_([u.id for u, _ in found]),
            SiteRoleAssignment.role_key != RESIDENT_KEY,
            SiteRoleAssignment.removed_at.is_(None),
        )
    ):
        role = catalog.by_key(a.role_key)
        if role is not None:
            names.setdefault(a.user_id, []).append((role.level, role.name))
    return [
        Linkable(
            u.id, u.full_name, u.email or u.phone or "", min(names.get(u.id, [(0, "")]))[1], sid
        )
        for u, sid in found
    ]


async def user_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return dict(rows.all())


async def own_card(session: AsyncSession, user_id: uuid.UUID) -> StaffMember | None:
    """K29: oturumdaki kullanıcının bu sitedeki personel kartı."""
    found: StaffMember | None = await session.scalar(
        select(StaffMember).where(StaffMember.user_id == user_id)
    )
    return found


@dataclass(frozen=True, slots=True)
class ClosedAccess:
    site_id: uuid.UUID
    staff_id: uuid.UUID
    full_name: str


async def run_offboarding(
    factory: async_sessionmaker[AsyncSession], *, today: date, now: datetime
) -> list[ClosedAccess]:
    """Gece işi (IK-01 adım 4): ayrılış tarihi geçen ve hesabı bağlı personelin site üyeliğini
    pasifleştirir. Site başına ayrı transaction; tekrar güvenli."""
    with all_sites_scope():
        async with factory() as session:
            due = (
                await session.execute(
                    select(StaffMember.site_id, StaffMember.id).where(
                        StaffMember.end_date < today,
                        StaffMember.user_id.is_not(None),
                        StaffMember.access_closed_at.is_(None),
                    )
                )
            ).all()
    set_actor(Actor(None, JOB_ACTOR, None, kind=ActorType.SYSTEM))
    closed: list[ClosedAccess] = []
    for site_id, staff_id in due:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                item = await get(session, staff_id)
                if item is not None and await close_access(session, item, now):
                    closed.append(ClosedAccess(site_id, item.id, item.full_name))
    return closed
