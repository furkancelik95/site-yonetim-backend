"""Sayaçlar ve okuma — Yazılım Dokümanı OPS-05 (servis isteği 57).

Çekirdek (modül yok); personel `units.manage`. Sakin (`/resident/meter-readings`) yalnız bugünkü
bölümlerinin sayaçlarını görür. Endeks ve tüketim JSON'da **metin** (`"1250.5"`).
**K14:** okumalar tahakkuka (tüketime dayalı dağıtım) henüz beslenmez — pilot sonrası.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import CurrentUserDep, SiteContext, TodayDep, require_permission
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.expenses import StoreDep, send_document
from site_yonetim.api.v1.ops_shared import fields_error, ops_error
from site_yonetim.api.v1.resident import resident
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.facility import MeterKind, MeterStatus, OpsError, ReadingStatus
from site_yonetim.domain.management import format_quantity
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Meter, MeterReading
from site_yonetim.services import meters as svc
from site_yonetim.services import requests as requests_svc
from site_yonetim.services.facility_common import PhotoSink, settings, stored_file
from site_yonetim.services.security import unit_names

router = APIRouter(prefix="/sites/{slug}", tags=["sayaç"])
Manager = Annotated[SiteContext, Depends(require_permission(Permission.UNITS_MANAGE))]
Resident = Annotated[SiteContext, Depends(resident)]
LIST_MAX = 5000


def _q(value: object) -> str | None:
    return format_quantity(value) if isinstance(value, Decimal) else None


class MeterIn(BaseModel):
    kind: str | None = Field(default=None, max_length=20)
    serial: str | None = Field(default=None, max_length=100)
    unit_id: uuid.UUID | None = None
    area: str | None = Field(default=None, max_length=200, description="ortak alan sayacında")
    unit_label: str | None = Field(default=None, max_length=20, description="boş: m³ / kWh / birim")
    multiplier: Decimal | None = Field(default=None, description='"1" — tüketim çarpanı')
    installed_on: dt.date | None = None
    status: str | None = Field(
        default=MeterStatus.ACTIVE.value, max_length=20, description="active · faulty"
    )


class MeterPatch(MeterIn):
    """Kısmi düzenleme: yalnız gönderilen alanlar değişir; gönderilmeyen alan olduğu gibi kalır
    (ör. `status` gönderilmezse sayaç durumu korunur). Bölüm verilip `area` verilmezse ortak alan
    adı temizlenir."""

    status: str | None = Field(default=None, max_length=20, description="active · faulty")


class MeterOut(BaseModel):
    id: uuid.UUID
    kind: MeterKind
    serial: str
    unit_id: uuid.UUID | None
    unit_name: str | None
    area: str | None
    unit_label: str
    multiplier: str
    installed_on: dt.date | None
    status: MeterStatus
    replaced_by: uuid.UUID | None

    @classmethod
    def of(cls, m: Meter, names: dict[uuid.UUID, str]) -> MeterOut:
        return cls(
            id=m.id,
            kind=MeterKind(m.kind),
            serial=m.serial,
            unit_id=m.unit_id,
            unit_name=names.get(m.unit_id) if m.unit_id else None,
            area=m.area,
            unit_label=m.unit_label,
            multiplier=format_quantity(m.multiplier),
            installed_on=m.installed_on,
            status=MeterStatus(m.status),
            replaced_by=m.replaced_by_id,
        )


class MeterReadingOut(BaseModel):
    id: uuid.UUID
    meter_id: uuid.UUID
    period: str = Field(description="YYYY-AA")
    value: str | None = Field(description="endeks; okunamadıysa null")
    consumption: str | None = Field(description="(endeks − önceki) × çarpan")
    prev_value: str | None
    status: ReadingStatus
    reason: str | None
    photo_id: uuid.UUID | None = Field(description="GET …/operation-photos/{id}")
    anomaly_percent: int | None = Field(description="önceki döneme göre tüketim değişimi (%)")
    by: str
    at: dt.datetime

    @classmethod
    def of(cls, r: MeterReading) -> MeterReadingOut:
        return cls(
            id=r.id,
            meter_id=r.meter_id,
            period=r.period,
            value=_q(r.value),
            consumption=_q(r.consumption),
            prev_value=_q(r.prev_value),
            status=ReadingStatus(r.status),
            reason=r.reason,
            photo_id=r.photo_file_id,
            anomaly_percent=r.anomaly_percent,
            by=r.by_name,
            at=r.created_at,
        )


async def _names(ctx: SiteContext, meters: list[Meter]) -> dict[uuid.UUID, str]:
    return await unit_names(ctx.session, {m.unit_id for m in meters if m.unit_id})


@router.get("/meters", summary="Sayaçlar (bölüm sayaçları önce, seri noya göre)")
async def list_meters(
    ctx: Manager,
    include_replaced: Annotated[bool, Query(description="değişen sayaçlar da")] = False,
) -> list[MeterOut]:
    rows = list(
        await ctx.session.scalars(
            svc.meters_query(include_replaced=include_replaced).limit(LIST_MAX)
        )
    )
    names = await _names(ctx, rows)
    return [MeterOut.of(m, names) for m in rows]


def _meter_data(body: MeterIn, current: svc.MeterData | None) -> svc.MeterData:
    sent = body.model_fields_set
    values: dict[str, Any] = {
        k: (None if getattr(body, k) is None else str(getattr(body, k)))
        if k == "multiplier"
        else getattr(body, k)
        for k in MeterIn.model_fields
    }
    if current is None:
        return svc.MeterData(**values)
    if "unit_id" in sent and values["unit_id"] is not None and "area" not in sent:
        values["area"] = None
    return svc.MeterData(**{k: values[k] if k in sent else getattr(current, k) for k in values})


@router.post("/meters", status_code=status.HTTP_201_CREATED, summary="Sayaç ekle")
async def create_meter(body: MeterIn, ctx: Manager) -> Written[MeterOut]:
    try:
        meter = await svc.save_meter(ctx.session, _meter_data(body, None), None)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = MeterOut.of(meter, await _names(ctx, [meter]))
    await ctx.session.commit()
    return Written(data=out, message="Sayaç eklendi.")


@router.patch("/meters/{meter_id}", summary="Sayacı düzenle (kısmi)")
async def update_meter(meter_id: uuid.UUID, body: MeterPatch, ctx: Manager) -> Written[MeterOut]:
    meter = await svc.get_meter(ctx.session, meter_id, lock=True)
    if meter is None:
        raise NotFoundError("Sayaç bulunamadı.")
    try:
        await svc.save_meter(ctx.session, _meter_data(body, svc.current_meter(meter)), meter)
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    out = MeterOut.of(meter, await _names(ctx, [meter]))
    await ctx.session.commit()
    return Written(data=out, message="Sayaç güncellendi.")


class MeterPeriodRowOut(BaseModel):
    meter: MeterOut
    reading: MeterReadingOut | None = Field(description="bu dönemin geçerli okuması")
    last: MeterReadingOut | None = Field(description="önceki dönemlerin son geçerli okuması")


class MeterPeriodOut(BaseModel):
    period: str
    rows: list[MeterPeriodRowOut]
    missing: int = Field(description="okunmamış ya da okunamamış sayaç")


@router.get("/meter-readings", summary="Dönem okumaları (tüm aktif sayaçlar)")
async def period_readings(
    ctx: Manager,
    today: TodayDep,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
) -> MeterPeriodOut:
    chosen = period or f"{today:%Y-%m}"
    rows = await svc.period_rows(ctx.session, chosen)
    names = await _names(ctx, [r.meter for r in rows])
    return MeterPeriodOut(
        period=chosen,
        rows=[
            MeterPeriodRowOut(
                meter=MeterOut.of(r.meter, names),
                reading=MeterReadingOut.of(r.reading) if r.reading else None,
                last=MeterReadingOut.of(r.last) if r.last else None,
            )
            for r in rows
        ],
        missing=svc.missing_count(rows),
    )


class ReplacementIn(BaseModel):
    old_final: str | None = Field(
        default=None, max_length=30, description="eski sayacın son endeksi"
    )
    new_serial: str | None = Field(default=None, max_length=100)
    new_initial: str | None = Field(
        default=None, max_length=30, description="yeni sayacın ilk endeksi"
    )


class ReadingIn(BaseModel):
    meter_id: uuid.UUID
    period: str | None = Field(default=None, max_length=7)
    value: str | None = Field(default=None, max_length=30, description='"1250.5"')
    unreadable_reason: str | None = Field(default=None, max_length=200)
    photo: str | None = Field(default=None, max_length=3_000_000, description="veri adresi")
    replacement: ReplacementIn | None = None


@router.post(
    "/meter-readings",
    status_code=status.HTTP_201_CREATED,
    summary="Okuma kaydet (endeks · okunamadı · sayaç değişimi)",
    responses={
        409: {"description": "`already_read` · `meter_replaced`"},
        422: {"description": "`index_decreased`"},
    },
)
async def create_reading(
    body: ReadingIn, ctx: Manager, current: CurrentUserDep, store: StoreDep
) -> Written[MeterReadingOut]:
    meter = await svc.get_meter(ctx.session, body.meter_id, lock=True)
    if meter is None:
        raise NotFoundError("Sayaç bulunamadı.")
    sink = PhotoSink(store)
    replacement = (
        svc.Replacement(
            body.replacement.old_final, body.replacement.new_serial, body.replacement.new_initial
        )
        if body.replacement
        else None
    )
    try:
        result = await svc.read(
            ctx.session,
            sink,
            meter,
            svc.ReadingData(
                body.period, body.value, body.unreadable_reason, body.photo, replacement
            ),
            by=svc.Reader(current.user.full_name, current.user.id),
        )
        reading = result.reading
        await ctx.session.refresh(reading, ["created_at"])
        out = MeterReadingOut.of(reading)
        await ctx.session.commit()
    except FormFieldError as exc:
        sink.discard()
        raise fields_error(exc) from exc
    except OpsError as exc:
        sink.discard()
        raise ops_error(exc) from exc
    except BaseException:
        sink.discard()
        raise
    if reading.status == ReadingStatus.UNREADABLE.value:
        message = "Okunamadı olarak kaydedildi; eksik okumalarda listelenir."
    elif reading.status == ReadingStatus.ANOMALY.value:
        message = (
            f"Okuma kaydedildi; tüketim önceki döneme göre %{reading.anomaly_percent} değişti — "
            "yönetici onayı bekliyor."
        )
    elif result.new_meter is not None:
        message = f"Sayaç değişimi kaydedildi; yeni sayaç {result.new_meter.serial} açıldı."
    else:
        message = "Okuma kaydedildi."
    return Written(data=out, message=message)


async def _decide(
    ctx: SiteContext, reading_id: uuid.UUID, approve: bool, by: str
) -> MeterReadingOut:
    reading = await svc.get_reading(ctx.session, reading_id)
    if reading is None:
        raise NotFoundError("Okuma bulunamadı.")
    try:
        await svc.decide(ctx.session, reading, approve=approve, by=by)
    except OpsError as exc:
        raise ops_error(exc) from exc
    out = MeterReadingOut.of(reading)
    await ctx.session.commit()
    return out


@router.post("/meter-readings/{reading_id}/approve", summary="Anormal okumayı onayla")
async def approve_reading(
    reading_id: uuid.UUID, ctx: Manager, current: CurrentUserDep
) -> Written[MeterReadingOut]:
    out = await _decide(ctx, reading_id, True, current.user.full_name)
    return Written(data=out, message="Anormal okuma onaylandı.")


@router.post("/meter-readings/{reading_id}/reread", summary="Anormal okuma için yeniden okuma iste")
async def reread_reading(
    reading_id: uuid.UUID, ctx: Manager, current: CurrentUserDep
) -> Written[MeterReadingOut]:
    out = await _decide(ctx, reading_id, False, current.user.full_name)
    return Written(data=out, message="Yeniden okuma istendi; sayaç yeniden okunacak.")


class OperationSettingsOut(BaseModel):
    meter_anomaly_percent: int = Field(description="±%X sapma anormal (varsayılan 50)")
    meter_photo_required: bool = Field(description="okumada sayaç fotoğrafı zorunlu")


class OperationSettingsIn(BaseModel):
    meter_anomaly_percent: int | None = None
    meter_photo_required: bool | None = None


@router.get("/operation-settings", summary="Sayaç okuma ayarları")
async def get_operation_settings(ctx: Manager) -> OperationSettingsOut:
    config = await settings(ctx.session)
    out = OperationSettingsOut(
        meter_anomaly_percent=config.meter_anomaly_percent,
        meter_photo_required=config.meter_photo_required,
    )
    await ctx.session.commit()
    return out


@router.put("/operation-settings", summary="Sayaç okuma ayarlarını değiştir")
async def put_operation_settings(
    body: OperationSettingsIn, ctx: Manager
) -> Written[OperationSettingsOut]:
    try:
        await svc.set_settings(
            ctx.session,
            anomaly_percent=body.meter_anomaly_percent,
            photo_required=body.meter_photo_required,
        )
    except FormFieldError as exc:
        raise fields_error(exc) from exc
    config = await settings(ctx.session)
    out = OperationSettingsOut(
        meter_anomaly_percent=config.meter_anomaly_percent,
        meter_photo_required=config.meter_photo_required,
    )
    await ctx.session.commit()
    return Written(data=out, message="Sayaç ayarları kaydedildi.")


# --- Sakin --------------------------------------------------------------------------


class ResidentMeterOut(BaseModel):
    meter: MeterOut
    readings: list[MeterReadingOut] = Field(description="yeniden eskiye (son 24 dönem)")


async def _my_unit_ids(ctx: SiteContext, today: dt.date) -> list[uuid.UUID]:
    person = ctx.access.person_id
    if person is None:  # pragma: no cover - `resident` bağımlılığı engeller
        return []
    return sorted(await requests_svc.person_unit_ids(ctx.session, person, today))


@router.get("/resident/meter-readings", summary="Sakin — bölümlerimin sayaçları ve okumaları")
async def my_readings(ctx: Resident, today: TodayDep) -> list[ResidentMeterOut]:
    found = await svc.readings_for_units(ctx.session, await _my_unit_ids(ctx, today))
    names = await _names(ctx, [m for m, _ in found])
    return [
        ResidentMeterOut(
            meter=MeterOut.of(m, names), readings=[MeterReadingOut.of(r) for r in readings[:24]]
        )
        for m, readings in found
    ]


@router.get(
    "/resident/meter-readings/{reading_id}/photo",
    summary="Sakin — kendi okumamın fotoğrafı",
    response_class=Response,
    responses={200: {"content": {"image/*": {}}}},
)
async def my_reading_photo(
    reading_id: uuid.UUID, ctx: Resident, today: TodayDep, store: StoreDep
) -> Response:
    units = await _my_unit_ids(ctx, today)
    row = await ctx.session.scalar(
        select(MeterReading.photo_file_id)
        .join(Meter, (Meter.id == MeterReading.meter_id) & (Meter.site_id == MeterReading.site_id))
        .where(
            MeterReading.id == reading_id,
            Meter.unit_id.in_(units),
            MeterReading.status != ReadingStatus.REREAD.value,  # sakin listesinde de yok
        )
    )
    photo = await stored_file(ctx.session, row) if row else None
    if photo is None:
        raise NotFoundError("Fotoğraf bulunamadı.")
    return send_document(photo, store)
