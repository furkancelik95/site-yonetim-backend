"""Toplantılar ve genel kurul — frontend servis isteği 14. Açık site kapsamında; transaction'ı
çağıran yönetir.

- Gündem planlarken girilir (1–30 madde); bu sürümde değiştirilmez (yanlışsa iptal + yeniden).
- Kararlar **tek seferlik**: her madde için sonuç zorunlu, "bilgi verildi" dışında karar metni
  zorunlu. Kayıttan sonra değişiklik yok (yasal kayıt); toplantı `held` olur.
- Yeter sayı hesabı yok: KMK m.30 kişi ve arsa payı çoğunluğuna bakar ve konuya göre değişir —
  hukukçuyla kurallar netleşince. Çağrı süresi uyarısı (15 gün, KMK m.29) ekranda; sunucu
  engellemez.
- Sistemdeki tutanak noter onaylı karar defterinin yerine geçmez.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import current_scope
from site_yonetim.domain.management import (
    AGENDA_MAX,
    AGENDA_MIN,
    AgendaResult,
    MeetingKind,
    MeetingStatus,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import Meeting, MeetingAgendaItem

_NUMBER_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


async def create(
    session: AsyncSession,
    *,
    kind: str | None,
    title: str | None,
    scheduled_at: datetime | None,
    location: str | None,
    agenda: list[str] | None,
    created_by: str,
) -> Meeting:
    errors: dict[str, str] = {}
    if kind not in {k.value for k in MeetingKind}:
        errors["kind"] = "Toplantı türünü seçin."
    name = _clean(title)
    if not 3 <= len(name) <= 200:
        errors["title"] = "Toplantıya bir başlık verin (3–200 karakter)."
    if scheduled_at is None:
        errors["scheduled_at"] = "Tarih ve saati seçin."
    place = _clean(location)
    if not 1 <= len(place) <= 200:
        errors["location"] = "Toplantı yerini yazın."
    items = [_clean(a) for a in agenda or []]
    if not AGENDA_MIN <= len(items) <= AGENDA_MAX or not all(1 <= len(i) <= 300 for i in items):
        errors["agenda"] = f"{AGENDA_MIN} ile {AGENDA_MAX} arasında gündem maddesi yazın."
    if errors:
        raise FormFieldError(errors)
    scope = current_scope()
    await session.execute(_NUMBER_LOCK, {"key": f"meetings:{scope.site_id if scope else ''}"})
    number = (await session.scalar(select(func.max(Meeting.number)))) or 0
    meeting = Meeting(
        id=uuid.uuid7(),
        number=number + 1,
        kind=kind,
        title=name,
        scheduled_at=scheduled_at,
        location=place,
        status=MeetingStatus.PLANNED.value,
        created_by_name=created_by,
    )
    session.add(meeting)
    await session.flush()
    session.add_all(
        MeetingAgendaItem(meeting_id=meeting.id, order=order, title=item)
        for order, item in enumerate(items, start=1)
    )
    await session.flush()
    return meeting


async def get(
    session: AsyncSession, meeting_id: uuid.UUID, *, lock: bool = False
) -> Meeting | None:
    query = select(Meeting).where(Meeting.id == meeting_id)
    found: Meeting | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def agenda(
    session: AsyncSession, meeting_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[MeetingAgendaItem]]:
    found: dict[uuid.UUID, list[MeetingAgendaItem]] = {i: [] for i in meeting_ids}
    if not meeting_ids:
        return found
    rows = await session.scalars(
        select(MeetingAgendaItem)
        .where(MeetingAgendaItem.meeting_id.in_(meeting_ids))
        .order_by(MeetingAgendaItem.order)
    )
    for item in rows:
        found[item.meeting_id].append(item)
    return found


def list_query(status: MeetingStatus | None) -> Select[Meeting]:
    query = select(Meeting)
    if status is not None:
        query = query.where(Meeting.status == status.value)
    return query.order_by(Meeting.scheduled_at.desc(), Meeting.number.desc())


def _not_planned(message: str) -> OperationRuleError:
    return OperationRuleError("not_planned", message, conflict=True)


@dataclass(frozen=True, slots=True)
class Decision:
    id: uuid.UUID
    result: str | None
    decision: str | None
    votes_for: int | None = None
    votes_against: int | None = None
    votes_abstain: int | None = None


async def decide(
    session: AsyncSession,
    meeting: Meeting,
    *,
    attendance_note: str | None,
    decisions: list[Decision],
    now: datetime,
) -> Meeting:
    if meeting.status != MeetingStatus.PLANNED.value:
        raise _not_planned("Kararlar yalnız planlanan toplantıya girilir.")
    items = (await agenda(session, [meeting.id]))[meeting.id]
    given = {d.id: d for d in decisions}
    errors: dict[str, str] = {}
    note = _clean(attendance_note)
    if not 1 <= len(note) <= 500:
        errors["attendance_note"] = (
            "Katılımı yazın (ör. 48 bölümden 31'i katıldı ya da temsil edildi)."
        )
    for item in items:
        d = given.get(item.id)
        key = f"items.{item.order}"
        if d is None or d.result not in {r.value for r in AgendaResult}:
            errors[key] = f"{item.order}. maddenin sonucunu seçin."
            continue
        text_ = _clean(d.decision)
        if d.result != AgendaResult.INFO.value and not text_:
            errors[key] = f"{item.order}. maddenin karar metnini yazın."
            continue
        votes = (d.votes_for, d.votes_against, d.votes_abstain)
        if any(v is not None and v < 0 for v in votes):
            errors[key] = f"{item.order}. maddenin oy sayıları sıfır ya da daha büyük olmalı."
    if errors:
        raise FormFieldError(errors)
    for item in items:
        d = given[item.id]
        item.result = d.result
        item.decision = _clean(d.decision)[:2000] or None
        item.votes_for, item.votes_against, item.votes_abstain = (
            d.votes_for, d.votes_against, d.votes_abstain
        )  # fmt: skip
    meeting.attendance_note = note
    meeting.status = MeetingStatus.HELD.value
    meeting.held_at = now
    await session.flush()
    return meeting


async def cancel(session: AsyncSession, meeting: Meeting, *, reason: str | None) -> Meeting:
    if meeting.status != MeetingStatus.PLANNED.value:
        raise _not_planned("Yalnız planlanan toplantı iptal edilir.")
    text_ = _clean(reason)
    if not 3 <= len(text_) <= 500:
        raise FormFieldError({"reason": "Gerekçe yazın."})
    meeting.status = MeetingStatus.CANCELLED.value
    meeting.cancel_reason = text_
    await session.flush()
    return meeting
