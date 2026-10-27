"""Anket — frontend servis isteği 15, Yazılım Dokümanı ILT-03, PM kararı K31. Açık site
kapsamında; transaction'ı çağıran yönetir.

- **Sorular:** bir ankette 1–20 soru; tipler tek seçim, çoklu seçim, 1–5 puan, serbest metin.
  Eski tek soruluk biçim (`question` + `options`) geriye uyumlu: tek "tek seçim" sorusu olur.
- **Gizlilik anket bazında (K31):** `is_anonymous` → kimin yanıtladığı tutulmaz (personel yalnız
  toplamları görür); isimli ankette yanıt veren kişi tutulur (kişisel veri) ve isimli dökümü
  yalnız `polls.manage` görür. `one_per_unit` → bölüm başına tek yanıt (`(anket, bölüm)`
  benzersiz; eşzamanlı iki istekte de); değilse kişi başına tek yanıt.
- Yanıt değiştirilemez; yanıt tabloları denetim kaydına yazılmaz. `audience` hangi rolün,
  `block_ids`
  hangi blokların yanıt vereceğini belirler.
- `ends_on` günü dahil açık; ertesi gün kendiliğinden kapanır. Erken kapatılabilir.
- Sakin, yanıt vermeden ve anket açıkken ara sonucu göremez.
- Sonuç danışma niteliğindedir; **resmî oylama yerine geçmez** (`POLL_NOTICE`).
"""

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.management import (
    OPTIONS_MAX,
    OPTIONS_MIN,
    QUESTIONS_MAX,
    TEXT_ANSWER_MAX,
    PollAudience,
    PollQuestionKind,
    PollStatus,
    poll_status,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.text import ascii_fold
from site_yonetim.models import (
    Block,
    Person,
    Poll,
    PollAnswer,
    PollBallot,
    PollOption,
    PollParticipant,
    PollQuestion,
    SiteMembership,
    Unit,
    UnitParty,
)
from site_yonetim.services import notifications as notify_svc

RESIDENT_HISTORY = timedelta(days=30)
CHOICE_KINDS = {PollQuestionKind.SINGLE.value, PollQuestionKind.MULTIPLE.value}


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def status_of(poll: Poll, today: date) -> PollStatus:
    return poll_status(poll.ends_on, poll.closed_at is not None, today)


@dataclass(frozen=True, slots=True)
class QuestionInput:
    text: str | None
    kind: str | None
    options: list[str] = field(default_factory=list)
    required: bool = True


def _check_options(raw: list[str], errors: dict[str, str], key: str) -> list[str]:
    labels = [_clean(o) for o in raw if _clean(o)]
    if not OPTIONS_MIN <= len(labels) <= OPTIONS_MAX or any(len(x) > 120 for x in labels):
        errors[key] = f"{OPTIONS_MIN} ile {OPTIONS_MAX} arasında seçenek yazın."
    elif len({ascii_fold(x) for x in labels}) != len(labels):
        errors[key] = "Aynı seçenek iki kez yazılmış."
    return labels


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
    questions: list[QuestionInput] | None = None,
    is_anonymous: bool = True,
    one_per_unit: bool = True,
    block_ids: list[uuid.UUID] | None = None,
) -> Poll:
    errors: dict[str, str] = {}
    title = _clean(question)
    if not 3 <= len(title) <= 300:
        errors["question"] = "Soruyu yazın (3–300 karakter)."
    checked: list[tuple[str, str, list[str], bool]] = []
    if not questions:  # eski biçim: tek "tek seçim" sorusu
        labels = _check_options(options or [], errors, "options")
        checked.append((title, PollQuestionKind.SINGLE.value, labels, True))
    elif len(questions) > QUESTIONS_MAX:
        errors["questions"] = f"En fazla {QUESTIONS_MAX} soru."
    else:
        for i, q in enumerate(questions):
            text = _clean(q.text)
            if not 3 <= len(text) <= 300:
                errors[f"questions.{i}.text"] = "Soruyu yazın (3–300 karakter)."
            if q.kind not in {k.value for k in PollQuestionKind}:
                errors[f"questions.{i}.kind"] = "Soru tipini seçin."
                continue
            labels = []
            if q.kind in CHOICE_KINDS:
                labels = _check_options(q.options, errors, f"questions.{i}.options")
            checked.append((text, q.kind, labels, q.required))
    if audience not in {a.value for a in PollAudience}:
        errors["audience"] = "Kimin oy vereceğini seçin."
    if ends_on is None or ends_on < today:
        errors["ends_on"] = "Bitiş tarihi bugünden önce olamaz."
    blocks = list(dict.fromkeys(block_ids or []))
    if blocks:
        found = set(await session.scalars(select(Block.id).where(Block.id.in_(blocks))))
        if found != set(blocks):
            errors["block_ids"] = "Seçilen blok bu sitede bulunamadı."
    if errors:
        raise FormFieldError(errors)
    poll = Poll(
        id=uuid.uuid7(),
        question=title,
        description=_clean(description)[:1000] or None,
        audience=audience,
        ends_on=ends_on,
        created_by_name=created_by,
        is_anonymous=is_anonymous,
        one_per_unit=one_per_unit,
        block_ids=blocks or None,
    )
    session.add(poll)
    await session.flush()
    for order, (text, kind, labels, required) in enumerate(checked, start=1):
        row = PollQuestion(
            id=uuid.uuid7(), poll_id=poll.id, order=order, text=text, kind=kind, required=required
        )
        session.add(row)
        await session.flush()
        session.add_all(
            PollOption(poll_id=poll.id, question_id=row.id, order=n, label=label)
            for n, label in enumerate(labels, start=1)
        )
    await session.flush()
    return poll


