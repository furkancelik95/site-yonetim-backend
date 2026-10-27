"""Talep yönetimi ekleri — iç not, ana olay ve birleştirme önerisi, sakin kapanış onayı ve puan,
zamanlanmış işler (Yazılım Dokümanı §12.3, HIZ-01/02; servis istekleri 48–50; K18, K26, K27).
Açık site kapsamında; commit çağırana ait (zamanlanmış iş hariç).

- **İç not** ayrı kayıttır (K26): yalnız `requests.assign` okur/yazar; sakin uçlarında,
  bildirimlerde ve olay geçmişinde **hiç** dönmez. Değişmezdir (silinmez, düzenlenmez).
- **Ana olay** (`outages`): bir talep en fazla bir olaya bağlı (409 `already_linked`); çözülmüş
  olaya bağlanmaz (409 `outage_resolved`). Olay çözülünce bağlı **açık** talepler aynı
  transaction'da "çözüldü" olur ve her sakine bildirim gider (HIZ-02 S2).
- **Birleştirme önerisi** kural tabanlı (yapay zekâ yok): aynı kategori + (aynı konum metni ya da
  ikisi de ortak alan ya da aynı blok) + pencere (`merge_window_hours`) içinde açılmış, bitmemiş
  ve olaya bağlı olmayan talepler; aynı kategorideki açık ana olaylar.
- **Kapanış onayı**: yalnız "çözüldü" talep (409 `not_resolved`). Çözüldü → 1–5 puan, talep
  kapanır; puan ≤ eşik → yöneticiye bildirim. Devam ediyor → açıklama zorunlu, talep yeniden açılır.
- **Zamanlanmış iş** (`cli.py run-request-jobs`, 5 dakikada bir): yanıtsız çözüldü talepleri
  kapatır; SLA'nın %80'inde talebe bakan personele, aşımında yöneticiye bildirim.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, and_, false, func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import (
    ACTIVE_STATUSES,
    STATUS_LABELS,
    OperationRuleError,
    RequestEventKind,
    RequestStatus,
    reopen_status,
)
from site_yonetim.domain.service_desk import RuleError, check_feedback
from site_yonetim.models import (
    Outage,
    Request,
    RequestCategory,
    RequestFeedback,
    RequestInternalNote,
    Site,
    Unit,
)
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import request_setup
from site_yonetim.services import requests as requests_svc
from site_yonetim.services import roles as roles_svc

NOTE_MIN, NOTE_MAX = 2, 2000
JOB_ACTOR = "Talep zamanlayıcısı"


# --- Personel bildirimi -------------------------------------------------------------------


async def staff_user_ids(
    session: AsyncSession, site: Site, permission: Permission
) -> set[uuid.UUID]:
    """İzni taşıyan etkin personel (izne bakar, rol adına değil). `requests.settings` = yönetici."""
    return await roles_svc.permission_holders(session, site, permission)


async def notify_staff(
    session: AsyncSession,
    site: Site,
    permission: Permission,
    request: Request,
    *,
    kind: str,
    title: str,
    key: str,
    body: str | None = None,
) -> None:
    await notify_svc.notify(
        session,
        await staff_user_ids(session, site, permission),
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=body or f"#{request.number} {request.title}",
            link_entity="requests",
            link_id=request.id,
        ),
        dedupe_key=f"request:{request.id}:{key}",
    )


# --- İç not --------------------------------------------------------------------------------


async def add_note(
    session: AsyncSession,
    request: Request,
    body: str | None,
    *,
    user_id: uuid.UUID,
    author: str,
) -> RequestInternalNote:
    text = (body or "").strip()
    if not NOTE_MIN <= len(text) <= NOTE_MAX:
        raise RuleError({"body": "Not yazın."})
    note = RequestInternalNote(
        request_id=request.id, body=text, author_user_id=user_id, author_name=author
    )
    session.add(note)
    await session.flush()
    return note


async def notes(session: AsyncSession, request_id: uuid.UUID) -> list[RequestInternalNote]:
    return list(
        await session.scalars(
            select(RequestInternalNote)
            .where(RequestInternalNote.request_id == request_id)
            .order_by(RequestInternalNote.id)
        )
    )


# --- Ana olay ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutageData:
    title: str | None
    category: str | None
    location: str | None
    public_description: str | None
    request_ids: list[uuid.UUID] = field(default_factory=list)


async def _link(
    session: AsyncSession, outage: Outage, request: Request, *, actor: str, now: datetime
) -> None:
    if request.outage_id is not None:
        raise OperationRuleError(
            "already_linked", "Talep zaten bir ana olaya bağlı.", conflict=True
        )
    if RequestStatus(request.status) not in ACTIVE_STATUSES:
        raise OperationRuleError(
            "request_not_active",
            f"#{request.number} talep {STATUS_LABELS[RequestStatus(request.status)].lower()}; "
            "yalnız açık talepler ana olaya bağlanır.",
            conflict=True,
        )
    request.outage_id = outage.id
    session.add(
        requests_svc.event(
            request, RequestEventKind.OUTAGE_LINKED, f"Ana olaya bağlandı: {outage.title}",
            actor, at=now,
        )
    )  # fmt: skip


async def _locked_requests(session: AsyncSession, ids: list[uuid.UUID]) -> list[Request]:
    if not ids:
        return []
    rows = list(
        await session.scalars(
            select(Request).where(Request.id.in_(ids)).order_by(Request.id).with_for_update()
        )
    )
    if len(rows) != len(set(ids)):
        raise RuleError({"request_ids": "Talep bulunamadı."})
    return rows


async def create_outage(
    session: AsyncSession, data: OutageData, *, actor: str, now: datetime
) -> Outage:
    errors: dict[str, str] = {}
    title = " ".join((data.title or "").split())
    description = (data.public_description or "").strip()
    if not 3 <= len(title) <= 120:
        errors["title"] = "Başlık yazın."
    if not 10 <= len(description) <= 1000:
        errors["public_description"] = "Sakinlerin göreceği açıklamayı yazın."
    if not data.category or not await session.scalar(
        select(RequestCategory.id).where(RequestCategory.key == data.category)
    ):
        errors["category"] = "Kategori seçin."
    if errors:
        raise RuleError(errors)
    outage = Outage(
        title=title,
        category=data.category,
        location=" ".join((data.location or "").split()) or None,
        public_description=description,
        status="open",
        created_by_name=actor,
    )
    session.add(outage)
    await session.flush()
    for request in await _locked_requests(session, list(dict.fromkeys(data.request_ids))):
        await _link(session, outage, request, actor=actor, now=now)
    await session.flush()
    return outage


async def get_outage(
    session: AsyncSession, outage_id: uuid.UUID, *, lock: bool = False
) -> Outage | None:
    query = select(Outage).where(Outage.id == outage_id)
    found: Outage | None = await session.scalar(query.with_for_update() if lock else query)
    return found


async def link(
    session: AsyncSession, outage: Outage, request_id: uuid.UUID, *, actor: str, now: datetime
) -> None:
    if outage.status != "open":
        raise OperationRuleError(
            "outage_resolved", "Çözülmüş ana olaya talep bağlanamaz.", conflict=True
        )
    [request] = await _locked_requests(session, [request_id])
    await _link(session, outage, request, actor=actor, now=now)
    await session.flush()


async def unlink(
    session: AsyncSession, outage: Outage, request_id: uuid.UUID, *, actor: str, now: datetime
) -> None:
    [request] = await _locked_requests(session, [request_id])
    if request.outage_id != outage.id:
        raise OperationRuleError("not_linked", "Talep bu ana olaya bağlı değil.", conflict=True)
    if outage.status != "open":
        raise OperationRuleError(
            "outage_resolved", "Çözülmüş ana olaydan talep ayrılamaz.", conflict=True
        )
    request.outage_id = None
    session.add(
        requests_svc.event(
            request, RequestEventKind.OUTAGE_UNLINKED, f"Ana olaydan ayrıldı: {outage.title}",
            actor, at=now,
        )
    )  # fmt: skip
    await session.flush()


async def resolve_outage(
    session: AsyncSession, outage: Outage, resolution: str | None, *, actor: str, now: datetime
) -> int:
    """Olayı çözer; bağlı açık talepleri "çözüldü" yapar (atomik). Kaç talep çözüldü döner."""
    if outage.status != "open":
        raise OperationRuleError("outage_resolved", "Ana olay zaten çözüldü.", conflict=True)
    text = " ".join((resolution or "").split())
    if len(text) < 5:
        raise RuleError({"resolution": "Çözüm açıklaması yazın; bağlı tüm sakinlere gider."})
    outage.status = "resolved"
    outage.resolution = text
    outage.resolved_at = now
    outage.resolved_by_name = actor
    linked = list(
        await session.scalars(
            select(Request)
            .where(
                Request.outage_id == outage.id,
                Request.status.in_([s.value for s in ACTIVE_STATUSES]),
            )
            .order_by(Request.id)
            .with_for_update()
        )
    )
    for request in linked:
        await requests_svc.apply_status(
            session,
            request,
            RequestStatus.RESOLVED,
            kind=RequestEventKind.RESOLVED,
            description=f"Ana olay çözüldü ({outage.title}). Çözüm: {text}",
            actor=actor,
            now=now,
            resolution=text,
        )
    await session.flush()
    return len(linked)


async def outage_counts(
    session: AsyncSession, ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not ids:
        return {}
    rows = await session.execute(
        select(Request.outage_id, Request.id)
        .where(Request.outage_id.in_(ids))
        .order_by(Request.number)
    )
    linked: dict[uuid.UUID, list[uuid.UUID]] = {i: [] for i in ids}
    for outage_id, request_id in rows:
        if outage_id is not None:
            linked[outage_id].append(request_id)
    return linked


@dataclass(frozen=True, slots=True)
class Suggestions:
    linked: Outage | None
    similar: list[Request]
    open_outages: list[tuple[Outage, int]]
    window_hours: int


async def merge_suggestions(session: AsyncSession, request: Request) -> Suggestions:
    settings = await request_setup.load_settings(session)
    window = timedelta(hours=settings.merge_window_hours)
    linked = await get_outage(session, request.outage_id) if request.outage_id else None
    if linked is not None:
        return Suggestions(linked, [], [], settings.merge_window_hours)
    place: list[ColumnElement[bool]] = []
    if request.unit_id is None:  # ikisi de ortak alan
        place.append(Request.unit_id.is_(None))
    if request.location:
        place.append(func.lower(Request.location) == request.location.lower())
    if request.unit_id is not None:
        block = select(Unit.block_id).where(Unit.id == request.unit_id).scalar_subquery()
        same_block = (
            select(Unit.id)
            .where(Unit.id == Request.unit_id, Unit.block_id == block)
            .correlate(Request)
            .exists()
        )
        place.append(same_block)
    similar = list(
        await session.scalars(
            select(Request)
            .where(
                Request.id != request.id,
                Request.category == request.category,
                Request.outage_id.is_(None),
                Request.status.in_([s.value for s in ACTIVE_STATUSES]),
                Request.created_at >= request.created_at - window,
                Request.created_at <= request.created_at + window,
                or_(*place) if place else false(),
            )
            .order_by(Request.created_at)
            .limit(20)
        )
    )
    outages = list(
        await session.scalars(
            select(Outage)
            .where(Outage.status == "open", Outage.category == request.category)
            .order_by(Outage.created_at.desc())
            .limit(10)
        )
    )
    counts = await outage_counts(session, [o.id for o in outages])
    return Suggestions(
        None,
        similar,
        [(o, len(counts.get(o.id, []))) for o in outages],
        settings.merge_window_hours,
    )


# --- Kapanış onayı -------------------------------------------------------------------------


async def latest_feedback(
    session: AsyncSession, request_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RequestFeedback]:
    if not request_ids:
        return {}
    rows = await session.scalars(
        select(RequestFeedback)
        .where(RequestFeedback.request_id.in_(request_ids))
        .order_by(RequestFeedback.request_id, RequestFeedback.id.desc())
        .ext(distinct_on(RequestFeedback.request_id))
    )
    return {f.request_id: f for f in rows}


async def confirm(
    session: AsyncSession,
    site: Site,
    request: Request,
    *,
    solved: bool,
    score: int | None,
    comment: str | None,
    person_id: uuid.UUID,
    actor: str,
    now: datetime,
) -> RequestFeedback:
    """Sakin kapanış onayı (HIZ-01 adım 7–10). `request` kilitli gelmeli."""
    if request.status != RequestStatus.RESOLVED.value:
        raise OperationRuleError(
            "not_resolved", "Yalnız çözüldü durumundaki talep onaylanır.", conflict=True
        )
    text = check_feedback(solved, score, comment)
    feedback = RequestFeedback(
        request_id=request.id,
        solved=solved,
        score=score if solved else None,
        comment=text,
        person_id=person_id,
        by_name=actor,
    )
    session.add(feedback)
    await session.flush()
    if solved:
        note = f"Sakin çözümü onayladı ({score}/5)" + (f": {text}" if text else ".")
        await requests_svc.apply_status(
            session, request, RequestStatus.CLOSED, kind=RequestEventKind.CLOSED,
            description=note, actor=actor, now=now, resolution=request.resolution,
        )  # fmt: skip
        threshold = (await request_setup.load_settings(session)).low_rating_threshold
        if score is not None and score <= threshold:
            await notify_staff(
                session, site, Permission.REQUESTS_SETTINGS, request,
                kind="request.low_rating", title=f"Düşük memnuniyet puanı: {score}/5",
                key=f"low_rating:{feedback.id}",
                body=f"#{request.number} {request.title}" + (f" — \"{text}\"" if text else ""),
            )  # fmt: skip
        return feedback
    target = reopen_status(bool(request.assigned_to))
    await requests_svc.apply_status(
        session, request, target, kind=RequestEventKind.REOPENED,
        description=f"Sakin sorunun devam ettiğini bildirdi: {text}", actor=actor, now=now,
        resolution=request.resolution,
    )  # fmt: skip
    await notify_staff(
        session, site, Permission.REQUESTS_ASSIGN, request,
        kind="request.reopened", title=f"Talep yeniden açıldı: #{request.number}",
        key=f"reopened:{feedback.id}", body=f"Sakin: {text}",
    )  # fmt: skip
    return feedback


# --- Zamanlanmış iş ------------------------------------------------------------------------


@dataclass(slots=True)
class JobResult:
    closed: int = 0
    warned: int = 0
    breached: int = 0


async def run_jobs(session: AsyncSession, site: Site, *, now: datetime) -> JobResult:
    result = JobResult()
    active = [s.value for s in ACTIVE_STATUSES]
    stale = list(
        await session.scalars(
            select(Request)
            .where(Request.status == RequestStatus.RESOLVED.value, Request.auto_close_at <= now)
            .order_by(Request.id)
            .with_for_update(skip_locked=True)
        )
    )
    days = (await request_setup.load_settings(session)).auto_close_days
    for request in stale:
        await requests_svc.apply_status(
            session, request, RequestStatus.CLOSED, kind=RequestEventKind.CLOSED,
            description=f"Sakin {days} gün içinde yanıt vermediği için talep kendiliğinden "
            "kapandı.",
            actor=JOB_ACTOR, now=now, resolution=request.resolution,
        )  # fmt: skip
        result.closed += 1
    warn = list(
        await session.scalars(
            select(Request).where(
                Request.status.in_(active),
                Request.sla_warn_at <= now,
                Request.sla_warned_at.is_(None),
            )
        )
    )
    for request in warn:
        request.sla_warned_at = now
        await notify_staff(
            session, site, Permission.REQUESTS_ASSIGN, request, kind="request.sla_warning",
            title=f"SLA doluyor: #{request.number}", key="sla_warning",
        )  # fmt: skip
        result.warned += 1
    breached = list(
        await session.scalars(
            select(Request).where(
                Request.status.in_(active),
                Request.due_at <= now,
                Request.sla_breached_at.is_(None),
            )
        )
    )
    for request in breached:
        request.sla_breached_at = now
        await notify_staff(
            session, site, Permission.REQUESTS_SETTINGS, request, kind="request.sla_breached",
            title=f"SLA aşıldı: #{request.number}", key="sla_breached",
        )  # fmt: skip
        result.breached += 1
    await session.flush()
    return result


async def run_all_jobs(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[tuple[uuid.UUID, JobResult]]:
    """Site başına ayrı transaction — bir sitedeki hata diğerini durdurmaz."""
    active = [s.value for s in ACTIVE_STATUSES]
    with all_sites_scope():
        async with factory() as session:
            due = select(Request.site_id).where(
                or_(
                    and_(
                        Request.status == RequestStatus.RESOLVED.value,
                        Request.auto_close_at <= now,
                    ),
                    and_(
                        Request.status.in_(active),
                        or_(
                            and_(Request.sla_warn_at <= now, Request.sla_warned_at.is_(None)),
                            and_(Request.due_at <= now, Request.sla_breached_at.is_(None)),
                        ),
                    ),
                )
            )
            site_ids = sorted(set(await session.scalars(due.distinct())))
    set_actor(Actor(None, JOB_ACTOR, None))
    results: list[tuple[uuid.UUID, JobResult]] = []
    for site_id in site_ids:
        with site_scope(site_id):
            async with factory() as session, session.begin():
                site = await session.get(Site, site_id)
                if site is None:  # pragma: no cover
                    continue
                results.append((site_id, await run_jobs(session, site, now=now)))
    return results
