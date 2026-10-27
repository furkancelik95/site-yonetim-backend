"""Duyuru servisleri — docs/03 §9, docs/06 §2.11, Yazılım Dokümanı ILT-01, 16.2–16.3.
Açık site kapsamında; commit çağırana ait.

Yayınlama, hedef kitledeki her kişi ve kanal için bir teslim kaydı yazar. Gerçek gönderim yok
(docs/12 K5): uygulama içi teslim anında `sent_at` alır, diğer kanallar bekler (P16c gönderim
katmanı bağlanınca gönderilecek).

- **Zamanlama:** ileri tarihli duyuru `published_at` boş, `scheduled_for` dolu yazılır; dakikalık
  iş (`run-announcement-jobs`) vakti gelince yayınlar. Sessiz saate düşen zaman sessiz saat
  bitişine kayar; **kritik** önem aşar. İptal yalnız yayınlanmamışsa.
- **Okuma onayı:** son onay = yayın + `ack_hours`. Onay yalnız teslim kaydı olan (hedef
  kitledeki) kişiden; onay "okundu" da sayılır; bir kez. Süre dolunca onaylamayanlara **bir kez**
  SMS yedeği (`services/outbound` — sağlayıcı P16c ile bağlanacak; şimdilik sayı kayda geçer).
"""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, current_scope, site_scope
from site_yonetim.domain.communication import (
    ACK_HOURS_MAX,
    ACK_HOURS_MIN,
    DEFAULT_QUIET_FROM,
    DEFAULT_QUIET_TO,
    in_quiet,
    shift_out_of_quiet,
)
from site_yonetim.domain.money import EPSILON
from site_yonetim.domain.operations import (
    Audience,
    Channel,
    Importance,
    OperationRuleError,
    PartyRef,
    channels_of,
    recipients,
)
from site_yonetim.domain.structure import PartyRole
from site_yonetim.models import (
    AccountBalance,
    Announcement,
    AnnouncementDelivery,
    Block,
    LedgerAccount,
    Person,
    Site,
    SiteMembership,
    Unit,
    UnitParty,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import outbound
from site_yonetim.services.charging import BUSINESS_TZ

logger = logging.getLogger(__name__)
RESIDENT_PARTY_ROLES = (PartyRole.OWNER, PartyRole.TENANT, PartyRole.RESIDENT)


@dataclass(frozen=True, slots=True)
class NewAnnouncement:
    title: str
    body: str
    importance: Importance
    audience: Audience
    block_ids: tuple[uuid.UUID, ...]
    channels: tuple[Channel, ...]
    expires_on: date | None
    is_pinned: bool
    requires_ack: bool = False
    ack_hours: int | None = None
    sms_fallback: bool = False
    publish_at: datetime | None = None  # ileride ise zamanlanır


async def quiet_hours(_session: AsyncSession) -> tuple[time, time]:
    """Sitenin sessiz saatleri. Ayar ekranı ve tablosu P16c'de (`site_messaging_settings`,
    `…/communication-settings`); o birleşince buradan okunacak. Şimdilik doküman varsayılanı
    22:00–08:00 (16.2)."""
    return DEFAULT_QUIET_FROM, DEFAULT_QUIET_TO


async def _parties(session: AsyncSession) -> list[PartyRef]:
    rows = await session.execute(
        select(UnitParty, Unit.block_id).join(
            Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id)
        )
    )
    return [
        PartyRef(p.person_id, p.unit_id, block_id, PartyRole(p.role), p.start_date, p.end_date)
        for p, block_id in rows
    ]


async def _debtor_person_ids(session: AsyncSession) -> set[uuid.UUID]:
    rows = await session.scalars(
        select(LedgerAccount.person_id)
        .join(
            AccountBalance,
            and_(
                AccountBalance.account_id == LedgerAccount.id,
                AccountBalance.site_id == LedgerAccount.site_id,
            ),
        )
        .where(AccountBalance.balance > EPSILON)
    )
    return set(rows)


@dataclass(frozen=True, slots=True)
class Published:
    announcement: Announcement
    recipient_count: int
    channels: list[Channel]


@dataclass(frozen=True, slots=True)
class Scheduled:
    announcement: Announcement
    shifted: bool