async def notify_audience(session: AsyncSession, poll: Poll, today: date) -> int:
    """Hedef kitleye uygulama içi bildirim (ILT-03 adım 2)."""
    roles = _ROLES[PollAudience(poll.audience)]
    persons = select(UnitParty.person_id).where(
        UnitParty.role.in_(roles),
        UnitParty.start_date <= today,
        or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
    )
    if poll.block_ids:
        persons = persons.join(
            Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id)
        ).where(Unit.block_id.in_(poll.block_ids))
    users = await session.scalars(
        select(SiteMembership.user_id).where(
            SiteMembership.person_id.in_(persons), SiteMembership.is_active
        )
    )
    return await notify_svc.notify(
        session,
        set(users),
        notify_svc.Message(
            category=NotificationCategory.ANNOUNCEMENT,
            kind="poll.opened",
            title=f"Yeni anket: {poll.question}"[:120],
            body=f"Son gün {poll.ends_on:%d.%m.%Y}. Anket danışma niteliğindedir.",
            link_entity="polls",
            link_id=poll.id,
        ),
        dedupe_key=f"poll:{poll.id}",
    )


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


# --- Sorular ve sonuçlar (toplamlar veritabanında) ------------------------------------------


@dataclass(slots=True)
class QuestionResult:
    question: PollQuestion
    options: list[PollOption]
    option_votes: dict[uuid.UUID, int]
    ratings: Counter[int]
    texts: list[str]
    answered: int  # bu soruyu yanıtlayan pusula


@dataclass(slots=True)
class PollResult:
    questions: list[QuestionResult]
    ballots: int


