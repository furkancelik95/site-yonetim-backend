"""Ziyaretçi ön daveti ve habersiz ziyaretçide sakin onayı (servis istekleri 41, 42;
Yazılım Dokümanı GUV-02, GUV-03). Site kapsamında; transaction'ı çağıran yönetir.

- Davet kodu `secrets` ile üretilir (6 hane), **yalnız HMAC özeti** saklanır (anahtar sunucu
  sırrından türetilir; veritabanı tek başına kodu vermez). Kod yalnız oluşturma ve "yeni kod"
  yanıtında bir kez görünür. Kapıda kodla giriş hız sınırlıdır (API katmanı).
- Habersiz ziyaretçi: yöntem kapı istisnası → site ayarı. Bilgi yönteminde ziyaretçi hemen girer,
  sakine bilgi; onay yönteminde sakine onay isteği, süre içinde ilk yanıt geçerli, süre dolunca
  görevli gerekçeli karar verir. Her karar denetim kaydına yazılır (kişisel veri olmadan).
- Bildirimler uygulama içi (dış kanal K5). Sessiz saat henüz yok; geldiğinde onay isteği aşar.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.gatehouse import (
    INVITE_CODE_DIGITS,
    DecisionSource,
    InviteForm,
    InviteStatus,
    InviteType,
    InviteWindow,
    SecuritySettingsData,
    UnannouncedMethod,
    VisitDecision,
    VisitRequestStatus,
    check_check_in,
    check_guard_decision,
    check_person_name,
    check_resident_decision,
    normalize_plate,
)
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import VisitorKind
from site_yonetim.models import Visitor, VisitorInvite, VisitRequest
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services import security as security_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.gatehouse import unit_user_ids

_CAPACITY_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
CODE_ATTEMPTS = 20
RESIDENT_LIST_LIMIT = 100
EXPECTED_LIMIT = 200
REQUEST_LIST_LIMIT = 100


def local(now: datetime) -> datetime:
    return now.astimezone(BUSINESS_TZ)


# --- Davet kodu -----------------------------------------------------------------------------


def code_hash(secret: str, site_id: uuid.UUID, code: str) -> str:
    """Kodun özeti: HMAC-SHA256, anahtar sunucu sırrından türetilir; site kimliği mesajda."""
    key = hashlib.sha256(b"site-yonetim:visitor-invite:" + secret.encode()).digest()
    return hmac.new(key, f"{site_id}:{code}".encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10**INVITE_CODE_DIGITS):0{INVITE_CODE_DIGITS}d}"


async def _unique_code(session: AsyncSession, secret: str, site_id: uuid.UUID) -> tuple[str, str]:
    for _ in range(CODE_ATTEMPTS):
        code = new_code()
        digest = code_hash(secret, site_id, code)
        taken = await session.scalar(
            select(VisitorInvite.id).where(
                VisitorInvite.code_hash == digest,
                VisitorInvite.status == InviteStatus.ACTIVE.value,
            )
        )
        if taken is None:
            return code, digest
    raise OperationRuleError(  # pragma: no cover - 10^6 kod alanında pratikte olmaz
        "code_unavailable", "Davet kodu üretilemedi, tekrar deneyin.", conflict=True
    )


def window(invite: VisitorInvite) -> InviteWindow:
    return InviteWindow(
        InviteType(invite.type),
        invite.date_from,
        invite.date_to,
        tuple(invite.weekdays or ()),
        invite.time_from,
        invite.time_to,
    )


# --- Ön davet (sakin) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Inviter:
    person_id: uuid.UUID
    user_id: uuid.UUID
    name: str


async def create_invite(
    session: AsyncSession,
    form: InviteForm,
    *,
    unit_id: uuid.UUID,
    inviter: Inviter,
    settings: SecuritySettingsData,
    site_id: uuid.UUID,
    secret: str,
) -> tuple[VisitorInvite, str]:
    if form.plate is not None:
        # Plakalı davet misafir parkına sayılır; eşzamanlı davetler kapasiteyi aşmasın.
        await session.execute(_CAPACITY_LOCK, {"key": f"guest-parking:{site_id}"})
        day = form.window.date_from
        used = await session.scalar(
            select(func.count())
            .select_from(VisitorInvite)
            .where(
                VisitorInvite.status == InviteStatus.ACTIVE.value,
                VisitorInvite.plate.is_not(None),
                VisitorInvite.date_from <= day,
                VisitorInvite.date_to >= day,
            )
        )
        capacity = settings.guest_parking_capacity
        if (used or 0) >= capacity:
            raise OperationRuleError(
                "guest_parking_full",
                f"Misafir parkı bu tarihte dolu ({capacity}). Plakasız davet oluşturabilirsiniz.",
                conflict=True,
            )
    code, digest = await _unique_code(session, secret, site_id)
    w = form.window
    invite = VisitorInvite(
        unit_id=unit_id,
        created_by_person_id=inviter.person_id,
        created_by_user_id=inviter.user_id,
        created_by_name=inviter.name,
        visitor_name=form.visitor_name,
        type=w.type.value,
        date_from=w.date_from,
        date_to=w.date_to,
        weekdays=list(w.weekdays),
        time_from=w.time_from,
        time_to=w.time_to,
        plate=form.plate,
        note=form.note,
        code_hash=digest,
        status=InviteStatus.ACTIVE.value,
    )
    session.add(invite)
    await session.flush()
    return invite, code


def resident_invites_query(unit_ids: set[uuid.UUID], today: date) -> Select[VisitorInvite]:
    """Etkin davetler ve son 30 günün geçmişi (yeni üstte)."""
    return (
        select(VisitorInvite)
        .where(
            VisitorInvite.unit_id.in_(unit_ids),
            or_(
                VisitorInvite.status == InviteStatus.ACTIVE.value,
                VisitorInvite.date_to >= today - timedelta(days=30),
            ),
        )
        .order_by(VisitorInvite.created_at.desc(), VisitorInvite.id.desc())
        .limit(RESIDENT_LIST_LIMIT)
    )


async def get_invite(
    session: AsyncSession, invite_id: uuid.UUID, unit_ids: set[uuid.UUID]
) -> VisitorInvite | None:
    row: VisitorInvite | None = await session.scalar(
        select(VisitorInvite)
        .where(VisitorInvite.id == invite_id, VisitorInvite.unit_id.in_(unit_ids))
        .with_for_update()
    )
    return row


def _require_active(invite: VisitorInvite, today: date) -> None:
    if invite.status != InviteStatus.ACTIVE.value or invite.date_to < today:
        raise OperationRuleError("invalid_status", "Bu davet artık geçerli değil.", conflict=True)


async def cancel_invite(
    session: AsyncSession, invite: VisitorInvite, *, now: datetime
) -> VisitorInvite:
    if invite.status != InviteStatus.ACTIVE.value:
        raise OperationRuleError("invalid_status", "Bu davet artık geçerli değil.", conflict=True)
    invite.status = InviteStatus.CANCELLED.value
    invite.cancelled_at = now
    await session.flush()
    return invite


async def reissue_code(
    session: AsyncSession, invite: VisitorInvite, *, today: date, site_id: uuid.UUID, secret: str
) -> str:
    """Yeni kod üretir; eski kod hemen geçersizleşir (kod kaybolduysa ya da sızdıysa)."""
    _require_active(invite, today)
    code, digest = await _unique_code(session, secret, site_id)
    invite.code_hash = digest
    await session.flush()
    return code


# --- Kapı: beklenen davetliler ve kodla giriş -----------------------------------------------


async def expected_today(session: AsyncSession, today: date) -> list[VisitorInvite]:
    rows = await session.scalars(
        select(VisitorInvite)
        .where(
            VisitorInvite.status == InviteStatus.ACTIVE.value,
            VisitorInvite.date_from <= today,
            VisitorInvite.date_to >= today,
        )
        .order_by(VisitorInvite.time_from.nulls_first(), VisitorInvite.visitor_name)
        .limit(EXPECTED_LIMIT)
    )
    return [i for i in rows if window(i).valid_on(today)]


@dataclass(frozen=True, slots=True)
class CheckIn:
    invite: VisitorInvite
    visitor: Visitor


async def check_in(
    session: AsyncSession,
    code: str,
    *,
    now: datetime,
    site_id: uuid.UUID,
    secret: str,
    recorded_by: str,
) -> CheckIn:
    digest = code_hash(secret, site_id, code)
    invite: VisitorInvite | None = await session.scalar(
        select(VisitorInvite)
        .where(
            VisitorInvite.code_hash == digest,
            VisitorInvite.status != InviteStatus.CANCELLED.value,
        )
        # etkin olan önce; yoksa en son kullanılan (409 mesajı için)
        .order_by(
            (VisitorInvite.status == InviteStatus.ACTIVE.value).desc(),
            VisitorInvite.created_at.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if invite is None:
        raise OperationRuleError(
            "invite_not_found", "Bu kodla davet yok. Habersiz ziyaretçi olarak kaydedin."
        )
    at = local(now)
    check_check_in(InviteStatus(invite.status), window(invite), at.date(), at.time())
    visitor = await security_svc.record_visitor(
        session,
        security_svc.NewVisitor(
            unit_id=invite.unit_id,
            full_name=invite.visitor_name,
            kind=VisitorKind.GUEST,
            host_person_id=invite.created_by_person_id,
            plate_number=invite.plate,
            note="Ön davetle giriş",
        ),
        now=now,
        today=at.date(),
        recorded_by=recorded_by,
    )
    if invite.type == InviteType.ONCE.value:
        invite.status = InviteStatus.USED.value
    invite.used_at = now
    invite.visitor_id = visitor.id
    await session.flush()
    if invite.created_by_user_id is not None:
        await notify_svc.notify(
            session,
            [invite.created_by_user_id],
            notify_svc.Message(
                category=NotificationCategory.SECURITY,
                kind="visitor.invite_used",
                title=f"Davetli ziyaretçiniz giriş yaptı: {invite.visitor_name}",
                link_entity="visitor_invites",
                link_id=invite.id,
            ),
            dedupe_key=f"visitor-invite:{invite.id}:{visitor.id}",
        )
    return CheckIn(invite, visitor)


# --- Habersiz ziyaretçi -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewVisitRequest:
    unit_id: uuid.UUID
    full_name: str | None
    kind: VisitorKind
    plate: str | None
    gate_id: uuid.UUID | None


async def _notify_unit(
    session: AsyncSession,
    request: VisitRequest,
    today: date,
    *,
    kind: str,
    title: str,
    body: str | None = None,
) -> None:
    users = await unit_user_ids(session, {request.unit_id}, today)
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.SECURITY,
            kind=kind,
            title=title,
            body=body,
            link_entity="visit_requests",
            link_id=request.id,
        ),
        dedupe_key=f"visit-request:{request.id}:{kind}",
    )


async def _enter(
    session: AsyncSession,
    request: VisitRequest,
    *,
    now: datetime,
    today: date,
    recorded_by: str,
    host_person_id: uuid.UUID | None = None,
) -> Visitor:
    visitor = await security_svc.record_visitor(
        session,
        security_svc.NewVisitor(
            unit_id=request.unit_id,
            full_name=request.full_name,
            kind=VisitorKind(request.kind),
            host_person_id=host_person_id,
            plate_number=request.plate,
        ),
        now=now,
        today=today,
        recorded_by=recorded_by,
    )
    request.visitor_id = visitor.id
    return visitor


async def create_request(
    session: AsyncSession,
    data: NewVisitRequest,
    *,
    settings: SecuritySettingsData,
    now: datetime,
    actor: str,
) -> VisitRequest:
    await security_svc.require_unit(session, data.unit_id)
    name = check_person_name(
        data.full_name, "full_name", "Ziyaretçinin adını soyadını yazın (yalnız harf).", low=2
    )
    method = settings.method_for(str(data.gate_id) if data.gate_id else None)
    today = local(now).date()
    plate = normalize_plate(data.plate) or None if settings.visitor_fields.plate else None
    request = VisitRequest(
        unit_id=data.unit_id,
        gate_id=data.gate_id,
        full_name=name,
        kind=data.kind.value,
        plate=plate,
        method=method.value,
        status=VisitRequestStatus.AWAITING.value,
        expires_at=now + timedelta(seconds=settings.approval_timeout_seconds),
        visit_date=today,
        created_by=actor,
    )
    session.add(request)
    await session.flush()
    if method is UnannouncedMethod.INFO:
        request.status = VisitRequestStatus.ALLOWED.value
        request.expires_at = now
        await _enter(session, request, now=now, today=today, recorded_by=actor)
        await _notify_unit(
            session,
            request,
            today,
            kind="visitor.arrived",
            title=f"Ziyaretçiniz geldi: {name}",
            body="Görevli kaydetti ve içeri aldı.",
        )
    else:
        await _notify_unit(
            session,
            request,
            today,
            kind="visitor.approval_requested",
            title=f"Kapıda ziyaretçiniz var: {name}",
            body=f"İçeri alınsın mı? {settings.approval_timeout_seconds} sn içinde yanıt "
            "vermezseniz görevli sizi arar.",
        )
    await session.flush()
    return request


def requests_query(today: date, status: VisitRequestStatus | None) -> Select[VisitRequest]:
    """Bugünkü talepler ve (günü geçmiş olsa da) bekleyenler — yeni üstte."""
    query = select(VisitRequest).where(
        or_(
            VisitRequest.visit_date == today,
            VisitRequest.status == VisitRequestStatus.AWAITING.value,
        )
    )
    if status is not None:
        query = query.where(VisitRequest.status == status.value)
    return query.order_by(VisitRequest.created_at.desc(), VisitRequest.id.desc()).limit(
        REQUEST_LIST_LIMIT
    )


async def get_request(
    session: AsyncSession, request_id: uuid.UUID, unit_ids: set[uuid.UUID] | None = None
) -> VisitRequest | None:
    query = select(VisitRequest).where(VisitRequest.id == request_id)
    if unit_ids is not None:
        query = query.where(VisitRequest.unit_id.in_(unit_ids))
    row: VisitRequest | None = await session.scalar(query.with_for_update())
    return row


def _audit(session: AsyncSession, request: VisitRequest) -> None:
    """Karar denetim kaydına — ziyaretçi adı/plaka olmadan (saklama süresi denetimden uzun)."""
    record(
        session,
        AuditAction.UPDATE,
        "visit_requests",
        request.id,
        {
            "status": request.status,
            "decision_source": request.decision_source,
            "unit_id": request.unit_id,
            "reason": request.reason,
        },
    )


async def guard_decide(
    session: AsyncSession,
    request: VisitRequest,
    *,
    decision: str | None,
    reason: str | None,
    now: datetime,
    actor: str,
    user_id: uuid.UUID,
) -> VisitRequest:
    choice, why = check_guard_decision(
        VisitRequestStatus(request.status), request.expires_at, now, decision, reason
    )
    today = local(now).date()
    request.decision_source = DecisionSource.GUARD.value
    request.decided_by = actor
    request.decided_by_user_id = user_id
    request.decided_at = now
    request.reason = why
    if choice is VisitDecision.ALLOW:
        request.status = VisitRequestStatus.ALLOWED.value
        await _enter(session, request, now=now, today=today, recorded_by=actor)
        await _notify_unit(
            session,
            request,
            today,
            kind="visitor.allowed_by_guard",
            title=f"Ziyaretçiniz görevli kararıyla içeri alındı: {request.full_name}",
            body=why,
        )
    else:
        request.status = VisitRequestStatus.DENIED.value
        await _notify_unit(
            session,
            request,
            today,
            kind="visitor.denied",
            title=f"Ziyaretçinizin girişi reddedildi: {request.full_name}",
            body=why,
        )
    await session.flush()
    _audit(session, request)
    return request


async def resident_decide(
    session: AsyncSession,
    request: VisitRequest,
    *,
    decision: str | None,
    now: datetime,
    actor: str,
    user_id: uuid.UUID,
    person_id: uuid.UUID,
) -> VisitRequest:
    choice = check_resident_decision(
        VisitRequestStatus(request.status), request.expires_at, now, decision
    )
    request.decision_source = DecisionSource.RESIDENT.value
    request.decided_by = actor
    request.decided_by_user_id = user_id
    request.decided_at = now
    if choice is VisitDecision.ALLOW:
        request.status = VisitRequestStatus.ALLOWED.value
        await _enter(
            session,
            request,
            now=now,
            today=local(now).date(),
            recorded_by=f"Sakin onayı: {actor}",
            host_person_id=person_id,
        )
    else:
        request.status = VisitRequestStatus.DENIED.value
    await session.flush()
    _audit(session, request)
    return request


def resident_requests_query(unit_ids: set[uuid.UUID], today: date) -> Select[VisitRequest]:
    return (
        select(VisitRequest)
        .where(
            VisitRequest.unit_id.in_(unit_ids),
            VisitRequest.method == UnannouncedMethod.APPROVAL.value,
            or_(
                VisitRequest.visit_date == today,
                VisitRequest.status == VisitRequestStatus.AWAITING.value,
            ),
        )
        .order_by(VisitRequest.created_at.desc(), VisitRequest.id.desc())
        .limit(REQUEST_LIST_LIMIT)
    )