def _check(data: NewAnnouncement, *, now: datetime, today: date) -> None:
    if data.expires_on is not None and data.expires_on < today:
        raise OperationRuleError(
            "expires_in_past", "Bitiş tarihi geçmiş bir tarih olamaz.", field="expires_on"
        )
    if data.requires_ack and not (
        data.ack_hours is not None and ACK_HOURS_MIN <= data.ack_hours <= ACK_HOURS_MAX
    ):
        raise OperationRuleError(
            "ack_hours", f"Son onay süresi {ACK_HOURS_MIN}–{ACK_HOURS_MAX} saat.", field="ack_hours"
        )
    if data.publish_at is not None and data.publish_at <= now:
        raise OperationRuleError("publish_at", "Yayın zamanı ileride olmalı.", field="publish_at")


async def _check_blocks(session: AsyncSession, data: NewAnnouncement) -> tuple[uuid.UUID, ...]:
    block_ids = tuple(dict.fromkeys(data.block_ids))
    if data.audience is Audience.BLOCKS:
        if not block_ids:
            raise OperationRuleError(
                "audience_blocks_required", "Duyurunun gideceği blokları seçin.",
                field="audience_block_ids",
            )  # fmt: skip
        found = set(await session.scalars(select(Block.id).where(Block.id.in_(block_ids))))
        if found != set(block_ids):
            raise OperationRuleError(
                "block_not_found", "Seçilen blok bu sitede bulunamadı.", field="audience_block_ids"
            )
    return block_ids


def _new(data: NewAnnouncement, block_ids: tuple[uuid.UUID, ...], by: str) -> Announcement:
    return Announcement(
        id=uuid.uuid7(),
        title=" ".join(data.title.split()),
        body=data.body.strip(),
        importance=data.importance.value,
        audience=data.audience.value,
        audience_block_ids=list(block_ids) if data.audience is Audience.BLOCKS else None,
        expires_on=data.expires_on,
        published_by=by,
        is_pinned=data.is_pinned,
        channels=[c.value for c in channels_of(data.channels)],
        requires_ack=data.requires_ack,
        ack_hours=data.ack_hours if data.requires_ack else None,
        sms_fallback=data.sms_fallback if data.requires_ack else False,
    )


async def _deliver(
    session: AsyncSession, announcement: Announcement, *, now: datetime, today: date
) -> Published:
    """Yayın anı: hedef kişiler o gün çözülür, teslim kayıtları yazılır, onay süresi başlar."""
    audience = Audience(announcement.audience)
    debtors = await _debtor_person_ids(session) if audience is Audience.DEBTORS_ONLY else set()
    people = recipients(
        await _parties(session),
        audience=audience,
        today=today,
        block_ids=set(announcement.audience_block_ids or ()),
        debtor_person_ids=debtors,
    )
    announcement.published_at = now
    if announcement.requires_ack and announcement.ack_hours:
        announcement.ack_deadline = now + timedelta(hours=announcement.ack_hours)
    await session.flush()
    channels = channels_of(tuple(Channel(c) for c in announcement.channels))
    scope = current_scope()
    rows = [
        {
            "id": uuid.uuid7(),
            "site_id": scope.site_id if scope else None,
            "announcement_id": announcement.id,
            "person_id": person_id,
            "channel": channel.value,
            "sent_at": now if channel is Channel.IN_APP else None,
        }
        for person_id in sorted(people)
        for channel in channels
    ]
    for start in range(0, len(rows), 2000):  # parametre sınırı: parça parça toplu ekleme
        await session.execute(insert(AnnouncementDelivery), rows[start : start + 2000])
    return Published(announcement, len(people), channels)


async def publish(
    session: AsyncSession, data: NewAnnouncement, *, published_by: str, now: datetime, today: date
) -> Published:
    """Hemen yayın (zamanlama için `create`)."""
    found = await create(
        session, replace(data, publish_at=None), published_by=published_by, now=now, today=today
    )
    if not isinstance(found, Published):  # pragma: no cover — publish_at yok → hemen yayın
        raise TypeError
    return found


