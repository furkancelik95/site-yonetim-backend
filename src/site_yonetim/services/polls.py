"""Anket — frontend servis isteği 15. Açık site kapsamında; transaction'ı çağıran yönetir.

- **Bir bağımsız bölüm = bir oy**: oy bölüme yazılır, değiştirilemez; `(anket, bölüm)` benzersiz
  kısıtı eşzamanlı iki isteği de engeller. `audience` hangi rolün vereceğini belirler.
- `ends_on` günü dahil açık; ertesi gün kendiliğinden kapanır. Erken kapatılabilir.
- **Gizli oy**: kimin neye oy verdiği tutulmaz, oy tablosu denetim kaydına yazılmaz; personel
  yalnız toplamları görür. Sakin, oy vermeden ve anket açıkken ara sonucu göremez.
- Sonuç danışma niteliğindedir; genel kurul kararı yerine geçmez.
"""

import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.management import (
    OPTIONS_MAX,
    OPTIONS_MIN,
    PollAudience,
    PollStatus,
    poll_status,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import Poll, PollOption, PollVote, UnitParty

RESIDENT_HISTORY = timedelta(days=30)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def status_of(poll: Poll, today: date) -> PollStatus:
    return poll_status(poll.ends_on, poll.closed_at is not None, today)


async def create(
    session: AsyncSession,
    *,
    question: str | None,
    description: str | None,
    options: list[str] | None,
    audience: str | None,
    ends_on: date | None,
    today: date,
    created_by: str,
) -> Poll:
    errors: dict[str, str] = {}
    text_ = _clean(question)
    if not 3 <= len(text_) <= 300:
        errors["question"] = "Soruyu yazın (3–300 karakter)."
    labels = [_clean(o) for o in options or [] if _clean(o)]
    if not OPTIONS_MIN <= len(labels) <= OPTIONS_MAX or any(len(x) > 120 for x in labels):
        errors["options"] = f"{OPTIONS_MIN} ile {OPTIONS_MAX} arasında seçenek yazın."
    elif len({ascii_fold(x) for x in labels}) != len(labels):
        errors["options"] = "Aynı seçenek iki kez yazılmış."
    if audience not in {a.value for a in PollAudience}:
        errors["audience"] = "Kimin oy vereceğini seçin."
    if ends_on is None or ends_on < today:
        errors["ends_on"] = "Bitiş tarihi bugünden önce olamaz."
    if errors:
        raise FormFieldError(errors)
    poll = Poll(
        id=uuid.uuid7(),
        question=text_,
        description=_clean(description)[:1000] or None,
        audience=audience,
        ends_on=ends_on,
        created_by_name=created_by,
    )
    session.add(poll)
    await session.flush()
    session.add_all(
        PollOption(poll_id=poll.id, order=order, label=label)
        for order, label in enumerate(labels, start=1)
    )
    await session.flush()
    return poll


async def get(session: AsyncSession, poll_id: uuid.UUID, *, lock: bool = False) -> Poll | None:
    query = select(Poll).where(Poll.id == poll_id)
    found: Poll | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def close(session: AsyncSession, poll: Poll, *, today: date, now: datetime) -> Poll:
    if status_of(poll, today) is PollStatus.CLOSED:
        raise OperationRuleError("already_closed", "Anket zaten kapandı.", conflict=True)
    poll.closed_at = now
    await session.flush()
    return poll


def _open(today: date) -> list[object]:
    return [Poll.closed_at.is_(None), Poll.ends_on >= today]


def list_query(status: PollStatus | None, today: date) -> Select[Poll]:
    query = select(Poll)
    if status is PollStatus.OPEN:
        query = query.where(*_open(today))  # type: ignore[arg-type]
    elif status is PollStatus.CLOSED:
        query = query.where(or_(Poll.closed_at.is_not(None), Poll.ends_on < today))
    return query.order_by(Poll.created_at.desc(), Poll.id.desc())


def resident_query(today: date, now: datetime) -> Select[Poll]:
    """Açık anketler + son 30 günde kapananlar."""
    since = today - RESIDENT_HISTORY
    recently_closed = or_(
        and_(Poll.closed_at.is_not(None), Poll.closed_at >= now - RESIDENT_HISTORY),
        and_(Poll.closed_at.is_(None), Poll.ends_on >= since),
    )
    return select(Poll).where(recently_closed).order_by(Poll.ends_on, Poll.created_at)


async def options(
    session: AsyncSession, poll_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PollOption]]:
    found: dict[uuid.UUID, list[PollOption]] = {i: [] for i in poll_ids}
    if poll_ids:
        for option in await session.scalars(
            select(PollOption).where(PollOption.poll_id.in_(poll_ids)).order_by(PollOption.order)
        ):
            found[option.poll_id].append(option)
    return found


