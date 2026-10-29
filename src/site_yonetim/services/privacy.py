"""KVKK: aydınlatma metni, onay kaydı, açık rıza, ilgili kişi başvurusu, kişisel veri paketi
(Yazılım Dokümanı RAP-04, PLT-01 adım 14–15; servis isteği 80; PM kararı P6).

Onay kapısı (`api/deps.py`) kullanıcının **erişebildiği her sitenin son sürümünü** onaylamış
olmasını ister. Bu sorgu siteleri aşar: tek bilinçli `all_sites_scope()` kullanımı buradadır ve
yalnız o kullanıcının satırlarıyla sınırlıdır (services/access.py ile aynı ilke).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.privacy import (
    KIND_LABELS,
    PURPOSE_LABELS,
    CheckedAnswer,
    CheckedNotice,
    CheckedRequest,
    ConsentSource,
    ExplicitPurpose,
    PrivacyRequestKind,
    PrivacyRequestStatus,
    due_on,
    needs_warning,
)
from site_yonetim.models import (
    AccountBalance,
    Block,
    ExplicitConsent,
    HouseholdMember,
    LedgerAccount,
    LedgerEntry,
    Person,
    PrivacyConsent,
    PrivacyNotice,
    PrivacyRequest,
    Registration,
    Request,
    Site,
    StructureSettings,
    Unit,
    UnitParty,
    User,
    Vehicle,
    VisitorInvite,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import roles as roles_svc

RECENT_ENTRIES = 100


class PrivacyRuleError(Exception):
    """İş kuralı: `status` 403/404/409."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- Aydınlatma metni -------------------------------------------------------------------------


def notices_query() -> Select[PrivacyNotice]:
    return select(PrivacyNotice).order_by(PrivacyNotice.version.desc())


async def latest_notice(session: AsyncSession) -> PrivacyNotice | None:
    found: PrivacyNotice | None = await session.scalar(notices_query().limit(1))
    return found


async def publish(
    session: AsyncSession,
    site: Site,
    checked: CheckedNotice,
    *,
    user_id: uuid.UUID,
    actor: str,
    ip: str | None,
    user_agent: str | None,
) -> PrivacyNotice:
    """Yeni sürüm: önceki sürümler değişmez; herkes yeniden onaylar (P6). Yayınlayanın onayı
    yayınla birlikte yazılır (metni okuyup yayınladı)."""
    # Sürüm numarası için site satırı kilitlenir (eşzamanlı iki yayın aynı numarayı almasın).
    await session.scalar(select(Site.id).where(Site.id == site.id).with_for_update())
    current = await session.scalar(select(func.max(PrivacyNotice.version)))
    notice = PrivacyNotice(
        version=(current or 0) + 1,
        text=checked.text,
        summary=checked.summary,
        published_by_user_id=user_id,
        published_by_name=actor,
    )
    session.add(notice)
    await session.flush()
    session.add(
        PrivacyConsent(
            user_id=user_id,
            notice_id=notice.id,
            version=notice.version,
            source=ConsentSource.PUBLISHER.value,
            ip=ip,
            user_agent=user_agent,
        )
    )
    config = await session.scalar(select(StructureSettings))
    if config is not None and not config.kvkk_published:
        config.kvkk_published = True  # kurulum kontrol listesi (PLT-10 adım 9)
    await session.flush()
    return notice


async def public_notice(
    factory: async_sessionmaker[AsyncSession], slug: str
) -> tuple[Site, PrivacyNotice | None] | None:
    """Herkese açık `/aydinlatma`: sitenin son sürümü (site adı + metin; kişi verisi yok)."""
    with all_sites_scope():
        async with factory() as session:
            site = await session.scalar(select(Site).where(Site.slug == slug))
            if site is None:
                return None
            notice: PrivacyNotice | None = await session.scalar(
                notices_query().where(PrivacyNotice.site_id == site.id).limit(1)
            )
    return site, notice