async def create(
    session: AsyncSession, data: NewAnnouncement, *, published_by: str, now: datetime, today: date
) -> Published | Scheduled:
    """Yayınla ya da (ileri tarihliyse) zamanla. Sessiz saate düşen zaman kaydırılır; kritik
    önem aşar."""
    _check(data, now=now, today=today)
    block_ids = await _check_blocks(session, data)
    announcement = _new(data, block_ids, published_by)
    session.add(announcement)
    if data.publish_at is None:
        return await _deliver(session, announcement, now=now, today=today)
    at, shifted = data.publish_at, False
    if data.importance is not Importance.CRITICAL:
        quiet_from, quiet_to = await quiet_hours(session)
        at, shifted = shift_out_of_quiet(
            data.publish_at.astimezone(BUSINESS_TZ), quiet_from, quiet_to
        )
    announcement.requested_publish_at = data.publish_at
    announcement.scheduled_for = at
    await session.flush()
    return Scheduled(announcement, shifted)


async def in_quiet_now(session: AsyncSession, now: datetime) -> tuple[bool, time]:
    quiet_from, quiet_to = await quiet_hours(session)
    return in_quiet(now.astimezone(BUSINESS_TZ).time(), quiet_from, quiet_to), quiet_to


def scheduled_query() -> Select[Announcement]:
    return (
        select(Announcement)
        .where(
            Announcement.published_at.is_(None),
            Announcement.scheduled_for.is_not(None),
            Announcement.cancelled_at.is_(None),
        )
        .order_by(Announcement.scheduled_for, Announcement.id)
    )


async def cancel(session: AsyncSession, announcement_id: uuid.UUID, now: datetime) -> Announcement:
    row: Announcement | None = await session.scalar(
        select(Announcement)
        .where(Announcement.id == announcement_id, Announcement.scheduled_for.is_not(None))
        .with_for_update()
    )
    if row is None:
        raise LookupError("Zamanlanmış duyuru bulunamadı.")
    if row.published_at is not None or row.cancelled_at is not None:
        raise OperationRuleError(
            "invalid_status", "Duyuru yayınlanmış ya da iptal edilmiş.", conflict=True
        )
    row.cancelled_at = now
    await session.flush()
    return row


async def publish_due(session: AsyncSession, *, now: datetime) -> int:
    """Vakti gelen zamanlanmış duyuruları yayınlar (dakikalık iş)."""
    rows = list(
        await session.scalars(
            scheduled_query()
            .where(Announcement.scheduled_for <= now)
            .with_for_update(skip_locked=True)
        )
    )
    today = now.astimezone(BUSINESS_TZ).date()
    for row in rows:
        await _deliver(session, row, now=now, today=today)
    return len(rows)


def visible_query(
    *, show_all: bool, person_id: uuid.UUID | None, today: date
) -> Select[Announcement]:
    """Yayınlayan hepsini görür; sakin yalnız kendisine teslim edilenleri; diğer personel
    yürürlükteki tüm duyuruları. Sabitlenmiş önce, sonra yayın tarihi (docs/06 §2.11)."""
    query = select(Announcement).where(Announcement.published_at.is_not(None))
    if not show_all:
        query = query.where(
            or_(Announcement.expires_on.is_(None), Announcement.expires_on >= today)
        )
        if person_id is not None:
            delivered = select(AnnouncementDelivery.announcement_id).where(
                AnnouncementDelivery.person_id == person_id,
                AnnouncementDelivery.channel == Channel.IN_APP.value,
            )
            query = query.where(Announcement.id.in_(delivered))
    return query.order_by(
        Announcement.is_pinned.desc(), Announcement.published_at.desc(), Announcement.id.desc()
    )


async def read_times(
    session: AsyncSession, person_id: uuid.UUID, announcement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, datetime | None]:
    return {
        aid: d.read_at
        for aid, d in (await my_deliveries(session, person_id, announcement_ids)).items()
    }


