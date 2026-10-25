"""Personelin kendi işleri — PM kararı **K29** (Yazılım Dokümanı IK-02 "Vardiyam", IK-03 izin
talebi, IK-04 vardiya giriş-çıkışı). Siteye bağlı: `/sites/{slug}/me/…`.

- Yalnız giriş hesabı bir personel kartına bağlı kullanıcı; kartı yoksa 403. İzin gerekmez —
  her şey kişinin kendi kartıyla süzülür (başkasının kaydı "yok"tur, 404).
- Vardiyam: yalnız **yayınlanmış** haftalar. Giriş-çıkış: sitenin doğrulama yöntemine göre
  (QR kodu / o anki konum); sürekli konum takibi yok, konum saklanmaz.
- İzin talebi: onay kuralı varsa onay kutusuna, yoksa İK'nın onayına düşer. Kendi bekleyen
  talebini geri çekebilir.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.leaves import (
    Actor,
    LeaveRequestOut,
    created_message,
    leave_out,
    rule_error,
)
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.shifts import ShiftTemplateOut
from site_yonetim.api.v1.staff import Module
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.staffing import (
    AttendanceKind,
    AttendanceMethod,
    LeaveStatus,
    week_start,
)
from site_yonetim.models import AttendanceEvent, StaffMember
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import leaves as leaves_svc
from site_yonetim.services import shifts as shifts_svc
from site_yonetim.services import staff as staff_svc
from site_yonetim.services import timesheets as ts_svc

router = APIRouter(prefix="/sites/{slug}/me", tags=["personel (kendi)"])
Paging = Annotated[PageParams, Depends()]
RANGE_MAX = 42


class OwnCard:
    def __init__(self, ctx: SiteContext, staff: StaffMember) -> None:
        self.ctx = ctx
        self.staff = staff


async def _own(ctx: Module, current: CurrentUserDep) -> OwnCard:
    staff = await staff_svc.own_card(ctx.session, current.user.id)
    if staff is None:
        raise ForbiddenError("Hesabınıza bağlı personel kartı yok; site yönetimine başvurun.")
    return OwnCard(ctx, staff)


Own = Annotated[OwnCard, Depends(_own)]


class AttendanceEventOut(BaseModel):
    id: uuid.UUID
    kind: AttendanceKind
    occurred_at: dt.datetime
    received_at: dt.datetime
    verified: bool

    @classmethod
    def of(cls, e: AttendanceEvent) -> AttendanceEventOut:
        return cls(
            id=e.id,
            kind=AttendanceKind(e.kind),
            occurred_at=e.occurred_at,
            received_at=e.received_at,
            verified=e.verified,
        )


class StaffOwnShiftOut(BaseModel):
    id: uuid.UUID
    date: dt.date
    template: ShiftTemplateOut
    changed_after_publish: bool
    events: list[AttendanceEventOut]


class StaffOwnShiftsOut(BaseModel):
    staff_id: uuid.UUID
    full_name: str
    position: str
    attendance_method: AttendanceMethod
    shifts: list[StaffOwnShiftOut]


@router.get("/shifts", summary="Vardiyam — yayınlanmış haftalar (boş → bu hafta + gelecek)")
async def own_shifts(
    own: Own,
    today: TodayDep,
    from_: Annotated[dt.date | None, Query(alias="from")] = None,
    to: dt.date | None = None,
) -> StaffOwnShiftsOut:
    first = from_ or week_start(today)
    last = to or first + dt.timedelta(days=13)
    if last < first or (last - first).days >= RANGE_MAX:
        raise form_error(FormFieldError({"to": f"Aralık en çok {RANGE_MAX} gün olmalı."}))
    session = own.ctx.session
    found = await shifts_svc.own_shifts(session, own.staff.id, first, last)
    events = await ts_svc.own_events(session, own.staff.id, [x.assignment.id for x in found])
    by_assignment: dict[uuid.UUID, list[AttendanceEvent]] = {}
    for e in events:
        if e.assignment_id is not None:
            by_assignment.setdefault(e.assignment_id, []).append(e)
    conf = await ts_svc.settings(session)
    return StaffOwnShiftsOut(
        staff_id=own.staff.id,
        full_name=own.staff.full_name,
        position=own.staff.position,
        attendance_method=AttendanceMethod(conf.method) if conf else AttendanceMethod.NONE,
        shifts=[
            StaffOwnShiftOut(
                id=x.assignment.id,
                date=x.assignment.work_date,
                template=ShiftTemplateOut.of(x.template),
                changed_after_publish=x.assignment.changed_after_publish,
                events=[AttendanceEventOut.of(e) for e in by_assignment.get(x.assignment.id, [])],
            )
            for x in found
        ],
    )


class AttendanceEventIn(BaseModel):
    kind: str | None = Field(default=None, max_length=10, description="in · out")
    assignment_id: uuid.UUID | None = Field(
        default=None, description="boşsa en yakın vardiya seçilir"
    )
    occurred_at: dt.datetime | None = Field(
        default=None, description="çevrimdışı kayıtta cihaz saati; boşsa sunucu saati"
    )
    code: str | None = Field(default=None, max_length=64, description="QR yönteminde kulübe kodu")
    latitude: Decimal | None = Field(default=None, max_digits=12, decimal_places=8)
    longitude: Decimal | None = Field(default=None, max_digits=12, decimal_places=8)


class AttendanceRecordedOut(AttendanceEventOut):
    assignment_id: uuid.UUID | None
    clock_skew_minutes: int = Field(description="cihaz ile sunucu saati farkı")


@router.post(
    "/attendance-events",
    status_code=status.HTTP_201_CREATED,
    summary="Vardiyaya başla / bitir (QR ya da o anki konum)",
)
async def record_event(
    body: AttendanceEventIn, own: Own, now: NowDep, today: TodayDep
) -> Written[AttendanceRecordedOut]:
    try:
        rec = await ts_svc.record_event(
            own.ctx.session,
            own.staff,
            ts_svc.EventData(
                body.kind,
                body.assignment_id,
                body.occurred_at,
                body.code,
                body.latitude,
                body.longitude,
            ),
            now=now,
            today=today,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except ts_svc.TimesheetRuleError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    out = AttendanceRecordedOut(
        **AttendanceEventOut.of(rec.event).model_dump(),
        assignment_id=rec.event.assignment_id,
        clock_skew_minutes=rec.skew_minutes,
    )
    await own.ctx.session.commit()
    return Written(data=out, message=rec.message)


@router.get("/leave-requests", summary="İzin taleplerim (yeni üstte)")
async def own_leaves(own: Own, paging: Paging) -> Page[LeaveRequestOut]:
    query = leaves_svc.list_query(None, own.staff.id)
    session = own.ctx.session
    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await leave_out(session, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class StaffOwnLeaveIn(BaseModel):
    type: str | None = Field(default=None, max_length=20)
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    note: str | None = Field(default=None, max_length=1000)


@router.post("/leave-requests", status_code=status.HTTP_201_CREATED, summary="İzin talep et")
async def create_own_leave(
    body: StaffOwnLeaveIn, own: Own, actor: Actor, today: TodayDep, now: NowDep
) -> Written[LeaveRequestOut]:
    session = own.ctx.session
    try:
        leave = await leaves_svc.create(
            session,
            own.ctx.site,
            leaves_svc.LeaveData(own.staff.id, body.type, body.from_, body.to, body.note),
            source="staff",
            actor=actor,
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except (leaves_svc.LeaveRuleError, approvals_svc.ApprovalError) as exc:
        raise rule_error(exc) from exc
    (out,) = await leave_out(session, [leave])
    await session.commit()
    return Written(data=out, message=created_message(leave, out.clashing_shifts))


@router.post("/leave-requests/{leave_id}/cancel", summary="Bekleyen talebimi geri çek")
async def cancel_own_leave(
    leave_id: uuid.UUID, own: Own, actor: Actor, now: NowDep
) -> Written[LeaveRequestOut]:
    session = own.ctx.session
    leave = await leaves_svc.get(session, leave_id)
    if leave is None or leave.staff_id != own.staff.id:
        raise NotFoundError("İzin talebi bulunamadı.")
    if leave.status != LeaveStatus.PENDING.value:
        raise ApiError(
            409,
            "invalid_status",
            "Yalnız bekleyen talep geri çekilir; onaylı izin için İK'ya başvurun.",
        )
    try:
        await leaves_svc.cancel(session, leave, actor=actor, now=now)
    except (leaves_svc.LeaveRuleError, approvals_svc.ApprovalError) as exc:
        raise rule_error(exc) from exc
    await session.refresh(leave)
    (out,) = await leave_out(session, [leave])
    await session.commit()
    return Written(data=out, message="İzin talebi geri çekildi.")
