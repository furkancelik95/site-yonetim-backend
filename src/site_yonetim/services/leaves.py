"""İzin talebi ve onayı — servis isteği 37 (Yazılım Dokümanı IK-03). Açık site kapsamında;
transaction'ı çağıran yönetir.

- Talebi personel kendisi (K29, `/me/leave-requests`) ya da İK sorumlusu personel adına açar.
- **Onay mekanizması (IK-03 adım 4, Bölüm 6):** sitede "İzin talebi" işlem tipi için onay kuralı
  varsa talep onay kutusuna düşer; karar ONY'de verilir (izin ekranındaki Onayla/Reddet de aynı
  kararı ONY üzerinden yazar) ve onaylanınca yürütücü izni "onaylandı" yapar. Kural yoksa basit
  akış: `staff.manage` izni olan kişi karar verir (varsayılan onaycı İK sorumlusu — belgede;
  sistemde İK rolü yok, `staff.manage` Yönetici'de). Kimse kendi iznini onaylayamaz.
- Gün sayısı takvim günü. Aynı kişinin bekleyen/onaylı izniyle çakışma 409. Puantajı kesinleşmiş
  günü kapsayan izin kaydedilemez, onaylanamaz, iptal edilemez.
- **Raporlu izinde tanı tutulmaz:** açıklama alanı kapalı (veritabanı kısıtı da var); rapor belgesi
  belge modülüyle (gizli kategori) bağlanacak. İzin türü denetim kaydında maskeli.
- Yıllık izin bakiyesi bilgi amaçlı (bu yıl kullanılan onaylı gün); bordro hesabı yapılmaz.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.approvals import ActionType, ApprovalStatus, Decision
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.staffing import (
    ACTIVE_LEAVE,
    LEAVE_DAYS_MAX,
    LEAVE_LABELS,
    LEAVE_NOTE_MAX,
    REASON_MAX,
    REASON_MIN,
    LeaveStatus,
    LeaveType,
    days_between,
)
from site_yonetim.models import (
    ApprovalDecision,
    ApprovalRequest,
    LeaveRequest,
    ShiftAssignment,
    Site,
    StaffMember,
)
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.approvals import ExecutionError, Person
from site_yonetim.services.shifts import final_days


class LeaveRuleError(Exception):
    """İş kuralı ihlali: `status` 403/409, `code` makine için, mesaj Türkçe."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


FINAL_MESSAGE = "İzin günlerinden birinin puantajı kesinleşti; {}."


async def _check_final(session: AsyncSession, leave: LeaveRequest, action: str) -> None:
    if await final_days(session, leave.date_from, leave.date_to):
        raise LeaveRuleError(409, "timesheet_final", FINAL_MESSAGE.format(action))


async def _overlap(
    session: AsyncSession,
    staff_id: uuid.UUID,
    first: date,
    last: date,
    *,
    exclude: uuid.UUID | None,
    statuses: frozenset[LeaveStatus] = ACTIVE_LEAVE,
) -> bool:
    found = await session.scalar(
        select(LeaveRequest.id)
        .where(
            LeaveRequest.staff_id == staff_id,
            LeaveRequest.status.in_([s.value for s in statuses]),
            LeaveRequest.date_from <= last,
            LeaveRequest.date_to >= first,
            LeaveRequest.id != (exclude or uuid.UUID(int=0)),
        )
        .limit(1)
    )
    return found is not None


async def clashing_shifts(
    session: AsyncSession, leaves: list[LeaveRequest]
) -> dict[uuid.UUID, int]:
    """İzin tarihlerindeki vardiya sayısı (onaycıya gösterilir; IK-03 adım 4)."""
    if not leaves:
        return {}
    rows = await session.execute(
        select(LeaveRequest.id, func.count(ShiftAssignment.id))
        .join(
            ShiftAssignment,
            (ShiftAssignment.staff_id == LeaveRequest.staff_id)
            & ShiftAssignment.work_date.between(LeaveRequest.date_from, LeaveRequest.date_to),
        )
        .where(LeaveRequest.id.in_([x.id for x in leaves]))
        .group_by(LeaveRequest.id)
    )
    return {i: int(c) for i, c in rows.all()}


@dataclass(frozen=True, slots=True)
class LeaveData:
    staff_id: uuid.UUID | None
    leave_type: str | None
    first: date | None
    last: date | None
    note: str | None