async def results(session: AsyncSession, poll_ids: list[uuid.UUID]) -> dict[uuid.UUID, PollResult]:
    found = {i: PollResult([], 0) for i in poll_ids}
    if not poll_ids:
        return found
    by_question: dict[uuid.UUID, QuestionResult] = {}
    for q in await session.scalars(
        select(PollQuestion).where(PollQuestion.poll_id.in_(poll_ids)).order_by(PollQuestion.order)
    ):
        item = QuestionResult(q, [], {}, Counter(), [], 0)
        by_question[q.id] = item
        found[q.poll_id].questions.append(item)
    for o in await session.scalars(
        select(PollOption).where(PollOption.poll_id.in_(poll_ids)).order_by(PollOption.order)
    ):
        by_question[o.question_id].options.append(o)
    choices = await session.execute(
        select(PollAnswer.question_id, PollAnswer.option_id, func.count())
        .where(PollAnswer.poll_id.in_(poll_ids), PollAnswer.option_id.is_not(None))
        .group_by(PollAnswer.question_id, PollAnswer.option_id)
    )
    for qid, oid, count in choices:
        if oid is None:  # pragma: no cover — sorguda süzüldü
            continue
        by_question[qid].option_votes[oid] = count
    ratings = await session.execute(
        select(PollAnswer.question_id, PollAnswer.rating, func.count())
        .where(PollAnswer.poll_id.in_(poll_ids), PollAnswer.rating.is_not(None))
        .group_by(PollAnswer.question_id, PollAnswer.rating)
    )
    for qid, rating, count in ratings:
        by_question[qid].ratings[int(rating or 0)] = count
    answered = await session.execute(
        select(PollAnswer.question_id, func.count(func.distinct(PollAnswer.ballot_id)))
        .where(PollAnswer.poll_id.in_(poll_ids))
        .group_by(PollAnswer.question_id)
    )
    for qid, count in answered:
        by_question[qid].answered = count
    ballots = await session.execute(
        select(PollBallot.poll_id, func.count())
        .where(PollBallot.poll_id.in_(poll_ids))
        .group_by(PollBallot.poll_id)
    )
    for pid, count in ballots:
        found[pid].ballots = count
    return found


async def text_answers(
    session: AsyncSession, poll_ids: list[uuid.UUID], limit: int = 200
) -> dict[uuid.UUID, list[str]]:
    """Metin yanıtları (soru → metinler, en yeniden). Kişi bilgisi olmadan."""
    found: dict[uuid.UUID, list[str]] = {}
    if not poll_ids:
        return found
    rows = await session.execute(
        select(PollAnswer.question_id, PollAnswer.text)
        .where(PollAnswer.poll_id.in_(poll_ids), PollAnswer.text.is_not(None))
        .order_by(PollAnswer.created_at.desc())
        .limit(limit * max(1, len(poll_ids)))
    )
    for qid, text in rows:
        bucket = found.setdefault(qid, [])
        if text and len(bucket) < limit:
            bucket.append(text)
    return found


# --- Sakin --------------------------------------------------------------------------


_ROLES = {
    PollAudience.ALL: [PartyRole.OWNER.value, PartyRole.TENANT.value, PartyRole.RESIDENT.value],
    PollAudience.OWNERS: [PartyRole.OWNER.value],
    PollAudience.TENANTS: [PartyRole.TENANT.value, PartyRole.RESIDENT.value],
}


@dataclass(frozen=True, slots=True)
class MyUnit:
    block_id: uuid.UUID
    roles: frozenset[str]


async def person_units(
    session: AsyncSession, person_id: uuid.UUID, today: date
) -> dict[uuid.UUID, MyUnit]:
    """Kişinin bugün bağlı olduğu bölümler, blokları ve oradaki rolleri."""
    rows = await session.execute(
        select(UnitParty.unit_id, Unit.block_id, UnitParty.role)
        .join(Unit, and_(Unit.id == UnitParty.unit_id, Unit.site_id == UnitParty.site_id))
        .where(
            UnitParty.person_id == person_id,
            UnitParty.role.in_(_ROLES[PollAudience.ALL]),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
    )
    roles: dict[uuid.UUID, set[str]] = {}
    blocks: dict[uuid.UUID, uuid.UUID] = {}
    for unit_id, block_id, role in rows:
        roles.setdefault(unit_id, set()).add(role)
        blocks[unit_id] = block_id
    return {u: MyUnit(blocks[u], frozenset(r)) for u, r in roles.items()}


def eligible_units(poll: Poll, units: dict[uuid.UUID, MyUnit]) -> list[uuid.UUID]:
    allowed = set(_ROLES[PollAudience(poll.audience)])
    return [
        unit
        for unit, mine in units.items()
        if mine.roles & allowed and (not poll.block_ids or mine.block_id in poll.block_ids)
    ]


async def unit_votes(
    session: AsyncSession, poll_ids: list[uuid.UUID], unit_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID | None]:
    """(anket, bölüm) → ilk sorudaki seçim (yoksa None) — yalnız sakinin kendi bölümleri."""
    if not poll_ids or not unit_ids:
        return {}
    ballots = await session.execute(
        select(PollBallot.id, PollBallot.poll_id, PollBallot.unit_id).where(
            PollBallot.poll_id.in_(poll_ids), PollBallot.unit_id.in_(unit_ids)
        )
    )
    found: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID | None] = {}
    by_ballot: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
    for ballot_id, poll_id, unit_id in ballots:
        if unit_id is None:  # pragma: no cover — sorguda süzüldü
            continue
        found[(poll_id, unit_id)] = None
        by_ballot[ballot_id] = (poll_id, unit_id)
    if by_ballot:
        first = (
            select(PollQuestion.id)
            .where(PollQuestion.poll_id == PollAnswer.poll_id, PollQuestion.order == 1)
            .correlate(PollAnswer)
            .scalar_subquery()
        )
        rows = await session.execute(
            select(PollAnswer.ballot_id, PollAnswer.option_id)
            .where(
                PollAnswer.ballot_id.in_(list(by_ballot)),
                PollAnswer.option_id.is_not(None),
                PollAnswer.question_id == first,
            )
            .order_by(PollAnswer.option_id)
        )
        for ballot_id, option_id in rows:
            key = by_ballot[ballot_id]
            if found[key] is None:
                found[key] = option_id
    return found


