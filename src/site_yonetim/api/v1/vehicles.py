"""Araçlar — servis isteği 22 (YAP-05): personel listesi/ekleme/onay, ayarlar, sakin "Araçlarım".

Plaka kişisel veridir (KVKK `location_plate`): liste `units.read` izniyle görülür; güvenlik
kapı ekranı yalnız `units/lookup` eşleşme sonucunu görür (bölüm + oturan adı).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from site_yonetim.api.deps import (
    SiteContext,
    SiteContextDep,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.structure import _load_unit
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.vehicles import (
    PLATE_LIMIT_MAX,
    PLATE_LIMIT_MIN,
    VehicleKind,
    VehicleStatus,
    display_plate,
)
from site_yonetim.models import Block, Unit, UnitParty, Vehicle
from site_yonetim.models.audit import current_actor
from site_yonetim.services import vehicles as svc

router = APIRouter(prefix="/sites/{slug}", tags=["araçlar"])
UnitsRead = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_READ))]
UnitsManage = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
Paging = Annotated[PageParams, Depends()]
FORM_ERROR = "Formda düzeltilmesi gereken alanlar var."


def _error(exc: svc.VehicleRuleError) -> ApiError:
    fields = {exc.field: exc.message} if exc.field else None
    message = FORM_ERROR if exc.field else exc.message
    return ApiError(exc.status, exc.code, message, fields)


def _actor_name() -> str | None:
    actor = current_actor()
    return actor.name if actor else None


# --- Ayar ---------------------------------------------------------------------------------


class VehicleSettingsOut(BaseModel):
    plate_limit_per_unit: int
    approval_required: bool


class VehicleSettingsIn(BaseModel):
    plate_limit_per_unit: int | None = None
    approval_required: bool | None = None


@router.get("/settings/vehicles", summary="Araç ayarları")
async def get_settings(ctx: UnitsRead) -> VehicleSettingsOut:
    config = await svc.read_settings(ctx.session)
    return VehicleSettingsOut(
        plate_limit_per_unit=config.plate_limit_per_unit,
        approval_required=config.vehicle_approval_required,
    )


@router.put("/settings/vehicles", summary="Araç ayarlarını kaydet")
async def put_settings(ctx: UnitsManage, body: VehicleSettingsIn) -> Written[VehicleSettingsOut]:
    limit = body.plate_limit_per_unit
    if limit is not None and not PLATE_LIMIT_MIN <= limit <= PLATE_LIMIT_MAX:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {"plate_limit_per_unit": f"{PLATE_LIMIT_MIN}–{PLATE_LIMIT_MAX} arası."},
        )
    config = await svc.settings(ctx.session)
    if limit is not None:
        config.plate_limit_per_unit = limit
    if body.approval_required is not None:
        config.vehicle_approval_required = body.approval_required
    out = VehicleSettingsOut(
        plate_limit_per_unit=config.plate_limit_per_unit,
        approval_required=config.vehicle_approval_required,
    )
    await ctx.session.commit()
    return Written(data=out, message="Araç ayarları kaydedildi.")


# --- Personel -------------------------------------------------------------------------------


class VehicleOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    plate: str = Field(description="normalize: `34ABC123`")
    plate_display: str = Field(description="`34 ABC 123`")
    brand_model: str | None
    color: str | None
    kind: VehicleKind
    valid_until: dt.date | None
    status: VehicleStatus
    owner_person_id: uuid.UUID | None
    owner_name: str | None
    created_at: dt.datetime

    @classmethod
    def of(cls, row: svc.VehicleRow) -> VehicleOut:
        v = row.vehicle
        return cls(
            id=v.id,
            unit_id=v.unit_id,
            unit_name=row.unit_name,
            plate=v.plate,
            plate_display=display_plate(v.plate),
            brand_model=v.brand_model,
            color=v.color,
            kind=VehicleKind(v.kind),
            valid_until=v.valid_until,
            status=VehicleStatus(v.status),
            owner_person_id=v.owner_person_id,
            owner_name=row.owner_name,
            created_at=v.created_at,
        )


@router.get("/vehicles", summary="Araçlar (sayfalı, yeni üstte)")
async def list_vehicles(
    ctx: UnitsRead,
    paging: Paging,
    unit_id: uuid.UUID | None = None,
    status_filter: Annotated[VehicleStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=60, description="plaka, bölüm ya da kişi")] = None,
) -> Page[VehicleOut]:
    query = svc.list_query(unit_id=unit_id, status=status_filter, q=q)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = await ctx.session.execute(query.offset(paging.offset).limit(paging.page_size))
    return Page(
        items=[VehicleOut.of(svc.to_row(tuple(r))) for r in rows],  # type: ignore[arg-type]
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class VehicleIn(BaseModel):
    unit_id: uuid.UUID | None = Field(default=None, description="sakin ucunda isteğe bağlı")
    plate: str = Field(max_length=20)
    brand_model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=30)
    kind: VehicleKind = VehicleKind.PERMANENT
    valid_until: dt.date | None = None
    owner_person_id: uuid.UUID | None = None


class StaffVehicleIn(VehicleIn):
    force: bool = Field(
        default=False, description="plaka başka bölümde etkinse yine de ekle (personel)"
    )


@router.post("/vehicles", status_code=status.HTTP_201_CREATED, summary="Araç ekle")
async def create_vehicle(
    ctx: UnitsManage, body: StaffVehicleIn, today: TodayDep
) -> Written[VehicleOut]:
    if body.unit_id is None:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
            {"unit_id": "Bölüm seçin."},
        )  # fmt: skip
    try:
        unit, _, _ = await _load_unit(ctx, body.unit_id)
    except NotFoundError:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
            {"unit_id": "Bölüm bulunamadı."},
        ) from None  # fmt: skip
    try:
        vehicle = await svc.add(
            ctx.session,
            unit,
            _new(body),
            by_resident=False,
            force=body.force,
            created_by=_actor_name(),
            today=today,
        )
    except svc.VehicleRuleError as exc:
        raise _error(exc) from None
    row = await svc.get_row(ctx.session, vehicle.id)
    if row is None:  # pragma: no cover — az önce yazıldı
        raise NotFoundError("Araç bulunamadı.")
    await ctx.session.commit()
    return Written(
        data=VehicleOut.of(row),
        message=f"{display_plate(vehicle.plate)} {row.unit_name} bölümüne eklendi.",
    )


def _new(body: VehicleIn) -> svc.NewVehicle:
    return svc.NewVehicle(
        plate=body.plate,
        kind=body.kind,
        brand_model=body.brand_model,
        color=body.color,
        valid_until=body.valid_until,
        owner_person_id=body.owner_person_id,
    )


class VehiclePatch(BaseModel):
    brand_model: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=30)
    valid_until: dt.date | None = None
    status: VehicleStatus | None = None


@router.patch("/vehicles/{vehicle_id}", summary="Araç düzenle / onayla / pasif yap")
async def update_vehicle(
    ctx: UnitsManage, vehicle_id: uuid.UUID, body: VehiclePatch
) -> Written[VehicleOut]:
    row = await svc.get_row(ctx.session, vehicle_id)
    if row is None:
        raise NotFoundError("Araç bulunamadı.")
    vehicle = row.vehicle
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is None:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
            {"status": "Durum seçin."},
        )  # fmt: skip
    new_status = data.get("status")
    if (
        new_status is VehicleStatus.ACTIVE
        and vehicle.status == VehicleStatus.INACTIVE.value
        and vehicle.kind == VehicleKind.PERMANENT.value
    ):
        config = await svc.read_settings(ctx.session)
        open_count = await ctx.session.scalar(
            select(func.count())
            .select_from(Vehicle)
            .where(
                Vehicle.unit_id == vehicle.unit_id,
                Vehicle.kind == VehicleKind.PERMANENT.value,
                Vehicle.status != VehicleStatus.INACTIVE.value,
            )
        )
        if (open_count or 0) >= config.plate_limit_per_unit:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "plate_limit",
                f"Bu bölüm plaka sınırına ulaştı ({config.plate_limit_per_unit}).",
            )
    if "brand_model" in data:
        vehicle.brand_model = " ".join((data["brand_model"] or "").split()) or None
    if "color" in data:
        vehicle.color = " ".join((data["color"] or "").split()) or None
    if "valid_until" in data:
        if data["valid_until"] is None and vehicle.kind == VehicleKind.GUEST.value:
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR,
                {"valid_until": "Misafir aracı için geçerlilik tarihi zorunlu."},
            )  # fmt: skip
        vehicle.valid_until = data["valid_until"]
    if new_status is not None:
        vehicle.status = new_status.value
    try:
        await ctx.session.flush()
    except IntegrityError as exc:  # aynı bölümde aynı plaka ikinci kez etkinleşemez
        await ctx.session.rollback()
        raise ApiError(
            HTTPStatus.CONFLICT, "already_registered", "Bu plaka bu bölümde zaten kayıtlı."
        ) from exc
    await ctx.session.commit()
    plate = display_plate(vehicle.plate)
    if new_status is VehicleStatus.INACTIVE:
        message = f"{plate} pasif yapıldı; kapı ekranında görünmez."
    elif new_status is VehicleStatus.ACTIVE:
        message = f"{plate} etkin; kapı ekranında görünür."
    else:
        message = f"{plate} güncellendi."
    return Written(data=VehicleOut.of(row), message=message)


# --- Sakin: Araçlarım -------------------------------------------------------------------------

RESIDENT_ROLES = [PartyRole.OWNER.value, PartyRole.TENANT.value, PartyRole.RESIDENT.value]


async def resident(ctx: SiteContextDep) -> SiteContext:
    if ctx.access.person_id is None:
        raise ForbiddenError("Bu ekran yalnız sakinler içindir.")
    return ctx


Resident = Annotated[SiteContext, Depends(resident)]


async def my_units(ctx: SiteContext, today: dt.date, roles: list[str]) -> dict[uuid.UUID, str]:
    rows = await ctx.session.execute(
        select(Unit.id, Unit.number, Block.name)
        .join(UnitParty, and_(UnitParty.unit_id == Unit.id, UnitParty.site_id == Unit.site_id))
        .join(Block, and_(Block.id == Unit.block_id, Block.site_id == Unit.site_id))
        .where(
            UnitParty.person_id == ctx.access.person_id,
            UnitParty.role.in_(roles),
            UnitParty.start_date <= today,
            or_(UnitParty.end_date.is_(None), UnitParty.end_date >= today),
        )
        .order_by(Block.sort_order, Block.name, func.length(Unit.number), Unit.number)
    )
    return {i: (f"{b}-{n}" if b else n) for i, n, b in rows}


class ResidentVehiclesOut(BaseModel):
    limit: int = Field(description="bölüm başına kalıcı plaka sınırı")
    approval_required: bool
    items: list[VehicleOut]


@router.get("/resident/vehicles", summary="Sakin: Araçlarım (etkin ve onay bekleyen)")
async def resident_vehicles(ctx: Resident, today: TodayDep) -> ResidentVehiclesOut:
    units = await my_units(ctx, today, RESIDENT_ROLES)
    config = await svc.read_settings(ctx.session)
    items: list[VehicleOut] = []
    if units:
        rows = await ctx.session.execute(
            svc.list_query(unit_id=None, status=None, q=None).where(
                Vehicle.unit_id.in_(list(units)), Vehicle.status != VehicleStatus.INACTIVE.value
            )
        )
        items = [VehicleOut.of(svc.to_row(tuple(r))) for r in rows]  # type: ignore[arg-type]
    return ResidentVehiclesOut(
        limit=config.plate_limit_per_unit,
        approval_required=config.vehicle_approval_required,
        items=items,
    )


@router.post("/resident/vehicles", status_code=status.HTTP_201_CREATED, summary="Sakin: araç ekle")
async def resident_add_vehicle(
    ctx: Resident, body: VehicleIn, today: TodayDep
) -> Written[VehicleOut]:
    units = await my_units(ctx, today, RESIDENT_ROLES)
    unit_id = body.unit_id or next(iter(units), None)
    if unit_id is None or unit_id not in units:
        raise NotFoundError("Bölümünüz bulunamadı.")
    unit = await ctx.session.scalar(select(Unit).where(Unit.id == unit_id))
    if unit is None:  # pragma: no cover
        raise NotFoundError("Bölümünüz bulunamadı.")
    data = _new(body)
    data = svc.NewVehicle(
        plate=data.plate,
        kind=data.kind,
        brand_model=data.brand_model,
        color=data.color,
        valid_until=data.valid_until,
        owner_person_id=ctx.access.person_id,
    )
    try:
        vehicle = await svc.add(
            ctx.session, unit, data, by_resident=True, force=False,
            created_by=_actor_name(), today=today,
        )  # fmt: skip
    except svc.VehicleRuleError as exc:
        raise _error(exc) from None
    row = await svc.get_row(ctx.session, vehicle.id)
    if row is None:  # pragma: no cover — az önce yazıldı
        raise NotFoundError("Araç bulunamadı.")
    await ctx.session.commit()
    plate = display_plate(vehicle.plate)
    message = (
        f"{plate} kaydedildi; site yönetimi onaylayınca kapıda görünür."
        if vehicle.status == VehicleStatus.PENDING.value
        else f"{plate} eklendi; kapı ekranında görünür."
    )
    return Written(data=VehicleOut.of(row), message=message)


@router.post("/resident/vehicles/{vehicle_id}/remove", summary="Sakin: aracı kaldır (pasif)")
async def resident_remove_vehicle(
    ctx: Resident, vehicle_id: uuid.UUID, today: TodayDep
) -> Written[VehicleOut]:
    units = await my_units(ctx, today, RESIDENT_ROLES)
    row = await svc.get_row(ctx.session, vehicle_id)
    if row is None or row.vehicle.unit_id not in units:
        raise NotFoundError("Araç bulunamadı.")
    row.vehicle.status = VehicleStatus.INACTIVE.value
    await ctx.session.commit()
    return Written(
        data=VehicleOut.of(row), message=f"{display_plate(row.vehicle.plate)} kaldırıldı."
    )