async def _validated(
    session: AsyncSession, data: LeaveData
) -> tuple[StaffMember, LeaveType, date, date, str | None]:
    errors: dict[str, str] = {}
    staff = await session.get(StaffMember, data.staff_id) if data.staff_id else None
    if staff is None:
        errors["staff_id"] = "Personel seçin."
    kind = None
    try:
        kind = LeaveType(data.leave_type or "")
    except ValueError:
        errors["type"] = "İzin türünü seçin."
    if data.first is None:
        errors["from"] = "Başlangıç tarihini girin."
    if data.last is None:
        errors["to"] = "Bitiş tarihini girin."
    elif data.first is not None and data.last < data.first:
        errors["to"] = "Bitiş başlangıçtan önce olamaz."
    elif data.first is not None and days_between(data.first, data.last) > LEAVE_DAYS_MAX:
        errors["to"] = f"Bir kayıt en çok {LEAVE_DAYS_MAX} gün olabilir."
    if (
        staff is not None
        and data.first is not None
        and data.last is not None
        and (
            data.first < staff.start_date
            or (staff.end_date is not None and data.last > staff.end_date)
        )
    ):
        errors["from"] = "İzin, personelin çalışma tarihleri içinde olmalı."
    note = " ".join((data.note or "").split())[:LEAVE_NOTE_MAX] or None
    if kind is LeaveType.SICK and note is not None:
        errors["note"] = "Raporlu izinde açıklama yazılmaz; tanı bilgisi tutulmaz."
    if errors or staff is None or kind is None or data.first is None or data.last is None:
        raise FormFieldError(errors)
    return staff, kind, data.first, data.last, note


async def rule_applies(session: AsyncSession, today: date) -> bool:
    """Sitede "İzin talebi" için yürürlükte onay kuralı var mı (yoksa basit akış)."""
    version = await approvals_svc.match_rule(
        session, ActionType.LEAVE_REQUEST, amount=None, source=None, today=today
    )
    return version is not None


async def create(
    session: AsyncSession,
    site: Site,
    data: LeaveData,
    *,
    source: str,
    actor: Person,
    today: date,
    now: datetime,
) -> LeaveRequest:
    staff, kind, first, last, note = await _validated(session, data)
    if await _overlap(session, staff.id, first, last, exclude=None):
        raise LeaveRuleError(409, "leave_overlap", "Bu tarihlerde kişinin başka bir izni var.")
    if await final_days(session, first, last):
        raise LeaveRuleError(409, "timesheet_final", FINAL_MESSAGE.format("kayıt yapılamaz"))
    leave = LeaveRequest(
        id=uuid.uuid7(),
        staff_id=staff.id,
        leave_type=kind.value,
        date_from=first,
        date_to=last,
        days=days_between(first, last),
        leave_note=note,
        status=LeaveStatus.PENDING.value,
        source=source,
        created_by=actor.user_id,
        created_by_name=actor.name,
    )
    session.add(leave)
    await session.flush()
    if await rule_applies(session, today):
        clash = (await clashing_shifts(session, [leave])).get(leave.id, 0)
        request = await approvals_svc.request_approval(
            session,
            site,
            approvals_svc.NewRequest(
                action_type=ActionType.LEAVE_REQUEST,
                title=f"İzin talebi — {staff.full_name}, {first:%d.%m.%Y}–{last:%d.%m.%Y}",
                summary={
                    "staff_name": staff.full_name,
                    "leave_type": kind.value,
                    "from": first.isoformat(),
                    "to": last.isoformat(),
                    "days": leave.days,
                    "clashing_shifts": clash,
                },
                payload={"leave_id": str(leave.id)},
                subject_type="leave_requests",
                subject_id=leave.id,
            ),
            requester=actor,
            today=today,
            now=now,
        )
        leave.approval_request_id = request.id
        await session.flush()
    return leave


async def get(session: AsyncSession, leave_id: uuid.UUID) -> LeaveRequest | None:
    found: LeaveRequest | None = await session.scalar(
        select(LeaveRequest).where(LeaveRequest.id == leave_id).with_for_update()
    )
    return found


async def _notify_staff(
    session: AsyncSession, leave: LeaveRequest, kind: str, title: str, *, skip: uuid.UUID | None
) -> None:
    staff = await session.get(StaffMember, leave.staff_id)
    if staff is None or staff.user_id is None or staff.user_id == skip:
        return
    await notify_svc.notify(
        session,
        [staff.user_id],
        notify_svc.Message(
            category=NotificationCategory.OPERATIONS,
            kind=kind,
            title=title,
            body=f"{leave.date_from:%d.%m.%Y} – {leave.date_to:%d.%m.%Y} ({leave.days} gün)",
            link_entity="leave_requests",
            link_id=leave.id,
        ),
        dedupe_key=f"leave:{leave.id}:{kind}",
    )


