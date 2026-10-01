"""Duyuru servisleri — docs/03 §9, docs/06 §2.11. Açık site kapsamında; commit çağırana ait.

Yayınlama, hedef kitledeki her kişi ve kanal için bir teslim kaydı yazar. Gerçek gönderim yok
(docs/12 K5): uygulama içi teslim anında `sent_at` alır, diğer kanallar bekler (arka plan işi
gelince gönderilecek).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
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
    Unit,
    UnitParty,
)


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


async def publish(
    session: AsyncSession, data: NewAnnouncement, *, published_by: str, now: datetime, today: date
) -> Published:
    if data.expires_on is not None and data.expires_on < today:
        raise OperationRuleError(
            "expires_in_past", "Bitiş tarihi geçmiş bir tarih olamaz.", field="expires_on"
        )
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
    debtors = await _debtor_person_ids(session) if data.audience is Audience.DEBTORS_ONLY else set()
    people = recipients(
        await _parties(session),
        audience=data.audience,
        today=today,
        block_ids=set(block_ids),
        debtor_person_ids=debtors,
    )
    announcement = Announcement(
        id=uuid.uuid7(),
        title=" ".join(data.title.split()),
        body=data.body.strip(),
        importance=data.importance.value,
        audience=data.audience.value,
        audience_block_ids=list(block_ids) if data.audience is Audience.BLOCKS else None,
        published_at=now,
        expires_on=data.expires_on,
        published_by=published_by,
        is_pinned=data.is_pinned,
    )
    session.add(announcement)
    await session.flush()
    channels = channels_of(data.channels)
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
    if not announcement_ids:
        return {}
    rows = await session.execute(
        select(AnnouncementDelivery.announcement_id, AnnouncementDelivery.read_at).where(
            AnnouncementDelivery.person_id == person_id,
            AnnouncementDelivery.channel == Channel.IN_APP.value,
            AnnouncementDelivery.announcement_id.in_(announcement_ids),
        )
    )
    return dict(rows.all())


async def delivery_stats(
    session: AsyncSession, announcement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """Duyuru başına (hedef kişi, okuyan kişi) — uygulama içi teslimden, veritabanında."""
    if not announcement_ids:
        return {}
    rows = await session.execute(
        select(
            AnnouncementDelivery.announcement_id,
            func.count(),
            func.count(AnnouncementDelivery.read_at),
        )
        .where(
            AnnouncementDelivery.announcement_id.in_(announcement_ids),
            AnnouncementDelivery.channel == Channel.IN_APP.value,
        )
        .group_by(AnnouncementDelivery.announcement_id)
    )
    return {aid: (total, read) for aid, total, read in rows}


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