async def delivery_stats(
    session: AsyncSession, announcement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int, int]]:
    """Duyuru başına (hedef kişi, okuyan, onaylayan) — uygulama içi teslimden, veritabanında."""
    if not announcement_ids:
        return {}
    rows = await session.execute(
        select(
            AnnouncementDelivery.announcement_id,
            func.count(),
            func.count(AnnouncementDelivery.read_at),
            func.count(AnnouncementDelivery.acked_at),
        )
        .where(
            AnnouncementDelivery.announcement_id.in_(announcement_ids),
            AnnouncementDelivery.channel == Channel.IN_APP.value,
        )
        .group_by(AnnouncementDelivery.announcement_id)
    )
    return {aid: (total, read, acked) for aid, total, read, acked in rows}


async def get(session: AsyncSession, announcement_id: uuid.UUID) -> Announcement | None:
    row: Announcement | None = await session.scalar(
        select(Announcement).where(Announcement.id == announcement_id)
    )
    return row


async def mark_read(
    session: AsyncSession, announcement_id: uuid.UUID, person_id: uuid.UUID, now: datetime
) -> datetime | None:
    """Okundu işaretler (ilk okuma anı korunur). Kişiye teslim edilmemişse `None`."""
    await session.execute(
        update(AnnouncementDelivery)
        .where(
            AnnouncementDelivery.announcement_id == announcement_id,
            AnnouncementDelivery.person_id == person_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
            AnnouncementDelivery.read_at.is_(None),
        )
        .values(read_at=now)
    )
    value: datetime | None = await session.scalar(
        select(AnnouncementDelivery.read_at).where(
            AnnouncementDelivery.announcement_id == announcement_id,
            AnnouncementDelivery.person_id == person_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
        )
    )
    return value


# --- Okuma onayı (16.3) ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MyDelivery:
    read_at: datetime | None
    acked_at: datetime | None


async def my_deliveries(
    session: AsyncSession, person_id: uuid.UUID, announcement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, MyDelivery]:
    if not announcement_ids:
        return {}
    rows = await session.execute(
        select(
            AnnouncementDelivery.announcement_id,
            AnnouncementDelivery.read_at,
            AnnouncementDelivery.acked_at,
        ).where(
            AnnouncementDelivery.person_id == person_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
            AnnouncementDelivery.announcement_id.in_(announcement_ids),
        )
    )
    return {aid: MyDelivery(read, acked) for aid, read, acked in rows}


async def acknowledge(
    session: AsyncSession, announcement: Announcement, person_id: uuid.UUID, now: datetime
) -> datetime:
    """ "Okudum, anladım": onay aynı zamanda okundu sayılır. Okuma onayı istemiyorsa
    `LookupError`, kişiye teslim edilmemişse `PermissionError`, zaten onaylıysa
    `OperationRuleError(already)`."""
    if not announcement.requires_ack:
        raise LookupError("Bu duyuru okuma onayı istemiyor.")
    row: AnnouncementDelivery | None = await session.scalar(
        select(AnnouncementDelivery)
        .where(
            AnnouncementDelivery.announcement_id == announcement.id,
            AnnouncementDelivery.person_id == person_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
        )
        .with_for_update()
    )
    if row is None:
        raise PermissionError("Okuma onayı yalnız hedef kitledeki sakinden alınır.")
    if row.acked_at is not None:
        raise OperationRuleError("already", "Okuma onayınız zaten kayıtlı.", conflict=True)
    row.acked_at = now
    if row.read_at is None:
        row.read_at = now
    await session.flush()
    return now


def pending_acks_query(person_id: uuid.UUID, today: date) -> Select[Announcement]:
    """Kişinin onaylamadığı, yürürlükteki okuma onaylı duyurular (ana sayfada sabit kalır)."""
    pending = select(AnnouncementDelivery.announcement_id).where(
        AnnouncementDelivery.person_id == person_id,
        AnnouncementDelivery.channel == Channel.IN_APP.value,
        AnnouncementDelivery.acked_at.is_(None),
    )
    return (
        visible_query(show_all=False, person_id=person_id, today=today)
        .where(Announcement.requires_ack, Announcement.id.in_(pending))
        .limit(50)
    )


@dataclass(frozen=True, slots=True)
class ReportRow:
    person_id: uuid.UUID
    name: str
    units: list[str]
    delivered_at: datetime | None
    read_at: datetime | None
    acked_at: datetime | None