# --- Onay (P6) ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingNotice:
    notice_id: uuid.UUID
    site_id: uuid.UUID
    version: int
    text: str
    summary: str
    published_at: datetime


def _latest_per_site(site_ids: Sequence[uuid.UUID]) -> Any:
    return (
        select(
            PrivacyNotice.site_id.label("site_id"),
            func.max(PrivacyNotice.version).label("version"),
        )
        .where(PrivacyNotice.site_id.in_(site_ids))
        .group_by(PrivacyNotice.site_id)
        .subquery()
    )


async def pending_notices(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    site_ids: Sequence[uuid.UUID],
) -> list[PendingNotice]:
    """Kullanıcının erişebildiği sitelerde onaylamadığı **son** sürümler. Sitede yayınlanmış
    metin yoksa onay istenmez."""
    if not site_ids:
        return []
    latest = _latest_per_site(site_ids)
    consented = (
        select(PrivacyConsent.id)
        .where(
            PrivacyConsent.user_id == user_id,
            PrivacyConsent.notice_id == PrivacyNotice.id,
        )
        .correlate(PrivacyNotice)
        .exists()
    )
    with all_sites_scope():
        async with factory() as session:
            rows = await session.scalars(
                select(PrivacyNotice)
                .join(
                    latest,
                    and_(
                        PrivacyNotice.site_id == latest.c.site_id,
                        PrivacyNotice.version == latest.c.version,
                    ),
                )
                .where(~consented)
                .order_by(PrivacyNotice.created_at)
            )
            return [
                PendingNotice(n.id, n.site_id, n.version, n.text, n.summary, n.created_at)
                for n in rows
            ]


async def accept(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    site_ids: Sequence[uuid.UUID],
    notice_ids: Sequence[uuid.UUID],
    *,
    ip: str | None,
    user_agent: str | None,
) -> int:
    """Onay ekranı: gönderilen her kimlik bekleyen bir sürüm olmalı (eski sürüm, başka site ya
    da zaten onaylanmış → 409). Her site kendi kapsamında yazılır."""
    pending = {p.notice_id: p for p in await pending_notices(factory, user_id, site_ids)}
    wanted = list(dict.fromkeys(notice_ids))
    if not wanted or any(n not in pending for n in wanted):
        raise PrivacyRuleError(
            409,
            "consent_outdated",
            "Onaylanacak metin güncel değil; sayfayı yenileyip son sürümü onaylayın.",
        )
    for notice_id in wanted:
        notice = pending[notice_id]
        with site_scope(notice.site_id):
            async with factory() as session, session.begin():
                session.add(
                    PrivacyConsent(
                        user_id=user_id,
                        notice_id=notice.notice_id,
                        version=notice.version,
                        source=ConsentSource.LOGIN.value,
                        ip=ip,
                        user_agent=user_agent,
                    )
                )
    return len(wanted)


@dataclass(frozen=True, slots=True)
class GivenConsent:
    site_id: uuid.UUID
    version: int
    source: str
    accepted_at: datetime


async def given_consents(
    factory: async_sessionmaker[AsyncSession],
    user_ids: Collection[uuid.UUID],
    site_ids: Sequence[uuid.UUID],
) -> list[GivenConsent]:
    """Hesap(lar)ın onay geçmişi, yeni üstte. Sakin olarak görüntülemede sakinin hesapları."""
    if not site_ids or not user_ids:
        return []
    with all_sites_scope():
        async with factory() as session:
            rows = await session.scalars(
                select(PrivacyConsent)
                .where(PrivacyConsent.user_id.in_(user_ids), PrivacyConsent.site_id.in_(site_ids))
                .order_by(PrivacyConsent.created_at.desc())
            )
            return [GivenConsent(c.site_id, c.version, c.source, c.created_at) for c in rows]


