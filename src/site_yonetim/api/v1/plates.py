"""Plaka sorgu, giriş-çıkış kaydı, otopark ihlali — servis isteği 43 (GUV-04, OTP-02).
Modül `visitors` (kapalıysa 404). Okuma `security.visitors` ya da `units.read` (yönetici);
yazma `security.visitors`. Görevli telefon görmez; yalnız bölüm ve ad. Plaka sorgusu hız sınırlı.

Kayıtlı araç sonucu P02 araç tablosu (servis isteği 22) gelince bağlanır (`services/plates.py`).
"""

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, FactoryDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.gatehouse import access, gate_rate_limit, rule_error
from site_yonetim.domain.access import Permission
from site_yonetim.domain.gatehouse import (
    GateDirection,
    PlateResult,
    ViolationNotice,
    display_plate,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.models import GateLog, ParkingViolation
from site_yonetim.services import gatehouse as gate_svc
from site_yonetim.services import plates as svc
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])
Paging = Annotated[PageParams, Depends()]
Reader = Annotated[
    SiteContext,
    Depends(access((ModuleKey.VISITORS,), (Permission.SECURITY_VISITORS, Permission.UNITS_READ))),
]
Guard = Annotated[
    SiteContext, Depends(access((ModuleKey.VISITORS,), (Permission.SECURITY_VISITORS,)))
]
LOOKUP_LIMIT = 120


def _shown(plate: str | None) -> str:
    return display_plate(plate) if plate else "—"


class PlateLookupOut(BaseModel):
    plate: str = Field(description="normalize: 34ABC123")
    plate_display: str = Field(description="34 ABC 123")
    result: PlateResult
    unit_name: str | None
    owner_name: str | None
    valid_until: dt.date | None
    detail: str | None


@router.get("/plates/lookup", summary="Plaka sorgula")
async def lookup(
    ctx: Reader,
    current: CurrentUserDep,
    now: NowDep,
    factory: FactoryDep,
    plate: Annotated[str, Query(max_length=20, description="büyük/küçük harf ve boşluk duyarsız")],
) -> PlateLookupOut:
    await gate_rate_limit(factory, ctx, current, now, kind="plate", limit=LOOKUP_LIMIT)
    try:
        info = await svc.lookup(ctx.session, plate, now)
    except FormFieldError as exc:
        raise rule_error(exc) from exc
    return PlateLookupOut(
        plate=info.plate,
        plate_display=info.plate_display,
        result=info.result,
        unit_name=info.unit_name,
        owner_name=info.owner_name,
        valid_until=info.valid_until,
        detail=info.detail,
    )


class GateLogOut(BaseModel):
    id: uuid.UUID
    plate: str | None = Field(description="saklama süresi dolunca boş")
    plate_display: str
    result: PlateResult
    detail: str | None
    unit_name: str | None
    gate_id: uuid.UUID | None
    direction: GateDirection
    by: str
    at: dt.datetime


async def _logs_out(ctx: SiteContext, rows: list[GateLog]) -> list[GateLogOut]:
    names = await unit_names(ctx.session, {r.unit_id for r in rows if r.unit_id})
    return [
        GateLogOut(
            id=r.id,
            plate=r.plate,
            plate_display=_shown(r.plate),
            result=PlateResult(r.result),
            detail=r.detail,
            unit_name=names.get(r.unit_id) if r.unit_id else None,
            gate_id=r.gate_id,
            direction=GateDirection(r.direction),
            by=r.recorded_by,
            at=r.at,
        )
        for r in rows
    ]


@router.get("/gate-logs", summary="Plaka giriş-çıkış kayıtları (gün; yeni üstte)")
async def list_gate_logs(
    ctx: Reader,
    paging: Paging,
    today: TodayDep,
    day: Annotated[dt.date | None, Query(alias="date", description="boşsa bugün")] = None,
) -> Page[GateLogOut]:
    query = svc.gate_logs_query(day or today)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _logs_out(ctx, rows), page=paging.page, page_size=paging.page_size, total=total
    )


class GateLogIn(BaseModel):
    plate: str | None = Field(default=None, max_length=20)
    direction: Literal["in", "out"] = "in"
    gate_id: uuid.UUID | None = None


@router.post("/gate-logs", status_code=status.HTTP_201_CREATED, summary="Plaka giriş/çıkış kaydı")
async def create_gate_log(
    ctx: Guard, body: GateLogIn, current: CurrentUserDep, now: NowDep
) -> Written[GateLogOut]:
    try:
        await gate_svc.require_gate(ctx.session, body.gate_id)
        log, _ = await svc.record_gate_log(
            ctx.session,
            plate=body.plate,
            direction=GateDirection(body.direction),
            gate_id=body.gate_id,
            now=now,
            actor=current.user.full_name,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _logs_out(ctx, [log])
    verb = "girişi" if out.direction is GateDirection.IN else "çıkışı"
    return Written(data=out, message=f"{out.plate_display} {verb} kaydedildi.")


class ParkingViolationOut(BaseModel):
    id: uuid.UUID
    plate: str | None
    plate_display: str
    location: str
    note: str | None
    result: PlateResult
    detail: str | None
    unit_name: str | None
    notified: ViolationNotice
    by: str
    at: dt.datetime


async def _violations_out(
    ctx: SiteContext, rows: list[ParkingViolation]
) -> list[ParkingViolationOut]:
    names = await unit_names(ctx.session, {r.unit_id for r in rows if r.unit_id})
    return [
        ParkingViolationOut(
            id=r.id,
            plate=r.plate,
            plate_display=_shown(r.plate),
            location=r.location,
            note=r.note,
            result=PlateResult(r.result),
            detail=r.detail,
            unit_name=names.get(r.unit_id) if r.unit_id else None,
            notified=ViolationNotice(r.notified),
            by=r.recorded_by,
            at=r.occurred_at,
        )
        for r in rows
    ]


@router.get("/parking-violations", summary="Otopark ihlalleri (yeni üstte)")
async def list_violations(ctx: Reader, paging: Paging) -> Page[ParkingViolationOut]:
    query = svc.violations_query()
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _violations_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class ParkingViolationIn(BaseModel):
    plate: str | None = Field(default=None, max_length=20)
    location: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=1000)


@router.post(
    "/parking-violations", status_code=status.HTTP_201_CREATED, summary="Otopark ihlali kaydet"
)
async def create_violation(
    ctx: Guard, body: ParkingViolationIn, current: CurrentUserDep, now: NowDep
) -> Written[ParkingViolationOut]:
    try:
        violation = await svc.record_violation(
            ctx.session,
            ctx.site,
            plate=body.plate,
            location=body.location,
            note=body.note,
            now=now,
            actor=current.user.full_name,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _violations_out(ctx, [violation])
    if out.notified is ViolationNotice.RESIDENT:
        message = f"İhlal kaydedildi; {out.unit_name} sakinlerine bildirildi."
    else:
        message = "İhlal kaydedildi; plaka bir bölüme bağlı değil, yönetime bildirildi."
    return Written(data=out, message=message)
