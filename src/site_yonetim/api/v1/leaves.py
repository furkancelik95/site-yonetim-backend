"""İzinler — servis isteği 37 (Yazılım Dokümanı IK-03).

Modül `staff` (kapalıysa 404). Liste, kayıt ve bakiye `staff.manage` — raporlu izin sağlık verisi
olabileceğinden izin listesi `people.read`'e açılmaz. Karar: sitede "İzin talebi" onay kuralı
varsa onay mekanizması (onaycı olan herkes; izin ekranındaki düğme de ONY'ye yazar), yoksa
`staff.manage`. Personelin kendi talebi `/me/leave-requests` (K29).
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.staff import Manager, Module
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.staffing import LeaveStatus, LeaveType
from site_yonetim.models import ApprovalRequest, LeaveRequest, StaffMember
from site_yonetim.services import approvals as approvals_svc
from site_yonetim.services import leaves as svc

router = APIRouter(prefix="/sites/{slug}", tags=["izin"])
Paging = Annotated[PageParams, Depends()]


def person(current: CurrentUserDep) -> approvals_svc.Person:
    return approvals_svc.Person(current.user.id, current.user.full_name)


Actor = Annotated[approvals_svc.Person, Depends(person)]


def rule_error(exc: svc.LeaveRuleError | approvals_svc.ApprovalError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def leave_form_error(exc: FormFieldError) -> ApiError:
    fields = dict(exc.fields)
    if "comment" in fields:
        fields["reason"] = fields.pop("comment")
    return form_error(FormFieldError(fields))


class LeaveRequestIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    staff_id: uuid.UUID | None = None
    type: str | None = Field(default=None, max_length=20)
    from_: dt.date | None = Field(default=None, alias="from")
    to: dt.date | None = None
    note: str | None = Field(
        default=None, max_length=1000, description="raporlu izinde yazılmaz (tanı tutulmaz)"
    )


class LeaveRequestOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: uuid.UUID
    staff_id: uuid.UUID
    staff_name: str
    type: LeaveType
    from_: dt.date = Field(alias="from")
    to: dt.date
    days: int = Field(description="takvim günü")
    note: str | None
    status: LeaveStatus
    source: str = Field(description="manager: İK adına · staff: personelin kendisi (K29)")
    created_by: str
    created_at: dt.datetime
    decided_by: str | None
    decided_at: dt.datetime | None
    reject_reason: str | None
    clashing_shifts: int = Field(description="izin tarihlerindeki vardiya sayısı")
    approval_request_id: uuid.UUID | None = Field(
        description="onay kuralına bağlıysa onay talebi (karar onay kutusunda)"
    )
    approval_number: int | None


async def leave_out(session: AsyncSession, rows: list[LeaveRequest]) -> list[LeaveRequestOut]:
    clash = await svc.clashing_shifts(session, rows)
    names = dict(
        (
            await session.execute(
                select(StaffMember.id, StaffMember.full_name).where(
                    StaffMember.id.in_({r.staff_id for r in rows})
                )
            )
        ).all()
    )
    request_ids = {r.approval_request_id for r in rows if r.approval_request_id}
    numbers = (
        dict(
            (
                await session.execute(
                    select(ApprovalRequest.id, ApprovalRequest.number).where(
                        ApprovalRequest.id.in_(request_ids)
                    )
                )
            ).all()
        )
        if request_ids
        else {}
    )
    return [
        LeaveRequestOut(
            id=r.id,
            staff_id=r.staff_id,
            staff_name=names.get(r.staff_id, "—"),
            type=LeaveType(r.leave_type),
            from_=r.date_from,
            to=r.date_to,
            days=r.days,
            note=r.leave_note,
            status=LeaveStatus(r.status),
            source=r.source,
            created_by=r.created_by_name,
            created_at=r.created_at,
            decided_by=r.decided_by_name,
            decided_at=r.decided_at,
            reject_reason=r.reject_reason,
            clashing_shifts=clash.get(r.id, 0),
            approval_request_id=r.approval_request_id,
            approval_number=numbers.get(r.approval_request_id) if r.approval_request_id else None,
        )
        for r in rows
    ]


@router.get("/leave-requests", summary="İzin talepleri (yeni üstte)")
async def list_leaves(
    ctx: Manager,
    paging: Paging,
    status_: Annotated[LeaveStatus | None, Query(alias="status")] = None,
    staff_id: uuid.UUID | None = None,
) -> Page[LeaveRequestOut]:
    query = svc.list_query(status_, staff_id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await leave_out(ctx.session, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


def created_message(leave: LeaveRequest, clash: int) -> str:
    text = "İzin talebi kaydedildi"
    if clash:
        text += f"; bu tarihlerde {clash} vardiyası var"
    if leave.approval_request_id is not None:
        return text + ". Onay kuralına göre onay kutusuna gönderildi."
    return text + ". Onay bekliyor."


@router.post("/leave-requests", status_code=status.HTTP_201_CREATED, summary="İzin kaydet")
async def create_leave(
    body: LeaveRequestIn, ctx: Manager, actor: Actor, today: TodayDep, now: NowDep
) -> Written[LeaveRequestOut]:
    try:
        leave = await svc.create(
            ctx.session,
            ctx.site,
            svc.LeaveData(body.staff_id, body.type, body.from_, body.to, body.note),
            source="manager",
            actor=actor,
            today=today,
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except (svc.LeaveRuleError, approvals_svc.ApprovalError) as exc:
        raise rule_error(exc) from exc
    (out,) = await leave_out(ctx.session, [leave])
    await ctx.session.commit()
    return Written(data=out, message=created_message(leave, out.clashing_shifts))


class LeaveRejectIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


async def _leave(ctx: SiteContext, leave_id: uuid.UUID) -> LeaveRequest:
    leave = await svc.get(ctx.session, leave_id)
    if leave is None:
        raise NotFoundError("İzin talebi bulunamadı.")
    return leave


def _decided_message(leave: LeaveRequest, approve: bool) -> str:
    if leave.status == LeaveStatus.APPROVED.value:
        return "İzin onaylandı; vardiya planına ve puantaja yansır."
    if leave.status == LeaveStatus.REJECTED.value:
        return "İzin reddedildi."
    if approve:
        return "Onayınız kaydedildi; onay zinciri sürüyor."
    return "Kararınız kaydedildi."  # pragma: no cover — ret tek adımda kapanır


async def _decide(
    leave_id: uuid.UUID,
    ctx: SiteContext,
    actor: approvals_svc.Person,
    now: dt.datetime,
    *,
    approve: bool,
    reason: str | None,
) -> Written[LeaveRequestOut]:
    leave = await _leave(ctx, leave_id)
    try:
        await svc.decide(
            ctx.session,
            ctx.site,
            leave,
            approve=approve,
            reason=reason,
            actor=actor,
            can_manage=ctx.access.can(Permission.STAFF_MANAGE),
            now=now,
        )
    except FormFieldError as exc:
        raise leave_form_error(exc) from exc
    except (svc.LeaveRuleError, approvals_svc.ApprovalError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.refresh(leave)
    (out,) = await leave_out(ctx.session, [leave])
    await ctx.session.commit()
    return Written(data=out, message=_decided_message(leave, approve))


@router.post("/leave-requests/{leave_id}/approve", summary="İzni onayla")
async def approve_leave(
    leave_id: uuid.UUID, ctx: Module, actor: Actor, now: NowDep
) -> Written[LeaveRequestOut]:
    return await _decide(leave_id, ctx, actor, now, approve=True, reason=None)


@router.post("/leave-requests/{leave_id}/reject", summary="İzni reddet (gerekçe zorunlu)")
async def reject_leave(
    leave_id: uuid.UUID, body: LeaveRejectIn, ctx: Module, actor: Actor, now: NowDep
) -> Written[LeaveRequestOut]:
    return await _decide(leave_id, ctx, actor, now, approve=False, reason=body.reason)


@router.post("/leave-requests/{leave_id}/cancel", summary="İzni iptal et / talebi geri çek")
async def cancel_leave(
    leave_id: uuid.UUID, ctx: Manager, actor: Actor, now: NowDep
) -> Written[LeaveRequestOut]:
    leave = await _leave(ctx, leave_id)
    try:
        await svc.cancel(ctx.session, leave, actor=actor, now=now)
    except (svc.LeaveRuleError, approvals_svc.ApprovalError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.refresh(leave)
    (out,) = await leave_out(ctx.session, [leave])
    await ctx.session.commit()
    return Written(data=out, message="İzin iptal edildi.")


class LeaveBalanceOut(BaseModel):
    staff_id: uuid.UUID
    staff_name: str
    used: dict[str, int] = Field(description="annual · excuse · sick · unpaid → onaylı gün")


@router.get("/leave-balances", summary="Bu yıl kullanılan izinler (bilgi amaçlı; dizi)")
async def leave_balances(
    ctx: Manager, today: TodayDep, year: Annotated[int | None, Query(ge=2000, le=2100)] = None
) -> list[LeaveBalanceOut]:
    rows = await svc.balances(ctx.session, year or today.year, today)
    return [
        LeaveBalanceOut(staff_id=b.staff_id, staff_name=b.staff_name, used=b.used) for b in rows
    ]
