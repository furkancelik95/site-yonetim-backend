"""Platform destek — Yazılım Dokümanı PYN-04, PYN-06, PLT-12 (servis istekleri 82–84; K12).

Kurallar `domain/support.py`'de; burası veritabanı ve transaction işini yürütür.

- Site tarafı (bilet, oturum onayı, sakin olarak görüntüle) açık site kapsamında çalışır.
- Platform tarafı biletleri ve oturumları **bilinçli** `all_sites_scope()` ile okur; bir siteye
  yazarken o sitenin kapsamında yeni oturum açar (`in_site`).
- Destek oturumu ve sakin olarak görüntüleme erişimi `services/access.py` ve `api/deps.py`'de
  uygulanır (salt okunur, her istek denetim kaydına).
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.support import (
    OPEN_SESSION_STATUSES,
    VIEW_AS_MINUTES,
    MessageSide,
    SupportSessionStatus,
    SupportTier,
    TicketCategory,
    TicketPriority,
    TicketRuleError,
    TicketStatus,
    ViewAsStatus,
    after_customer_message,
    after_support_message,
    close_by_customer,
    routes_to_on_call,
    session_effective_status,
    sla_due,
    view_as_effective_status,
)
from site_yonetim.models import (
    KnowledgeArticle,
    KnowledgeFeedback,
    Organization,
    Person,
    Plan,
    Site,
    SiteMembership,
    StructureSettings,
    SupportSession,
    SupportTicket,
    SupportTicketMessage,
    ViewAsSession,
)
from site_yonetim.models.audit import Actor, ActorType, set_actor
from site_yonetim.services.notifications import Message, notify
from site_yonetim.services.roles import permission_holders

Factory = async_sessionmaker[AsyncSession]
JOB_ACTOR = "Destek işleri"


class SupportRuleError(Exception):
    """İş kuralı ihlali: `code` (409 `exists`, `invalid_status`, 422 alan…)."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


