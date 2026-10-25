"""Nöbet defteri ve vardiya devri (servis isteği 45; Yazılım Dokümanı GUV-06).

Kayıt, ek not ve devir onayı **yalnız eklenir** (veritabanı tetikleyicisi değişikliği ve silmeyi
reddeder). Devri yazan kendi devrini onaylayamaz; onay bir kez (tekil kısıt).
Olay kaydı (`incidents`, servis isteği 09) ayrıdır (K27).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.gatehouse import ShiftLogKind, check_addendum, check_shift_entry
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import ShiftHandoverAck, ShiftLogAddendum, ShiftLogEntry
from site_yonetim.services.charging import BUSINESS_TZ

DAY_LIMIT = 500


@dataclass(frozen=True, slots=True)
class Author:
    user_id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class EntryView:
    entry: ShiftLogEntry
    addenda: list[ShiftLogAddendum]
    ack: ShiftHandoverAck | None


async def write(
    session: AsyncSession,
    *,
    kind: str | None,
    text: str | None,
    gate_id: uuid.UUID | None,
    author: Author,
    now: datetime,
) -> ShiftLogEntry:
    clean_kind, clean_text = check_shift_entry(kind, text)
    entry = ShiftLogEntry(
        kind=clean_kind.value,
        text=clean_text,
        gate_id=gate_id,
        author_user_id=author.user_id,
        author_name=author.name,
        at=now,
        log_date=now.astimezone(BUSINESS_TZ).date(),
    )
    session.add(entry)
    await session.flush()
    return entry


async def get(session: AsyncSession, entry_id: uuid.UUID) -> ShiftLogEntry | None:
    row: ShiftLogEntry | None = await session.scalar(
        select(ShiftLogEntry).where(ShiftLogEntry.id == entry_id)
    )
    return row


async def add_addendum(
    session: AsyncSession, entry: ShiftLogEntry, *, text: str | None, author: Author, now: datetime
) -> ShiftLogAddendum:
    row = ShiftLogAddendum(
        entry_id=entry.id,
        text=check_addendum(text),
        author_user_id=author.user_id,
        author_name=author.name,
        at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def acknowledge(
    session: AsyncSession, entry: ShiftLogEntry, *, author: Author, now: datetime
) -> ShiftHandoverAck:
    if entry.kind != ShiftLogKind.HANDOVER.value:
        raise OperationRuleError("not_found", "Devir notu bulunamadı.")
    existing = await session.scalar(
        select(ShiftHandoverAck.id).where(ShiftHandoverAck.entry_id == entry.id)
    )
    if existing is not None:
        raise OperationRuleError("already_acknowledged", "Devir zaten onaylanmış.", conflict=True)
    if entry.author_user_id == author.user_id:
        raise OperationRuleError(
            "same_person",
            "Devri yazan kişi kendi devrini onaylayamaz; sonraki vardiya görevlisi onaylar.",
            conflict=True,
        )
    ack = ShiftHandoverAck(entry_id=entry.id, user_id=author.user_id, user_name=author.name, at=now)
    try:
        async with session.begin_nested():
            session.add(ack)
            await session.flush()
    except IntegrityError as exc:  # eşzamanlı ikinci onay
        raise OperationRuleError(
            "already_acknowledged", "Devir zaten onaylanmış.", conflict=True
        ) from exc
    return ack


async def views(session: AsyncSession, entries: list[ShiftLogEntry]) -> list[EntryView]:
    ids = [e.id for e in entries]
    addenda: dict[uuid.UUID, list[ShiftLogAddendum]] = defaultdict(list)
    acks: dict[uuid.UUID, ShiftHandoverAck] = {}
    if ids:
        for a in await session.scalars(
            select(ShiftLogAddendum)
            .where(ShiftLogAddendum.entry_id.in_(ids))
            .order_by(ShiftLogAddendum.at, ShiftLogAddendum.id)
        ):
            addenda[a.entry_id].append(a)
        for k in await session.scalars(
            select(ShiftHandoverAck).where(ShiftHandoverAck.entry_id.in_(ids))
        ):
            acks[k.entry_id] = k
    return [EntryView(e, addenda[e.id], acks.get(e.id)) for e in entries]


async def day(session: AsyncSession, on: date) -> list[EntryView]:
    entries = list(
        await session.scalars(
            select(ShiftLogEntry)
            .where(ShiftLogEntry.log_date == on)
            .order_by(ShiftLogEntry.at.desc(), ShiftLogEntry.id.desc())
            .limit(DAY_LIMIT)
        )
    )
    return await views(session, entries)