async def record_consent(
    session: AsyncSession,
    user_id: uuid.UUID,
    notice: PrivacyNotice,
    source: ConsentSource,
    *,
    ip: str | None = None,
) -> bool:
    """Açık site kapsamında onay yazar (varsa yazmaz)."""
    exists = await session.scalar(
        select(PrivacyConsent.id).where(
            PrivacyConsent.user_id == user_id, PrivacyConsent.notice_id == notice.id
        )
    )
    if exists is not None:
        return False
    session.add(
        PrivacyConsent(
            user_id=user_id, notice_id=notice.id, version=notice.version, source=source.value, ip=ip
        )
    )
    await session.flush()
    return True


async def link_registration(session: AsyncSession, user_id: uuid.UUID, r: Registration) -> None:
    """Kayıt başvurusu onaylanınca formdaki bilgilendirme onayı hesaba bağlanır: başvuru
    anında yayında olan sürüm onaylanmış sayılır (daha yenisi varsa girişte yine sorulur).
    Formdaki açık rıza da (isteğe bağlı) ayrı kayda geçer."""
    notice: PrivacyNotice | None = await session.scalar(
        notices_query().where(PrivacyNotice.created_at <= r.kvkk_ack_at).limit(1)
    )
    if notice is not None:
        await record_consent(session, user_id, notice, ConsentSource.REGISTRATION, ip=r.ip)
    if r.explicit_consent_at is not None and await explicit_state(session, {user_id}) == {}:
        session.add(
            ExplicitConsent(
                user_id=user_id,
                purpose=ExplicitPurpose.COMMUNICATIONS.value,
                granted=True,
                source=ConsentSource.REGISTRATION.value,
                ip=r.ip,
            )
        )
        await session.flush()


async def link_invitation(session: AsyncSession, user_id: uuid.UUID, ip: str | None) -> None:
    """Davet kabul formundaki onay: o an yayındaki son sürüm."""
    notice = await latest_notice(session)
    if notice is not None:
        await record_consent(session, user_id, notice, ConsentSource.INVITATION, ip=ip)


# --- Açık rıza ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExplicitState:
    purpose: ExplicitPurpose
    granted: bool
    at: datetime


async def explicit_state(
    session: AsyncSession, user_ids: Collection[uuid.UUID]
) -> dict[ExplicitPurpose, ExplicitState]:
    """Amaç başına son karar (birden çok hesapta en yenisi geçerli)."""
    rows = await session.scalars(
        select(ExplicitConsent)
        .where(ExplicitConsent.user_id.in_(user_ids))
        .order_by(ExplicitConsent.created_at, ExplicitConsent.id)
    )
    state: dict[ExplicitPurpose, ExplicitState] = {}
    for row in rows:
        purpose = ExplicitPurpose(row.purpose)
        state[purpose] = ExplicitState(purpose, row.granted, row.created_at)
    return state


async def set_explicit(
    session: AsyncSession,
    user_id: uuid.UUID,
    purpose: ExplicitPurpose,
    *,
    granted: bool,
    ip: str | None,
) -> bool:
    """Değişiklik yoksa yazmaz (False)."""
    current = (await explicit_state(session, {user_id})).get(purpose)
    if current is not None and current.granted == granted:
        return False
    if current is None and not granted:
        return False
    session.add(
        ExplicitConsent(
            user_id=user_id,
            purpose=purpose.value,
            granted=granted,
            source=ConsentSource.LOGIN.value,
            ip=ip,
        )
    )
    await session.flush()
    return True


def purpose_label(purpose: ExplicitPurpose) -> str:
    return PURPOSE_LABELS[purpose]


# --- İlgili kişi başvurusu ------------------------------------------------------------------