async def report_rows(
    session: AsyncSession, announcement: Announcement, today: date
) -> list[ReportRow]:
    """Alıcı listesi (kişi bazında teslim / açılma / onay zamanı) — uygulama içi teslimden."""
    rows = (
        await session.execute(
            select(AnnouncementDelivery, Person.first_name, Person.last_name)
            .join(
                Person,
                and_(
                    Person.id == AnnouncementDelivery.person_id,
                    Person.site_id == AnnouncementDelivery.site_id,
                ),
            )
            .where(
                AnnouncementDelivery.announcement_id == announcement.id,
                AnnouncementDelivery.channel == Channel.IN_APP.value,
            )
            .order_by(Person.last_name, Person.first_name, Person.id)
        )
    ).all()
    person_ids = [d.person_id for d, _, _ in rows]
    units: dict[uuid.UUID, list[str]] = {}
    if person_ids:
        parties = await session.execute(
            select(UnitParty.person_id, Unit.number, Block.name)
            .join(Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id))
            .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
            .where(
                UnitParty.person_id.in_(person_ids),
                UnitParty.role.in_([r.value for r in RESIDENT_PARTY_ROLES]),
                UnitParty.start_date <= today,
                or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
            )
            .order_by(Block.name, Unit.number)
        )
        for person_id, number, block in parties:
            name = f"{block}-{number}"
            if name not in units.setdefault(person_id, []):
                units[person_id].append(name)
    return [
        ReportRow(
            d.person_id,
            f"{first} {last}",
            units.get(d.person_id, []),
            d.sent_at,
            d.read_at,
            d.acked_at,
        )
        for d, first, last in rows
    ]


async def user_ids_of(session: AsyncSession, person_ids: Sequence[uuid.UUID]) -> list[uuid.UUID]:
    """Kişilerin bu sitedeki etkin giriş hesapları."""
    if not person_ids:
        return []
    return list(
        await session.scalars(
            select(SiteMembership.user_id).where(
                SiteMembership.person_id.in_(person_ids), SiteMembership.is_active
            )
        )
    )


async def run_ack_fallbacks(session: AsyncSession, *, now: datetime) -> int:
    """Son onay süresi dolan, SMS yedeği seçili duyurularda onaylamayanlara **bir kez** SMS."""
    rows = list(
        await session.scalars(
            select(Announcement)
            .where(
                Announcement.requires_ack,
                Announcement.sms_fallback,
                Announcement.sms_fallback_ran_at.is_(None),
                Announcement.ack_deadline <= now,
            )
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        pending = list(
            await session.scalars(
                select(AnnouncementDelivery.person_id).where(
                    AnnouncementDelivery.announcement_id == row.id,
                    AnnouncementDelivery.channel == Channel.IN_APP.value,
                    AnnouncementDelivery.acked_at.is_(None),
                )
            )
        )
        result = await outbound.send_sms(
            session,
            await user_ids_of(session, pending),
            "ack_reminder",
            f"Okuma onayı bekleyen duyuru: {row.title}",
        )
        row.sms_fallback_ran_at = now
        row.sms_fallback_count = len(pending)
        row.sms_fallback_sent = result.sent
    await session.flush()
    return len(rows)


# --- Zamanlanmış iş -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobResult:
    site_id: uuid.UUID
    published: int
    fallbacks: int


async def run_jobs(factory: async_sessionmaker[AsyncSession], *, now: datetime) -> list[JobResult]:
    """Dakikalık: zamanı gelen duyuruyu yayınla, süresi dolan okuma onayında SMS yedeği.
    Site başına ayrı transaction; bir sitenin hatası diğerlerini durdurmaz."""
    set_actor(Actor(None, "Duyuru zamanlayıcısı", None))
    with all_sites_scope():
        async with factory() as session:
            site_ids = list(await session.scalars(select(Site.id).order_by(Site.id)))
    results = []
    for site_id in site_ids:
        try:
            with site_scope(site_id):
                async with factory() as session, session.begin():
                    published = await publish_due(session, now=now)
                    fallbacks = await run_ack_fallbacks(session, now=now)
            if published or fallbacks:
                results.append(JobResult(site_id, published, fallbacks))
        except Exception:
            logger.exception("Duyuru zamanlayıcısı başarısız (site %s)", site_id)
    return results