async def person_responded(
    session: AsyncSession, poll_ids: list[uuid.UUID], person_id: uuid.UUID
) -> set[uuid.UUID]:
    """Kişi başına anketlerde kişinin yanıt verdiği anketler (isimli: pusula; anonim: katılım)."""
    if not poll_ids:
        return set()
    named = await session.scalars(
        select(PollBallot.poll_id).where(
            PollBallot.poll_id.in_(poll_ids),
            PollBallot.person_id == person_id,
            PollBallot.unit_id.is_(None),
        )
    )
    anonymous = await session.scalars(
        select(PollParticipant.poll_id).where(
            PollParticipant.poll_id.in_(poll_ids), PollParticipant.person_id == person_id
        )
    )
    return set(named) | set(anonymous)


@dataclass(frozen=True, slots=True)
class AnswerInput:
    question_id: uuid.UUID
    option_ids: list[uuid.UUID] = field(default_factory=list)
    rating: int | None = None
    text: str | None = None


async def _check_answers(
    session: AsyncSession, poll: Poll, answers: list[AnswerInput], *, legacy: bool
) -> list[PollAnswer]:
    questions = list(
        await session.scalars(
            select(PollQuestion).where(PollQuestion.poll_id == poll.id).order_by(PollQuestion.order)
        )
    )
    options: dict[uuid.UUID, uuid.UUID] = dict(
        (
            await session.execute(
                select(PollOption.id, PollOption.question_id).where(PollOption.poll_id == poll.id)
            )
        ).all()
    )
    given = {a.question_id: a for a in answers}
    errors: dict[str, str] = {}
    known = {q.id for q in questions}
    for qid in given:
        if qid not in known:
            errors[f"answers.{qid}"] = "Bu soru ankette yok."
    rows: list[PollAnswer] = []
    for q in questions:
        key = "option_id" if legacy else f"answers.{q.id}"
        a = given.get(q.id)
        if a is None or (not a.option_ids and a.rating is None and not _clean(a.text)):
            if q.required:
                errors[key] = (
                    "Bir seçenek işaretleyin." if q.kind in CHOICE_KINDS else "Bu soru zorunlu."
                )
            continue
        if q.kind in CHOICE_KINDS:
            picked = list(dict.fromkeys(a.option_ids))
            if not picked or any(options.get(o) != q.id for o in picked):
                errors[key] = "Bir seçenek işaretleyin."
            elif q.kind == PollQuestionKind.SINGLE.value and len(picked) != 1:
                errors[key] = "Tek seçenek işaretleyin."
            else:
                rows += [PollAnswer(question_id=q.id, option_id=o) for o in picked]
        elif q.kind == PollQuestionKind.RATING.value:
            if a.rating is None or not 1 <= a.rating <= 5:
                errors[key] = "1 ile 5 arasında puan verin."
            else:
                rows.append(PollAnswer(question_id=q.id, rating=a.rating))
        else:
            text = (a.text or "").strip()
            if not text or len(text) > TEXT_ANSWER_MAX:
                errors[key] = f"Yanıtı yazın (en fazla {TEXT_ANSWER_MAX} karakter)."
            else:
                rows.append(PollAnswer(question_id=q.id, text=text))
    if errors:
        raise FormFieldError(errors)
    return rows