def _reason(text: str | None) -> str | None:
    value = " ".join((text or "").split())[:REASON_MAX]
    return value if len(value) >= REASON_MIN else None


async def decide(
    session: AsyncSession,
    site: Site,
    leave: LeaveRequest,
    *,
    approve: bool,
    reason: str | None,
    actor: Person,
    can_manage: bool,
    now: datetime,
) -> None:
    """Onay kuralı varsa kararı ONY'ye yazar (yetki ve sıra orada denetlenir); yoksa basit akış
    (`staff.manage`). İzin durumu ONY'nin yürütücü/kapatıcısıyla güncellenir."""
    if leave.status != LeaveStatus.PENDING.value:
        raise LeaveRuleError(409, "invalid_status", "Yalnız bekleyen talep karara bağlanır.")
    text = _reason(reason)
    if not approve and text is None:
        raise FormFieldError({"reason": "Ret gerekçesi yazın."})
    staff = await session.get(StaffMember, leave.staff_id)
    if staff is not None and staff.user_id == actor.user_id:
        raise LeaveRuleError(403, "own_request", "Kendi izninizi onaylayamazsınız.")
    if leave.approval_request_id is not None:
        request = await approvals_svc.get_request(session, leave.approval_request_id, lock=True)
        if request is None:  # pragma: no cover — FK
            raise LeaveRuleError(409, "invalid_status", "Onay talebi bulunamadı.")
        await approvals_svc.decide(
            session,
            site,
            request,
            decider=actor,
            decision=Decision.APPROVE if approve else Decision.REJECT,
            comment=text,
            now=now,
        )
        return
    if not can_manage:
        raise LeaveRuleError(403, "forbidden", "Bu işlem için yetkiniz yok.")
    if approve:
        await _check_final(session, leave, "onaylanamaz")
    leave.status = (LeaveStatus.APPROVED if approve else LeaveStatus.REJECTED).value
    leave.decided_by_name = actor.name
    leave.decided_at = now
    leave.reject_reason = None if approve else text
    await session.flush()
    await _notify_staff(
        session,
        leave,
        "leave.approved" if approve else "leave.rejected",
        "İzniniz onaylandı" if approve else "İzniniz reddedildi",
        skip=actor.user_id,
    )


async def cancel(
    session: AsyncSession, leave: LeaveRequest, *, actor: Person, now: datetime
) -> None:
    """Onaylı izin iptal edilir (kesin gün yoksa). Bekleyen talep: onay kuralına bağlıysa yalnız
    talebi açan geri çeker (ONY), değilse doğrudan iptal."""
    status = LeaveStatus(leave.status)
    if status in {LeaveStatus.REJECTED, LeaveStatus.CANCELLED}:
        raise LeaveRuleError(409, "invalid_status", "Bu talep zaten kapanmış.")
    if status is LeaveStatus.APPROVED:
        await _check_final(session, leave, "iptal edilemez")
    if status is LeaveStatus.PENDING and leave.approval_request_id is not None:
        request = await approvals_svc.get_request(session, leave.approval_request_id, lock=True)
        if request is not None and ApprovalStatus(request.status) in {
            ApprovalStatus.PENDING,
            ApprovalStatus.REVISION_REQUESTED,
        }:
            await approvals_svc.withdraw(session, request, user_id=actor.user_id, now=now)
            return
    leave.status = LeaveStatus.CANCELLED.value
    leave.decided_by_name = actor.name
    leave.decided_at = now
    await session.flush()
    if status is LeaveStatus.APPROVED:
        await _notify_staff(
            session, leave, "leave.cancelled", "İzniniz iptal edildi", skip=actor.user_id
        )


# --- Onay mekanizması yürütücüsü ve kapatıcısı ---------------------------------------------


async def _leave_of(session: AsyncSession, request: ApprovalRequest) -> LeaveRequest | None:
    if request.subject_id is None:
        return None
    return await get(session, request.subject_id)


async def _decider_names(
    session: AsyncSession, request: ApprovalRequest, decision: Decision
) -> list[str]:
    rows = await session.execute(
        select(ApprovalDecision.user_name, ApprovalDecision.comment)
        .where(
            ApprovalDecision.request_id == request.id,
            ApprovalDecision.revision == request.revision,
            ApprovalDecision.decision == decision.value,
        )
        .order_by(ApprovalDecision.created_at)
    )
    return [f"{n}\t{c or ''}" for n, c in rows.all()]