async def _unit_names(session: AsyncSession, person_id: uuid.UUID, today: date) -> list[str]:
    rows = await session.execute(
        select(Block.name, Unit.number)
        .select_from(UnitParty)
        .join(Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(
            UnitParty.person_id == person_id,
            UnitParty.start_date <= today,
            (UnitParty.end_date.is_(None)) | (UnitParty.end_date >= today),
        )
        .order_by(Block.name, Unit.number)
    )
    return list(dict.fromkeys(f"{block}-{number}" for block, number in rows))


async def privacy_managers(
    session: AsyncSession, site: Site, now: datetime | None = None
) -> set[uuid.UUID]:
    return await roles_svc.permission_holders(session, site, Permission.PRIVACY_MANAGE, now)


async def create_request(
    session: AsyncSession,
    site: Site,
    checked: CheckedRequest,
    *,
    user: User,
    person_id: uuid.UUID,
    today: date,
) -> PrivacyRequest:
    open_same = await session.scalar(
        select(PrivacyRequest.id).where(
            PrivacyRequest.user_id == user.id,
            PrivacyRequest.kind == checked.kind.value,
            PrivacyRequest.status == PrivacyRequestStatus.OPEN.value,
        )
    )
    if open_same is not None:
        raise PrivacyRuleError(409, "already_open", "Aynı türde açık başvurunuz var.")
    person = await session.get(Person, person_id)
    name = f"{person.first_name} {person.last_name}" if person is not None else user.full_name
    request = PrivacyRequest(
        user_id=user.id,
        person_id=person_id,
        requester_name=name,
        unit_names=await _unit_names(session, person_id, today),
        kind=checked.kind.value,
        detail=checked.detail,
        due_on=due_on(today),
    )
    session.add(request)
    await session.flush()
    await notify_svc.notify(
        session,
        await privacy_managers(session, site),
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="privacy.request_created",
            title=f"KVKK başvurusu: {KIND_LABELS[checked.kind]}",
            body=f"{name} — son gün {request.due_on:%d.%m.%Y} (30 gün).",
            link_entity="privacy_requests",
            link_id=request.id,
        ),
        dedupe_key=f"privacy-request:{request.id}",
    )
    return request


def requests_query(user_ids: Collection[uuid.UUID] | None = None) -> Select[PrivacyRequest]:
    """Açıklar önce, son güne göre; yanıtlananlar en yeni önce. `user_ids`: başvuranın
    hesap(lar)ı (sakin ekranı); verilmezse tümü (KVKK sorumlusu)."""
    query = select(PrivacyRequest)
    if user_ids is not None:
        query = query.where(PrivacyRequest.user_id.in_(user_ids))
    return query.order_by(
        (PrivacyRequest.status == PrivacyRequestStatus.OPEN.value).desc(),
        PrivacyRequest.due_on,
        PrivacyRequest.created_at.desc(),
        PrivacyRequest.id,
    )


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, *, lock: bool = False
) -> PrivacyRequest | None:
    query = select(PrivacyRequest).where(PrivacyRequest.id == request_id)
    found: PrivacyRequest | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def answer(
    session: AsyncSession,
    request: PrivacyRequest,
    checked: CheckedAnswer,
    *,
    actor: str,
    now: datetime,
) -> PrivacyRequest:
    if request.status != PrivacyRequestStatus.OPEN.value:
        raise PrivacyRuleError(409, "invalid_status", "Başvuru zaten yanıtlandı.")
    request.status = PrivacyRequestStatus.ANSWERED.value
    request.response = checked.response
    request.outcome = checked.outcome.value
    request.retained = checked.retained
    request.answered_by_name = actor
    request.answered_at = now
    await session.flush()
    await notify_svc.notify(
        session,
        [request.user_id],
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind="privacy.request_answered",
            title=f"KVKK başvurunuz yanıtlandı: {KIND_LABELS[PrivacyRequestKind(request.kind)]}",
            body=checked.response[:200],
            link_entity="privacy_requests",
            link_id=request.id,
        ),
        dedupe_key=f"privacy-answer:{request.id}",
    )
    return request