async def counts(session: AsyncSession, poll_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Seçenek başına oy sayısı (seçenek kimliği → sayı) — yalnız toplam, kim değil."""
    if not poll_ids:
        return {}
    rows = await session.execute(
        select(PollVote.option_id, func.count())
        .where(PollVote.poll_id.in_(poll_ids))
        .group_by(PollVote.option_id)
    )
    return dict(rows.all())


# --- Sakin --------------------------------------------------------------------------


_ROLES = {
    PollAudience.ALL: {PartyRole.OWNER.value, PartyRole.TENANT.value, PartyRole.RESIDENT.value},
    PollAudience.OWNERS: {PartyRole.OWNER.value},
    PollAudience.TENANTS: {PartyRole.TENANT.value, PartyRole.RESIDENT.value},
}


async def person_units(
    session: AsyncSession, person_id: uuid.UUID, today: date
) -> dict[uuid.UUID, set[str]]:
    """Kişinin bugün bağlı olduğu bölümler ve oradaki rolleri."""
    rows = await session.execute(
        select(UnitParty.unit_id, UnitParty.role).where(
            UnitParty.person_id == person_id,
            UnitParty.role.in_(list(_ROLES[PollAudience.ALL])),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    found: dict[uuid.UUID, set[str]] = {}
    for unit_id, role in rows:
        found.setdefault(unit_id, set()).add(role)
    return found


def eligible_units(poll: Poll, units: dict[uuid.UUID, set[str]]) -> list[uuid.UUID]:
    allowed = _ROLES[PollAudience(poll.audience)]
    return [unit for unit, roles in units.items() if roles & allowed]


async def unit_votes(
    session: AsyncSession, poll_ids: list[uuid.UUID], unit_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID]:
    """(anket, bölüm) → seçenek — yalnız sakinin kendi bölümleri için."""
    if not poll_ids or not unit_ids:
        return {}
    rows = await session.execute(
        select(PollVote.poll_id, PollVote.unit_id, PollVote.option_id).where(
            PollVote.poll_id.in_(poll_ids), PollVote.unit_id.in_(unit_ids)
        )
    )
    return {(p, u): o for p, u, o in rows}


async def vote(
    session: AsyncSession,
    poll: Poll,
    *,
    person_id: uuid.UUID,
    unit_id: uuid.UUID,
    option_id: uuid.UUID,
    today: date,
) -> PollVote:
    if status_of(poll, today) is PollStatus.CLOSED:
        raise OperationRuleError("poll_closed", "Anket kapandı; oy verilemez.", conflict=True)
    units = await person_units(session, person_id, today)
    if unit_id not in units:
        raise LookupError("Bu bölüm adına oy veremezsiniz.")
    if unit_id not in eligible_units(poll, units):
        raise PermissionError("Bu anket bölümünüzdeki rolünüze açık değil.")
    option = await session.scalar(
        select(PollOption.id).where(PollOption.id == option_id, PollOption.poll_id == poll.id)
    )
    if option is None:
        raise FormFieldError({"option_id": "Bir seçenek işaretleyin."})
    ballot = PollVote(poll_id=poll.id, option_id=option_id, unit_id=unit_id)
    try:
        async with session.begin_nested():
            session.add(ballot)
            await session.flush()
    except IntegrityError as exc:
        raise OperationRuleError(
            "already_voted", "Bu bölüm adına zaten oy verildi.", conflict=True
        ) from exc
    return ballot