async def _execute(session: AsyncSession, request: ApprovalRequest) -> None:
    """ONY onayı tamamlandı: izin onaylanır (veri bu arada geçersizse yürütülemez)."""
    leave = await _leave_of(session, request)
    if leave is None or leave.status != LeaveStatus.PENDING.value:
        raise ExecutionError("İzin talebi artık beklemede değil.")
    if await final_days(session, leave.date_from, leave.date_to):
        raise ExecutionError("İzin günlerinden birinin puantajı kesinleşti.")
    if await _overlap(
        session,
        leave.staff_id,
        leave.date_from,
        leave.date_to,
        exclude=leave.id,
        statuses=frozenset({LeaveStatus.APPROVED}),
    ):
        raise ExecutionError("Bu tarihlerde kişinin onaylı başka bir izni var.")
    names = [x.split("\t")[0] for x in await _decider_names(session, request, Decision.APPROVE)]
    leave.status = LeaveStatus.APPROVED.value
    leave.decided_by_name = ", ".join(names) or None
    leave.decided_at = request.decided_at
    await session.flush()
    await _notify_staff(
        session, leave, "leave.approved", "İzniniz onaylandı", skip=request.requested_by
    )


async def _close(session: AsyncSession, request: ApprovalRequest) -> None:
    """ONY'de reddedildi ya da geri çekildi: izin kaydı kapanır."""
    leave = await _leave_of(session, request)
    if leave is None or leave.status != LeaveStatus.PENDING.value:
        return
    leave.decided_at = request.decided_at
    if ApprovalStatus(request.status) is ApprovalStatus.REJECTED:
        last = (await _decider_names(session, request, Decision.REJECT))[-1:]
        name, _, comment = (last[0] if last else "\t").partition("\t")
        leave.status = LeaveStatus.REJECTED.value
        leave.decided_by_name = name or None
        leave.reject_reason = comment or None
        await session.flush()
        await _notify_staff(
            session, leave, "leave.rejected", "İzniniz reddedildi", skip=request.requested_by
        )
        return
    leave.status = LeaveStatus.CANCELLED.value
    leave.decided_by_name = request.requested_by_name
    await session.flush()


approvals_svc.register_executor(ActionType.LEAVE_REQUEST, _execute)
approvals_svc.register_closer(ActionType.LEAVE_REQUEST, _close)


# --- Liste ve bakiye ----------------------------------------------------------------------


def list_query(
    status: LeaveStatus | None, staff_id: uuid.UUID | None = None
) -> Select[LeaveRequest]:
    query = select(LeaveRequest)
    if status is not None:
        query = query.where(LeaveRequest.status == status.value)
    if staff_id is not None:
        query = query.where(LeaveRequest.staff_id == staff_id)
    return query.order_by(LeaveRequest.created_at.desc(), LeaveRequest.id.desc())


@dataclass(frozen=True, slots=True)
class Balance:
    staff_id: uuid.UUID
    staff_name: str
    used: dict[str, int]


async def balances(session: AsyncSession, year: int, today: date) -> list[Balance]:
    """Bu yıl onaylı izin günleri (yıl sınırına kırpılmış), tür bazında — veritabanında toplanır."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    days = func.least(LeaveRequest.date_to, last) - func.greatest(LeaveRequest.date_from, first) + 1
    rows = await session.execute(
        select(LeaveRequest.staff_id, LeaveRequest.leave_type, func.sum(days))
        .where(
            LeaveRequest.status == LeaveStatus.APPROVED.value,
            LeaveRequest.date_from <= last,
            LeaveRequest.date_to >= first,
        )
        .group_by(LeaveRequest.staff_id, LeaveRequest.leave_type)
    )
    used: dict[uuid.UUID, dict[str, int]] = {}
    for staff_id, kind, total in rows.all():
        used.setdefault(staff_id, {})[kind] = int(total or 0)
    staff = await session.scalars(
        select(StaffMember)
        .where((StaffMember.end_date.is_(None)) | (StaffMember.end_date >= first))
        .order_by(StaffMember.full_name, StaffMember.id)
    )
    return [
        Balance(s.id, s.full_name, {k.value: used.get(s.id, {}).get(k.value, 0) for k in LeaveType})
        for s in staff
        if s.start_date <= max(today, last)
    ]


def label(kind: str) -> str:
    return LEAVE_LABELS[LeaveType(kind)]