@asynccontextmanager
async def in_site(factory: Factory, site_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Platform ucundan bir siteye yazmak için o sitenin kapsamında yeni oturum."""
    with site_scope(site_id):
        async with factory() as session:
            yield session


# --- Paket destek seviyesi ---------------------------------------------------------------


async def support_tier(session: AsyncSession, site: Site) -> SupportTier:
    """Sitenin planı; yoksa yönetim şirketinin planı; o da yoksa mesai içi."""
    plan_id = site.plan_id
    if plan_id is None and site.organization_id is not None:
        plan_id = await session.scalar(
            select(Organization.plan_id).where(Organization.id == site.organization_id)
        )
    if plan_id is None:
        return SupportTier.BUSINESS_HOURS
    tier = await session.scalar(select(Plan.support_tier).where(Plan.id == plan_id))
    return SupportTier(tier) if tier else SupportTier.BUSINESS_HOURS


# --- Biletler -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TicketView:
    ticket: SupportTicket
    site: Site
    messages: list[SupportTicketMessage]


async def _next_number(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT nextval('support_ticket_number_seq')"))
    return int(value)


async def open_ticket(
    session: AsyncSession,
    site: Site,
    *,
    user_id: uuid.UUID,
    user_name: str,
    title: str,
    body: str,
    category: TicketCategory,
    priority: TicketPriority,
    path: str,
    app_version: str,
    user_agent: str,
    correlation_id: str,
    now: datetime,
) -> SupportTicket:
    tier = await support_tier(session, site)
    due = sla_due(tier, priority, now)
    ticket = SupportTicket(
        number=await _next_number(session),
        category=category.value,
        priority=priority.value,
        status=TicketStatus.OPEN.value,
        title=title,
        context_path=path,
        app_version=app_version,
        user_agent=user_agent,
        correlation_id=correlation_id,
        created_by_id=user_id,
        created_by_name=user_name,
        support_tier=tier.value,
        first_due=due.first_due,
        resolve_due=due.resolve_due,
        on_call=routes_to_on_call(tier, priority),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        SupportTicketMessage(
            ticket_id=ticket.id,
            side=MessageSide.CUSTOMER.value,
            author_id=user_id,
            author_name=user_name,
            body=body,
        )
    )
    await session.flush()
    return ticket


async def messages_of(
    session: AsyncSession, ticket_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[SupportTicketMessage]]:
    found: dict[uuid.UUID, list[SupportTicketMessage]] = {t: [] for t in ticket_ids}
    if ticket_ids:
        rows = await session.scalars(
            select(SupportTicketMessage)
            .where(SupportTicketMessage.ticket_id.in_(ticket_ids))
            .order_by(SupportTicketMessage.created_at, SupportTicketMessage.id)
        )
        for row in rows:
            found[row.ticket_id].append(row)
    return found


async def get_ticket(session: AsyncSession, ticket_id: uuid.UUID) -> SupportTicket | None:
    return await session.scalar(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    )


def _rule(exc: TicketRuleError) -> SupportRuleError:
    return SupportRuleError(exc.code, exc.message)


async def customer_message(
    session: AsyncSession,
    ticket: SupportTicket,
    *,
    user_id: uuid.UUID,
    user_name: str,
    body: str,
) -> None:
    try:
        status = after_customer_message(TicketStatus(ticket.status))
    except TicketRuleError as exc:
        raise _rule(exc) from exc
    session.add(
        SupportTicketMessage(
            ticket_id=ticket.id,
            side=MessageSide.CUSTOMER.value,
            author_id=user_id,
            author_name=user_name,
            body=body,
        )
    )
    if ticket.status != status.value:
        ticket.status = status.value
        ticket.resolved_at = None  # yeniden açıldı: çözüm süresi işlemeye devam eder
    await session.flush()


async def close_ticket(session: AsyncSession, ticket: SupportTicket, *, now: datetime) -> None:
    try:
        ticket.status = close_by_customer(TicketStatus(ticket.status)).value
    except TicketRuleError as exc:
        raise _rule(exc) from exc
    ticket.closed_at = now
    await session.flush()


async def support_reply(
    session: AsyncSession,
    ticket: SupportTicket,
    *,
    user_id: uuid.UUID,
    user_name: str,
    body: str,
    status: TicketStatus,
    now: datetime,
) -> None:
    try:
        new_status = after_support_message(TicketStatus(ticket.status), status)
    except TicketRuleError as exc:
        raise _rule(exc) from exc
    session.add(
        SupportTicketMessage(
            ticket_id=ticket.id,
            side=MessageSide.SUPPORT.value,
            author_id=user_id,
            author_name=f"{user_name} (destek)",
            body=body,
        )
    )
    ticket.status = new_status.value
    if ticket.first_response_at is None:
        ticket.first_response_at = now
    if new_status is TicketStatus.RESOLVED:
        ticket.resolved_at = now
    await session.flush()


async def notify_ticket_answer(session: AsyncSession, ticket: SupportTicket) -> None:
    """Destek yanıtı bileti açan kişinin bildirim merkezine (site kapsamında)."""
    if ticket.created_by_id is None:
        return
    await notify(
        session,
        [ticket.created_by_id],
        Message(
            NotificationCategory.OPERATIONS,
            "support.ticket_answered",
            f"Destek bileti yanıtlandı: {ticket.title}",
            link_entity="support_tickets",
            link_id=ticket.id,
        ),
        dedupe_key=f"support-ticket:{ticket.id}:{uuid.uuid7()}",
    )


# --- Bilgi bankası ---------------------------------------------------------------------


async def feedback_counts(
    session: AsyncSession, article_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """Makale → (faydalı, faydasız) — veritabanında sayılır."""
    if not article_ids:
        return {}
    rows = await session.execute(
        select(
            KnowledgeFeedback.article_id,
            func.count().filter(KnowledgeFeedback.helpful),
            func.count().filter(~KnowledgeFeedback.helpful),
        )
        .where(KnowledgeFeedback.article_id.in_(article_ids))
        .group_by(KnowledgeFeedback.article_id)
    )
    return {aid: (int(yes), int(no)) for aid, yes, no in rows}


async def give_feedback(
    session: AsyncSession, article: KnowledgeArticle, *, user_id: uuid.UUID, helpful: bool
) -> None:
    """Kullanıcı başına makale başına tek oy; ikinci oy öncekinin yerine geçer."""
    stmt = insert(KnowledgeFeedback).values(
        id=uuid.uuid7(), article_id=article.id, user_id=user_id, helpful=helpful
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["article_id", "user_id"],
            set_={"helpful": stmt.excluded.helpful, "updated_at": func.now()},
        )
    )


# --- Destek oturumu ---------------------------------------------------------------------


async def expire_stale_sessions(session: AsyncSession, now: datetime) -> int:
    """Süresi dolan aktif oturumları kapatır (kapsamdaki site ya da tüm siteler)."""
    rows = list(
        await session.scalars(
            select(SupportSession)
            .where(
                SupportSession.status == SupportSessionStatus.ACTIVE.value,
                SupportSession.ends_at <= now,
            )
            .with_for_update()
        )
    )
    for row in rows:
        row.status = SupportSessionStatus.EXPIRED.value
    await session.flush()
    return len(rows)


async def request_session(
    session: AsyncSession,
    site: Site,
    *,
    user_id: uuid.UUID,
    user_name: str,
    reason: str,
    hours: int,
    now: datetime,
) -> SupportSession:
    """Talep açılır, tesisin `members.manage` yetkililerine onay bildirimi gider (adım 1–2)."""
    await expire_stale_sessions(session, now)
    existing = await session.scalar(
        select(SupportSession.id).where(
            SupportSession.status.in_([s.value for s in OPEN_SESSION_STATUSES])
        )
    )
    if existing is not None:
        raise SupportRuleError("exists", "Bu tesis için bekleyen ya da aktif oturum var.")
    row = SupportSession(
        reason=reason,
        hours=hours,
        status=SupportSessionStatus.PENDING.value,
        requested_by_id=user_id,
        requested_by_name=user_name,
    )
    session.add(row)
    await session.flush()
    managers = await permission_holders(session, site, Permission.MEMBERS_MANAGE, now)
    await notify(
        session,
        managers,
        Message(
            NotificationCategory.SECURITY,
            "support.session_requested",
            "Destek oturumu onayınızı bekliyor",
            f"{user_name}, {hours} saat salt okunur erişim istiyor: {reason}",
            link_entity="support_sessions",
            link_id=row.id,
        ),
        dedupe_key=f"support-session:{row.id}",
    )
    return row


async def get_session(session: AsyncSession, session_id: uuid.UUID) -> SupportSession | None:
    return await session.scalar(
        select(SupportSession).where(SupportSession.id == session_id).with_for_update()
    )


def _require(row: SupportSession, status: SupportSessionStatus, now: datetime) -> None:
    effective = session_effective_status(SupportSessionStatus(row.status), row.ends_at, now)
    if effective is not status:
        message = (
            "Aktif destek oturumu yok."
            if status is SupportSessionStatus.ACTIVE
            else "Oturum karara bağlanmış."
        )
        raise SupportRuleError("invalid_status", message)


async def approve_session(
    session: AsyncSession, row: SupportSession, *, user_id: uuid.UUID, user_name: str, now: datetime
) -> None:
    _require(row, SupportSessionStatus.PENDING, now)
    row.status = SupportSessionStatus.ACTIVE.value
    row.decided_by_id = user_id
    row.decided_by_name = user_name
    row.decided_at = now
    row.ends_at = now + timedelta(hours=row.hours)
    await session.flush()


async def reject_session(
    session: AsyncSession,
    row: SupportSession,
    *,
    user_id: uuid.UUID,
    user_name: str,
    reason: str | None,
    now: datetime,
) -> None:
    _require(row, SupportSessionStatus.PENDING, now)
    row.status = SupportSessionStatus.REJECTED.value
    row.decided_by_id = user_id
    row.decided_by_name = user_name
    row.decided_at = now
    row.reject_reason = reason
    await session.flush()


async def end_session(
    session: AsyncSession, row: SupportSession, *, user_name: str, now: datetime
) -> None:
    """Tesis yöneticisi oturumu süre dolmadan bitirir; erişim bir sonraki istekte kapanır.
    Oturum içinde açılmış sakin olarak görüntülemeler de kapanır."""
    _require(row, SupportSessionStatus.ACTIVE, now)
    row.status = SupportSessionStatus.EXPIRED.value
    row.ends_at = now
    row.ended_by_name = user_name
    await session.execute(
        update(ViewAsSession)
        .where(
            ViewAsSession.support_session_id == row.id,
            ViewAsSession.status == ViewAsStatus.ACTIVE.value,
        )
        .values(status=ViewAsStatus.ENDED.value, ended_at=now)
    )
    await session.flush()


async def active_session(session: AsyncSession, now: datetime) -> SupportSession | None:
    return await session.scalar(
        select(SupportSession).where(
            SupportSession.status == SupportSessionStatus.ACTIVE.value,
            SupportSession.ends_at > now,
        )
    )


@dataclass(frozen=True, slots=True)
class SupportGrantRow:
    site: Site
    session_id: uuid.UUID
    ends_at: datetime


async def granted_sessions(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> list[SupportGrantRow]:
    """Platform kullanıcısının açtığı, onaylanmış ve süresi dolmamış oturumlar. Bütün siteleri
    okur — çağıran `all_sites_scope()` açar; sorgu yalnız bu kullanıcının oturumlarıyla sınırlı."""
    rows = await session.execute(
        select(SupportSession, Site)
        .join(Site, Site.id == SupportSession.site_id)
        .where(
            SupportSession.requested_by_id == user_id,
            SupportSession.status == SupportSessionStatus.ACTIVE.value,
            SupportSession.ends_at > now,
        )
    )
    return [
        SupportGrantRow(site, row.id, row.ends_at) for row, site in rows if row.ends_at is not None
    ]


# --- Sakin olarak görüntüle ---------------------------------------------------------------


async def notify_setting(session: AsyncSession) -> bool:
    value = await session.scalar(select(StructureSettings.view_as_notify_resident))
    return True if value is None else bool(value)


async def set_notify_setting(session: AsyncSession, *, notify_resident: bool) -> None:
    row = await session.scalar(select(StructureSettings).with_for_update())
    if row is None:
        row = StructureSettings()
        session.add(row)
    row.view_as_notify_resident = notify_resident
    await session.flush()


async def resident_users(session: AsyncSession, person: Person) -> set[uuid.UUID]:
    """Kişinin hesapları: kişi kartındaki kullanıcı + kişiye bağlı site üyelikleri."""
    users = set(
        await session.scalars(
            select(SiteMembership.user_id).where(
                SiteMembership.person_id == person.id, SiteMembership.is_active
            )
        )
    )
    if person.user_id is not None:
        users.add(person.user_id)
    return users


async def start_view_as(
    session: AsyncSession,
    person: Person,
    *,
    viewer_id: uuid.UUID,
    viewer_name: str,
    reason: str,
    support_session_id: uuid.UUID | None,
    now: datetime,
) -> ViewAsSession:
    """En çok 30 dakikalık salt okunur oturum; görüntüleyenin önceki açık oturumu kapanır (tek
    görüntüleme). "Sakine bildir" ayarı açıksa sakine bildirim gider (PLT-12 adım 4–6)."""
    await session.execute(
        update(ViewAsSession)
        .where(
            ViewAsSession.viewer_id == viewer_id,
            ViewAsSession.status == ViewAsStatus.ACTIVE.value,
        )
        .values(status=ViewAsStatus.ENDED.value, ended_at=now)
    )
    row = ViewAsSession(
        viewer_id=viewer_id,
        viewer_name=viewer_name,
        person_id=person.id,
        reason=reason,
        support_session_id=support_session_id,
        status=ViewAsStatus.ACTIVE.value,
        ends_at=now + timedelta(minutes=VIEW_AS_MINUTES),
    )
    session.add(row)
    await session.flush()
    if await notify_setting(session):
        recipients = await resident_users(session, person)
        written = await notify(
            session,
            recipients,
            Message(
                NotificationCategory.SECURITY,
                "support.view_as",
                "Site yönetiminiz destek amacıyla hesabınızı görüntüledi",
                "Görüntüleme salt okunurdur; hesabınızda değişiklik yapılamaz.",
                link_entity="view_as_sessions",
                link_id=row.id,
            ),
            dedupe_key=f"view-as:{row.id}",
        )
        row.resident_notified = written > 0
        await session.flush()
    return row


async def find_view_as(session: AsyncSession, session_id: uuid.UUID) -> ViewAsSession | None:
    return await session.scalar(select(ViewAsSession).where(ViewAsSession.id == session_id))


async def usable_view_as(
    session: AsyncSession, session_id: uuid.UUID, viewer_id: uuid.UUID, now: datetime
) -> tuple[ViewAsSession, Person] | None:
    """İstekteki görüntüleme oturumu: bu kullanıcının, açık ve süresi dolmamış olmalı."""
    found = (
        await session.execute(
            select(ViewAsSession, Person)
            .join(Person, Person.id == ViewAsSession.person_id)
            .where(ViewAsSession.id == session_id, ViewAsSession.viewer_id == viewer_id)
        )
    ).first()
    if found is None:
        return None
    row, person = found
    status = view_as_effective_status(ViewAsStatus(row.status), row.ends_at, now)
    return (row, person) if status is ViewAsStatus.ACTIVE else None


async def end_view_as(session: AsyncSession, row: ViewAsSession, *, now: datetime) -> None:
    if row.status == ViewAsStatus.ACTIVE.value:
        expired = row.ends_at <= now
        row.status = (ViewAsStatus.EXPIRED if expired else ViewAsStatus.ENDED).value
        row.ended_at = min(now, row.ends_at)
        await session.flush()


# --- Zamanlanmış iş (dakikalık; SLA kontrolü dokümanda 15 dakika) ------------------------


@dataclass(frozen=True, slots=True)
class JobResult:
    sessions_expired: int
    view_as_expired: int
    first_breached: int
    resolve_breached: int


async def run_jobs(factory: Factory, *, now: datetime) -> JobResult:
    """Süresi dolan destek oturumlarını ve sakin olarak görüntülemeleri kapatır; SLA aşımını
    biletlere işler. Tekrar güvenli. Tüm siteler: bilinçli `all_sites_scope()`."""
    set_actor(Actor(None, JOB_ACTOR, None, kind=ActorType.SYSTEM))
    with all_sites_scope():
        async with factory() as session, session.begin():
            expired = await expire_stale_sessions(session, now)
            views = list(
                await session.scalars(
                    select(ViewAsSession)
                    .where(
                        ViewAsSession.status == ViewAsStatus.ACTIVE.value,
                        ViewAsSession.ends_at <= now,
                    )
                    .with_for_update()
                )
            )
            for view in views:
                view.status = ViewAsStatus.EXPIRED.value
                view.ended_at = view.ends_at
            open_statuses = [TicketStatus.CLOSED.value]
            first = list(
                await session.scalars(
                    select(SupportTicket)
                    .where(
                        SupportTicket.first_breached_at.is_(None),
                        SupportTicket.first_response_at.is_(None),
                        SupportTicket.status.not_in(open_statuses),
                        SupportTicket.first_due < now,
                    )
                    .with_for_update()
                )
            )
            for ticket in first:
                ticket.first_breached_at = now
            resolve = list(
                await session.scalars(
                    select(SupportTicket)
                    .where(
                        SupportTicket.resolve_breached_at.is_(None),
                        or_(
                            SupportTicket.resolved_at.is_(None),
                            SupportTicket.resolved_at > SupportTicket.resolve_due,
                        ),
                        SupportTicket.status.not_in(
                            [TicketStatus.CLOSED.value, TicketStatus.RESOLVED.value]
                        ),
                        SupportTicket.resolve_due < now,
                    )
                    .with_for_update()
                )
            )
            for ticket in resolve:
                ticket.resolve_breached_at = now
    return JobResult(expired, len(views), len(first), len(resolve))