async def vote(
    session: AsyncSession,
    poll: Poll,
    *,
    person_id: uuid.UUID,
    unit_id: uuid.UUID | None,
    answers: list[AnswerInput],
    today: date,
    legacy: bool = False,
) -> PollBallot:
    """Yanıt (oy) verir; değiştirilemez. Sıra: kapanış 409 → bölüm 404 → hedef 403 → yanıt 422."""
    if status_of(poll, today) is PollStatus.CLOSED:
        raise OperationRuleError("poll_closed", "Anket kapandı; oy verilemez.", conflict=True)
    units = await person_units(session, person_id, today)
    eligible = eligible_units(poll, units)
    if poll.one_per_unit:
        if unit_id is None or unit_id not in units:
            raise LookupError("Bu bölüm adına oy veremezsiniz.")
        if unit_id not in eligible:
            raise PermissionError("Bu anket bölümünüzdeki rolünüze açık değil.")
    elif not eligible:
        raise PermissionError("Bu anket bölümünüzdeki rolünüze açık değil.")
    rows = await _check_answers(session, poll, answers, legacy=legacy)
    ballot = PollBallot(
        id=uuid.uuid7() if not poll.is_anonymous else uuid.uuid4(),
        poll_id=poll.id,
        unit_id=unit_id if poll.one_per_unit else None,
        person_id=None if poll.is_anonymous else person_id,
    )
    try:
        async with session.begin_nested():
            if poll.is_anonymous and not poll.one_per_unit:
                session.add(PollParticipant(poll_id=poll.id, person_id=person_id))
            session.add(ballot)
            await session.flush()
            for row in rows:
                row.ballot_id, row.poll_id = ballot.id, poll.id
            session.add_all(rows)
            await session.flush()
    except IntegrityError as exc:
        message = (
            "Bu bölüm adına zaten oy verildi."
            if poll.one_per_unit
            else "Bu ankete zaten yanıt verdiniz."
        )
        raise OperationRuleError("already_voted", message, conflict=True) from exc
    return ballot


# --- İsimli döküm (yalnız yetkili, K31) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class NamedAnswer:
    question_id: uuid.UUID
    option_ids: list[uuid.UUID]
    rating: int | None
    text: str | None


@dataclass(frozen=True, slots=True)
class NamedResponse:
    person_name: str
    unit_id: uuid.UUID | None
    at: datetime
    answers: list[NamedAnswer]


async def named_responses(session: AsyncSession, poll: Poll) -> list[NamedResponse]:
    if poll.is_anonymous:
        raise OperationRuleError(
            "anonymous_poll", "Anonim ankette kimin yanıtladığı tutulmaz.", conflict=True
        )
    ballots = (
        await session.execute(
            select(PollBallot, Person.first_name, Person.last_name)
            .join(
                Person,
                and_(Person.id == PollBallot.person_id, Person.site_id == PollBallot.site_id),
            )
            .where(PollBallot.poll_id == poll.id)
            .order_by(PollBallot.created_at, PollBallot.id)
        )
    ).all()
    answers: dict[uuid.UUID, dict[uuid.UUID, NamedAnswer]] = {}
    for a in await session.scalars(select(PollAnswer).where(PollAnswer.poll_id == poll.id)):
        per = answers.setdefault(a.ballot_id, {})
        prev = per.get(a.question_id)
        option_ids = (prev.option_ids if prev else []) + ([a.option_id] if a.option_id else [])
        per[a.question_id] = NamedAnswer(a.question_id, option_ids, a.rating, a.text)
    return [
        NamedResponse(
            f"{first} {last}", b.unit_id, b.created_at, list(answers.get(b.id, {}).values())
        )
        for b, first, last in ballots
    ]
