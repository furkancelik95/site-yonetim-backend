"""Otopark: park yerleri ve tarihçeli tahsis — PM kararı K30 (Yazılım Dokümanı OTP-01).

Çekirdek yapı verisi (modül kapısı yok). Okuma `units.read` ya da `security.visitors` (güvenlik
okur); yazma `units.manage`. Sakin "Park yerim": `resident/parking-spaces`.
Ücretli yerin ücreti dönem dışı ücret modülü gelince cariye bağlanır (şimdilik yalnız işaret).
"""

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, SiteContext, SiteContextDep, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.gatehouse import my_unit_ids, rule_error
from site_yonetim.core.errors import ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.parking import ParkingKind, check_space
from site_yonetim.models import ParkingAllocation, ParkingSpace
from site_yonetim.services import parking as svc
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}", tags=["otopark"])
Paging = Annotated[PageParams, Depends()]


def _any(*permissions: Permission) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if not any(ctx.access.can(p) for p in permissions):
            raise ForbiddenError
        return ctx

    return dependency


Reader = Annotated[SiteContext, Depends(_any(Permission.UNITS_READ, Permission.SECURITY_VISITORS))]
Manager = Annotated[SiteContext, Depends(_any(Permission.UNITS_MANAGE))]


class ParkingHolderOut(BaseModel):
    allocation_id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    start_date: dt.date
    end_date: dt.date | None


class ParkingSpaceOut(BaseModel):
    id: uuid.UUID
    code: str
    kind: ParkingKind
    location: str | None
    is_paid: bool
    is_active: bool
    note: str | None
    current: ParkingHolderOut | None = Field(description="bugünkü tahsis (yoksa boş)")


async def _spaces_out(
    ctx: SiteContext, rows: list[ParkingSpace], today: dt.date
) -> list[ParkingSpaceOut]:
    holders = await svc.holders(ctx.session, [s.id for s in rows], today)
    out = []
    for s in rows:
        h = holders.get(s.id)
        out.append(
            ParkingSpaceOut(
                id=s.id,
                code=s.code,
                kind=ParkingKind(s.kind),
                location=s.location,
                is_paid=s.is_paid,
                is_active=s.is_active,
                note=s.note,
                current=ParkingHolderOut(
                    allocation_id=h.allocation_id,
                    unit_id=h.unit_id,
                    unit_name=h.unit_name,
                    start_date=h.start_date,
                    end_date=h.end_date,
                )
                if h
                else None,
            )
        )
    return out


@router.get("/parking-spaces", summary="Park yerleri (koda göre)")
async def list_spaces(
    ctx: Reader,
    paging: Paging,
    today: TodayDep,
    kind: ParkingKind | None = None,
    q: Annotated[str | None, Query(max_length=20, description="kod araması")] = None,
    unit_id: Annotated[uuid.UUID | None, Query(description="bugün bu bölüme tahsisliler")] = None,
) -> Page[ParkingSpaceOut]:
    query = svc.spaces_query(kind=kind, q=q, unit_id=unit_id, today=today)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _spaces_out(ctx, rows, today),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class ParkingSpaceIn(BaseModel):
    code: str | None = Field(default=None, max_length=100)
    kind: str | None = Field(default=None, description="standard · disabled · ev · guest")
    location: str | None = Field(default=None, max_length=300)
    is_paid: bool = False
    is_active: bool = True
    note: str | None = Field(default=None, max_length=500)


async def _save(
    ctx: SiteContext, space: ParkingSpace | None, body: ParkingSpaceIn, today: dt.date
) -> ParkingSpaceOut:
    try:
        form = check_space(
            code=body.code,
            kind=body.kind,
            location=body.location,
            is_paid=body.is_paid,
            note=body.note,
            taken=await svc.taken_codes(ctx.session, space.id if space else None),
        )
    except FormFieldError as exc:
        raise rule_error(exc) from exc
    saved = await svc.save_space(ctx.session, space, form, is_active=body.is_active)
    await ctx.session.commit()
    [out] = await _spaces_out(ctx, [saved], today)
    return out


@router.post("/parking-spaces", status_code=status.HTTP_201_CREATED, summary="Park yeri ekle")
async def create_space(
    ctx: Manager, body: ParkingSpaceIn, today: TodayDep
) -> Written[ParkingSpaceOut]:
    out = await _save(ctx, None, body, today)
    return Written(data=out, message=f"{out.code} park yeri eklendi.")