# --- Kişisel veri paketi (erişim başvurusu, "verilerimi indir") ---------------------------


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def data_package(
    session: AsyncSession,
    site: Site,
    *,
    user_id: uuid.UUID,
    person_id: uuid.UUID | None,
    now: datetime,
) -> dict[str, Any]:
    """Kişiye ait kayıtların dökümü (JSON). Site kapsamı açık olmalı; yalnız bu sitenin verisi
    (veri sorumlusu site yönetimidir)."""
    user = await session.get(User, user_id)
    out: dict[str, Any] = {
        "hazirlanma": now.isoformat(),
        "veri_sorumlusu": site.name,
        "hesap": {
            "ad_soyad": user.full_name if user else None,
            "eposta": user.email if user else None,
        },
    }
    if person_id is not None:
        person = await session.get(Person, person_id)
        if person is not None:
            out["kisi"] = {
                "ad": person.first_name,
                "soyad": person.last_name,
                "telefon": person.phone,
                "eposta": person.email,
            }
        out.update(await _person_records(session, person_id))
    notices = {n.id: n for n in await session.scalars(select(PrivacyNotice))}
    out["aydinlatma_onaylari"] = [
        {"surum": c.version, "kaynak": c.source, "tarih": _iso(c.created_at)}
        for c in await session.scalars(
            select(PrivacyConsent)
            .where(PrivacyConsent.user_id == user_id)
            .order_by(PrivacyConsent.created_at)
        )
        if c.notice_id in notices
    ]
    out["acik_riza"] = [
        {"amac": PURPOSE_LABELS[ExplicitPurpose(c.purpose)], "verildi": c.granted,
         "tarih": _iso(c.created_at)}
        for c in await session.scalars(
            select(ExplicitConsent)
            .where(ExplicitConsent.user_id == user_id)
            .order_by(ExplicitConsent.created_at)
        )
    ]  # fmt: skip
    out["kvkk_basvurulari"] = [
        {
            "tur": KIND_LABELS[PrivacyRequestKind(r.kind)],
            "aciklama": r.detail,
            "tarih": _iso(r.created_at),
            "durum": r.status,
            "yanit": r.response,
        }
        for r in await session.scalars(
            select(PrivacyRequest)
            .where(PrivacyRequest.user_id == user_id)
            .order_by(PrivacyRequest.created_at)
        )
    ]
    return out


