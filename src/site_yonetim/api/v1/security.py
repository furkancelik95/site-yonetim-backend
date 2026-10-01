"""Kapı işleri — docs/06 §2.12 (modüller: `packages`, `visitors`; kapalıysa 404).

- Kargo: `security.packages`. Teslim kodu **güvenliğe dönmez**; sakin `resident/packages`'ta
  görür. Teslimde kod doğrulanır (sabit zamanlı karşılaştırma).
- Ziyaretçi: `security.visitors`. Kayıt (hemen giriş ya da beklenen), giriş, çıkış.
- Daire arama: `security.*` — yalnız bölüm adı ve oturan adları; borç ve telefon yok (docs/05).

Bu router yapı router'ından **önce** kaydedilir: `/units/lookup`, `/units/{unit_id}` ile karışmasın.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_module,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import PackageStatus, VisitorKind, VisitorStatus
from site_yonetim.models import Package, Visitor
from site_yonetim.services import security as svc

router = APIRouter(prefix="/sites/{slug}", tags=["güvenlik"])
Paging = Annotated[PageParams, Depends()]


async def _packages(
    ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.PACKAGES))],
) -> SiteContext:
    if not ctx.access.can(Permission.SECURITY_PACKAGES):
        raise ForbiddenError
    return ctx


async def _visitors(
    ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.VISITORS))],
) -> SiteContext:
    if not ctx.access.can(Permission.SECURITY_VISITORS):
        raise ForbiddenError
    return ctx


Packages = Annotated[SiteContext, Depends(_packages)]
Visitors = Annotated[SiteContext, Depends(_visitors)]


def _error(exc: OperationRuleError) -> ApiError:
    if exc.conflict:
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


# --- Kargo ----------------------------------------------------------------------------


class PackageOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    person_id: uuid.UUID | None
    carrier: str | None
    status: PackageStatus
    received_at: dt.datetime
    received_by: str | None
    delivered_at: dt.datetime | None
    delivered_by: str | None
    delivered_to: str | None
    note: str | None


async def packages_out(ctx: SiteContext, rows: list[Package]) -> list[PackageOut]:
    names = await svc.unit_names(ctx.session, {p.unit_id for p in rows})
    return [
        PackageOut(
            id=p.id,
            unit_id=p.unit_id,
            unit_name=names.get(p.unit_id, "—"),
            person_id=p.person_id,
            carrier=p.carrier,
            status=PackageStatus(p.status),
            received_at=p.received_at,
            received_by=p.received_by,
            delivered_at=p.delivered_at,
            delivered_by=p.delivered_by,
            delivered_to=p.delivered_to,
            note=p.note,
        )
        for p in rows
    ]


@router.get("/packages", summary="Kargolar (yeni üstte)")
async def list_packages(
    ctx: Packages,
    paging: Paging,
    status_filter: Annotated[PackageStatus | None, Query(alias="status")] = None,
    unit_id: uuid.UUID | None = None,
) -> Page[PackageOut]:
    query = svc.packages_query(status=status_filter, unit_ids={unit_id} if unit_id else None)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await packages_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class PackageIn(BaseModel):
    unit_id: uuid.UUID
    person_id: uuid.UUID | None = Field(default=None, description="alıcı (isteğe bağlı)")
    carrier: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=500)


@router.post("/packages", status_code=status.HTTP_201_CREATED, summary="Kargo kaydet")
async def receive_package(
    ctx: Packages, body: PackageIn, current: CurrentUserDep, now: NowDep
) -> Written[PackageOut]:
    try:
        package = await svc.receive_package(
            ctx.session, unit_id=body.unit_id, person_id=body.person_id, carrier=body.carrier,
            note=body.note, now=now, received_by=current.user.full_name,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    [item] = await packages_out(ctx, [package])
    return Written(
        data=item,
        message=f"{item.unit_name} için kargo kaydedildi. Teslim kodu sakinin ekranında görünür.",
    )


class DeliverIn(BaseModel):
    pickup_code: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    delivered_to: str = Field(min_length=2, max_length=80)


@router.post("/packages/{package_id}/deliver", summary="Teslim et (kodla)")
async def deliver_package(
    package_id: uuid.UUID, ctx: Packages, body: DeliverIn, current: CurrentUserDep, now: NowDep
) -> Written[PackageOut]:
    package = await svc.get_package(ctx.session, package_id)
    if package is None:
        raise NotFoundError("Kargo bulunamadı.")
    try:
        await svc.deliver_package(
            ctx.session, package, pickup_code=body.pickup_code, delivered_to=body.delivered_to,
            now=now, delivered_by=current.user.full_name,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    [item] = await packages_out(ctx, [package])
    return Written(data=item, message=f"Kargo {item.delivered_to} kişisine teslim edildi.")


# --- Ziyaretçi --------------------------------------------------------------------------


class VisitorOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    host_person_id: uuid.UUID | None
    full_name: str
    phone: str | None
    plate_number: str | None
    kind: VisitorKind
    expected_on: dt.date | None
    visit_date: dt.date
    status: VisitorStatus
    entered_at: dt.datetime | None
    exited_at: dt.datetime | None
    recorded_by: str | None
    note: str | None


async def _visitors_out(ctx: SiteContext, rows: list[Visitor]) -> list[VisitorOut]:
    names = await svc.unit_names(ctx.session, {v.unit_id for v in rows})
    return [
        VisitorOut(
            id=v.id,
            unit_id=v.unit_id,
            unit_name=names.get(v.unit_id, "—"),
            host_person_id=v.host_person_id,
            full_name=v.full_name,
            phone=v.phone,
            plate_number=v.plate_number,
            kind=VisitorKind(v.kind),
            expected_on=v.expected_on,
            visit_date=v.visit_date,
            status=VisitorStatus(v.status),
            entered_at=v.entered_at,
            exited_at=v.exited_at,
            recorded_by=v.recorded_by,
            note=v.note,
        )
        for v in rows
    ]


@router.get("/visitors", summary="Günün ziyaretçileri")
async def list_visitors(
    ctx: Visitors,
    paging: Paging,
    today: TodayDep,
    day: Annotated[dt.date | None, Query(alias="date", description="boşsa bugün")] = None,
    status_filter: Annotated[VisitorStatus | None, Query(alias="status")] = None,
) -> Page[VisitorOut]:
    query = svc.visitors_query(day or today, status_filter)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    return Page(
        items=await _visitors_out(ctx, rows),
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class VisitorIn(BaseModel):
    unit_id: uuid.UUID
    full_name: str = Field(min_length=2, max_length=80)
    kind: VisitorKind = VisitorKind.GUEST
    host_person_id: uuid.UUID | None = None
    phone: str | None = Field(default=None, max_length=30)
    plate_number: str | None = Field(default=None, max_length=20)
    expected_on: dt.date | None = Field(default=None, description="ileri tarihse beklenen kaydı")
    note: str | None = Field(default=None, max_length=500)
    enter_now: bool = Field(default=True, description="bugünkü ziyaretçi hemen içeri alınır")


@router.post("/visitors", status_code=status.HTTP_201_CREATED, summary="Ziyaretçi kaydet")
async def record_visitor(
    ctx: Visitors, body: VisitorIn, current: CurrentUserDep, now: NowDep, today: TodayDep
) -> Written[VisitorOut]:
    try:
        visitor = await svc.record_visitor(
            ctx.session,
            svc.NewVisitor(
                unit_id=body.unit_id, full_name=body.full_name, kind=body.kind,
                host_person_id=body.host_person_id, phone=body.phone,
                plate_number=body.plate_number, expected_on=body.expected_on, note=body.note,
                enter_now=body.enter_now,
            ),
            now=now, today=today, recorded_by=current.user.full_name,
        )  # fmt: skip
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    [item] = await _visitors_out(ctx, [visitor])
    state = "içeri alındı" if item.status is VisitorStatus.ENTERED else "beklenen olarak kaydedildi"
    return Written(data=item, message=f"{item.full_name} ({item.unit_name}) {state}.")


async def _visitor(ctx: SiteContext, visitor_id: uuid.UUID) -> Visitor:
    visitor = await svc.get_visitor(ctx.session, visitor_id)
    if visitor is None:
        raise NotFoundError("Ziyaretçi bulunamadı.")
    return visitor


@router.post("/visitors/{visitor_id}/enter", summary="Giriş")
async def visitor_enter(visitor_id: uuid.UUID, ctx: Visitors, now: NowDep) -> Written[VisitorOut]:
    visitor = await _visitor(ctx, visitor_id)
    try:
        await svc.enter(ctx.session, visitor, now)
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    [item] = await _visitors_out(ctx, [visitor])
    return Written(data=item, message=f"{item.full_name} içeri alındı.")


@router.post("/visitors/{visitor_id}/exit", summary="Çıkış")
async def visitor_exit(visitor_id: uuid.UUID, ctx: Visitors, now: NowDep) -> Written[VisitorOut]:
    visitor = await _visitor(ctx, visitor_id)
    try:
        await svc.exit_(ctx.session, visitor, now)
    except OperationRuleError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    [item] = await _visitors_out(ctx, [visitor])
    return Written(data=item, message=f"{item.full_name} çıkış yaptı.")


# --- Daire arama ------------------------------------------------------------------------


class LookupOut(BaseModel):
    unit_id: uuid.UUID
    unit_name: str
    occupants: list[str] = Field(description="oturan adları (kiracı/oturan, yoksa malik)")


@router.get("/units/lookup", summary="Güvenlik için daire arama (en fazla 20)")
async def units_lookup(
    ctx: SiteContextDep,
    today: TodayDep,
    q: Annotated[str, Query(min_length=1, max_length=60, description="`A-12`, `12` ya da ad")],
) -> list[LookupOut]:
    if not (ModuleKey.VISITORS in ctx.modules or ModuleKey.PACKAGES in ctx.modules):
        raise NotFoundError
    if not (
        ctx.access.can(Permission.SECURITY_VISITORS) or ctx.access.can(Permission.SECURITY_PACKAGES)
    ):
        raise ForbiddenError
    return [
        LookupOut(unit_id=r.unit_id, unit_name=r.unit_name, occupants=r.occupants)
        for r in await svc.lookup(ctx.session, q, today)
    ]