@router.patch("/parking-spaces/{space_id}", summary="Park yerini düzenle")
async def update_space(
    space_id: uuid.UUID, ctx: Manager, body: ParkingSpaceIn, today: TodayDep
) -> Written[ParkingSpaceOut]:
    space = await svc.get_space(ctx.session, space_id)
    if space is None:
        raise NotFoundError("Park yeri bulunamadı.")
    out = await _save(ctx, space, body, today)
    return Written(data=out, message=f"{out.code} güncellendi.")


class ParkingAllocationOut(BaseModel):
    id: uuid.UUID
    space_id: uuid.UUID
    space_code: str
    unit_id: uuid.UUID
    unit_name: str
    start_date: dt.date
    end_date: dt.date | None
    is_current: bool
    note: str | None
    created_by: str
    ended_by: str | None


async def _allocations_out(
    ctx: SiteContext, rows: list[ParkingAllocation], today: dt.date
) -> list[ParkingAllocationOut]:
    names = await unit_names(ctx.session, {a.unit_id for a in rows})
    space_ids = {a.space_id for a in rows}
    codes: dict[uuid.UUID, str] = {}
    if space_ids:
        rows_ = await ctx.session.execute(
            select(ParkingSpace.id, ParkingSpace.code).where(ParkingSpace.id.in_(space_ids))
        )
        codes = dict(rows_.all())
    return [
        ParkingAllocationOut(
            id=a.id,
            space_id=a.space_id,
            space_code=codes.get(a.space_id, "—"),
            unit_id=a.unit_id,
            unit_name=names.get(a.unit_id, "—"),
            start_date=a.start_date,
            end_date=a.end_date,
            is_current=a.start_date <= today and (a.end_date is None or a.end_date >= today),
            note=a.note,
            created_by=a.created_by,
            ended_by=a.ended_by,
        )
        for a in rows
    ]


@router.get("/parking-allocations", summary="Tahsis geçmişi (yeni üstte)")
async def list_allocations(
    ctx: Reader,
    paging: Paging,
    today: TodayDep,
    space_id: uuid.UUID | None = None,
    unit_id: uuid.UUID | None = None,
) -> Page[ParkingAllocationOut]:
    query = svc.allocations_query(space_id=space_id, unit_id=unit_id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _allocations_out(ctx, rows, today),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class ParkingAllocationIn(BaseModel):
    unit_id: uuid.UUID
    start_date: dt.date | None = None
    end_date: dt.date | None = Field(default=None, description="boşsa süresiz")
    note: str | None = Field(default=None, max_length=500)


@router.post(
    "/parking-spaces/{space_id}/allocations",
    status_code=status.HTTP_201_CREATED,
    summary="Park yerini bölüme tahsis et",
)
async def allocate(
    space_id: uuid.UUID,
    ctx: Manager,
    body: ParkingAllocationIn,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[ParkingAllocationOut]:
    space = await svc.get_space(ctx.session, space_id, lock=True)
    if space is None:
        raise NotFoundError("Park yeri bulunamadı.")
    try:
        row = await svc.allocate(
            ctx.session,
            space,
            unit_id=body.unit_id,
            start=body.start_date,
            end=body.end_date,
            note=body.note,
            actor=current.user.full_name,
        )
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _allocations_out(ctx, [row], today)
    return Written(data=out, message=f"{out.space_code}, {out.unit_name} bölümüne tahsis edildi.")


class ParkingAllocationEndIn(BaseModel):
    end_date: dt.date | None = None


@router.post("/parking-allocations/{allocation_id}/end", summary="Tahsisi bitir")
async def end_allocation(
    allocation_id: uuid.UUID,
    ctx: Manager,
    body: ParkingAllocationEndIn,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[ParkingAllocationOut]:
    row = await svc.get_allocation(ctx.session, allocation_id)
    if row is None:
        raise NotFoundError("Tahsis bulunamadı.")
    try:
        await svc.end_allocation(ctx.session, row, end=body.end_date, actor=current.user.full_name)
    except (OperationRuleError, FormFieldError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    [out] = await _allocations_out(ctx, [row], today)
    return Written(
        data=out, message=f"{out.space_code} tahsisi {out.end_date:%d.%m.%Y} itibarıyla bitti."
    )


@router.get("/resident/parking-spaces", summary="Park yerim (bugünkü tahsisler)")
async def my_spaces(ctx: SiteContextDep, today: TodayDep) -> list[ParkingSpaceOut]:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    units = await my_unit_ids(ctx, today)
    rows: list[ParkingSpace] = []
    for unit in sorted(units):
        rows += list(
            await ctx.session.scalars(
                svc.spaces_query(kind=None, q=None, unit_id=unit, today=today)
            )
        )
    return await _spaces_out(ctx, rows, today)