async def _person_records(session: AsyncSession, person_id: uuid.UUID) -> dict[str, Any]:
    units = {
        u.id: f"{b.name}-{u.number}"
        for u, b in await session.execute(
            select(Unit, Block).join(
                Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id)
            )
        )
    }
    parties = list(
        await session.scalars(
            select(UnitParty).where(UnitParty.person_id == person_id).order_by(UnitParty.start_date)
        )
    )
    unit_ids = {p.unit_id for p in parties}
    accounts = list(
        await session.scalars(
            select(LedgerAccount)
            .where(LedgerAccount.person_id == person_id)
            .order_by(LedgerAccount.reference_code)
        )
    )
    balances = {
        b.account_id: b.balance
        for b in await session.scalars(
            select(AccountBalance).where(AccountBalance.account_id.in_([a.id for a in accounts]))
        )
    }
    entries = list(
        await session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.account_id.in_([a.id for a in accounts]))
            .order_by(LedgerEntry.date.desc(), LedgerEntry.created_at.desc())
            .limit(RECENT_ENTRIES)
        )
    )
    codes = {a.id: a.reference_code for a in accounts}
    requests = await session.scalars(
        select(Request).where(Request.reported_by_person_id == person_id).order_by(Request.number)
    )
    vehicles = await session.scalars(
        select(Vehicle).where(Vehicle.owner_person_id == person_id).order_by(Vehicle.plate)
    )
    household: list[HouseholdMember] = (
        list(
            await session.scalars(
                select(HouseholdMember)
                .where(HouseholdMember.unit_id.in_(unit_ids))
                .order_by(HouseholdMember.created_at)
            )
        )
        if unit_ids
        else []
    )
    invites = await session.scalars(
        select(VisitorInvite)
        .where(VisitorInvite.created_by_person_id == person_id)
        .order_by(VisitorInvite.date_from.desc())
        .limit(RECENT_ENTRIES)
    )
    registrations = await session.scalars(
        select(Registration).where(Registration.person_id == person_id)
    )
    return {
        "bolumler": [
            {"bolum": units.get(p.unit_id), "rol": p.role, "pay": format(p.share_percent, "f"),
             "baslangic": _iso(p.start_date), "bitis": _iso(p.end_date)}
            for p in parties
        ],
        "hesaplar": [
            {"kod": a.reference_code, "bolum": units.get(a.unit_id), "tur": a.kind,
             "bakiye": format(balances.get(a.id, 0), ".2f"), "kapali": a.is_closed}
            for a in accounts
        ],
        "son_hareketler": [
            {"hesap": codes.get(e.account_id), "tarih": _iso(e.date), "aciklama": e.description,
             "borc": format(e.debit, "f"), "alacak": format(e.credit, "f")}
            for e in entries
        ],
        "talepler": [
            {"no": r.number, "baslik": r.title, "durum": r.status, "tarih": _iso(r.created_at)}
            for r in requests
        ],
        "araclar": [
            {"plaka": v.plate, "marka_model": v.brand_model, "renk": v.color, "durum": v.status}
            for v in vehicles
        ],
        "hane": [
            {"ad_soyad": h.full_name, "telefon": h.phone, "bolum": units.get(h.unit_id),
             "durum": h.status}
            for h in household
        ],
        "ziyaretci_davetleri": [
            {"ziyaretci": i.visitor_name, "plaka": i.plate, "baslangic": _iso(i.date_from),
             "bitis": _iso(i.date_to)}
            for i in invites
        ],
        "kayit_basvurulari": [
            {"ad": r.first_name, "soyad": r.last_name, "telefon": r.phone, "eposta": r.email,
             "durum": r.status, "tarih": _iso(r.created_at)}
            for r in registrations
        ],
    }  # fmt: skip


# --- Gece işi: 30 gün sayacı uyarıları ------------------------------------------------------


async def warn_due(session: AsyncSession, site: Site, *, today: date) -> int:
    """Son 7 gün ve gecikmede yöneticilere günde bir uyarı."""
    rows = [
        r
        for r in await session.scalars(
            select(PrivacyRequest)
            .where(PrivacyRequest.status == PrivacyRequestStatus.OPEN.value)
            .order_by(PrivacyRequest.due_on)
        )
        if needs_warning(r.due_on, today) and r.warned_on != today
    ]
    if not rows:
        return 0
    managers = await privacy_managers(session, site)
    for r in rows:
        left = (r.due_on - today).days
        await notify_svc.notify(
            session,
            managers,
            notify_svc.Message(
                category=NotificationCategory.OPERATIONS,
                kind="privacy.request_due",
                title=(
                    f"KVKK başvurusu {-left} gün gecikti"
                    if left < 0
                    else f"KVKK başvurusuna {left} gün kaldı"
                ),
                body=f"{r.requester_name} — {KIND_LABELS[PrivacyRequestKind(r.kind)]}",
                link_entity="privacy_requests",
                link_id=r.id,
            ),
            dedupe_key=f"privacy-due:{r.id}:{today.isoformat()}",
        )
        r.warned_on = today
    await session.flush()
    return len(rows)


async def run_due_warnings(
    factory: async_sessionmaker[AsyncSession], *, today: date
) -> dict[uuid.UUID, int]:
    set_actor(Actor(None, "KVKK gece işi", None))
    with all_sites_scope():
        async with factory() as session:
            sites = list(await session.scalars(select(Site).order_by(Site.id)))
    out: dict[uuid.UUID, int] = {}
    for site in sites:
        with site_scope(site.id):
            async with factory() as session, session.begin():
                warned = await warn_due(session, site, today=today)
        if warned:
            out[site.id] = warned
    return out
