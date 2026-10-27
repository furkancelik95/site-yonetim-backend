"""Sayaçlar ve okuma — Yazılım Dokümanı OPS-05 (servis isteği 57). Açık site kapsamında;
transaction'ı çağıran yönetir.

- Seri no aktif sayaçlar içinde benzersiz; sayaç bölüme ya da ortak alana bağlı.
- Tüketim = (endeks − önceki endeks) × çarpan. **Endeks düşemez** (sayaç değişimi hariç).
- Tüketim önceki dönem tüketiminden site ayarındaki eşikten (varsayılan %50) fazla saparsa
  `anomaly`: yönetici onaylar ya da yeniden okuma ister (eski kayıt `reread`). Okunamadı → endeks
  yok, neden zorunlu; eksik okumalarda sayılır ve sonra okunabilir (eski kayıt `reread` olur).
- Sayaç değişimi: eski sayacın son endeksi okunur, sayaç `replaced` olur; yeni sayaç ilk endeksiyle
  açılır ve aynı dönemde (varsa) ilk okumasını alır.
- **K14:** merkezi ısınma tüketimi pilot sonrası — okumalar burada tutulur, tahakkuk motoruna
  (`by_meter_consumption`) henüz beslenmez.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.facility import (
    MeterKind,
    MeterStatus,
    OpsError,
    ReadingStatus,
    clean,
    conflict,
    consumption,
    deviation_percent,
    is_anomaly,
    unit_label_for,
    valid_period,
)
from site_yonetim.domain.management import format_quantity_tr, parse_quantity
from site_yonetim.domain.members import FormFieldError
from site_yonetim.models import Meter, MeterReading, Unit
from site_yonetim.services.facility_common import PhotoSink, save_photo, settings

MULTIPLIER_MAX = Decimal(10000)
VALID_STATUSES = (ReadingStatus.OK.value, ReadingStatus.APPROVED.value, ReadingStatus.ANOMALY.value)


@dataclass(frozen=True, slots=True)
class MeterData:
    kind: str | None
    serial: str | None
    unit_id: uuid.UUID | None
    area: str | None
    unit_label: str | None
    multiplier: str | None
    installed_on: date | None
    status: str | None


def current_meter(m: Meter) -> MeterData:
    return MeterData(
        kind=m.kind,
        serial=m.serial,
        unit_id=m.unit_id,
        area=m.area,
        unit_label=m.unit_label,
        multiplier=str(m.multiplier),
        installed_on=m.installed_on,
        status=m.status,
    )


def _multiplier(value: str | None, errors: dict[str, str]) -> Decimal:
    try:
        number = Decimal(str(value if value not in (None, "") else "1").replace(",", "."))
    except ArithmeticError:
        number = Decimal(0)
    if not number.is_finite() or number <= 0 or number > MULTIPLIER_MAX:
        errors["multiplier"] = "Çarpan sıfırdan büyük bir sayı olmalı."
        return Decimal(1)
    return number.quantize(Decimal("0.0001"))


async def save_meter(session: AsyncSession, data: MeterData, current: Meter | None) -> Meter:
    errors: dict[str, str] = {}
    if data.kind not in {k.value for k in MeterKind}:
        errors["kind"] = "Tip seçin."
    serial = clean(data.serial, 40)
    if not serial:
        errors["serial"] = "Seri no yazın."
    elif await session.scalar(
        select(Meter.id).where(
            Meter.serial == serial,
            Meter.status != MeterStatus.REPLACED.value,
            Meter.id != (current.id if current else uuid.UUID(int=0)),
        )
    ):
        errors["serial"] = "Bu seri no kayıtlı."
    area = clean(data.area, 80) or None
    if data.unit_id is not None:
        if await session.scalar(select(Unit.id).where(Unit.id == data.unit_id)) is None:
            errors["unit_id"] = "Bölüm bulunamadı."
        area = None
    elif area is None:
        errors["unit_id"] = "Bölüm ya da ortak alan seçin."
    if data.status not in {s.value for s in MeterStatus} - {MeterStatus.REPLACED.value} and not (
        current is not None and data.status == current.status
    ):
        errors["status"] = "Durumu seçin (aktif ya da arızalı)."
    multiplier = _multiplier(data.multiplier, errors)
    if errors:
        raise FormFieldError(errors)
    kind = MeterKind(str(data.kind))
    meter = current or Meter(id=uuid.uuid7())
    meter.kind = kind.value
    meter.serial = serial
    meter.unit_id = data.unit_id
    meter.area = area
    meter.unit_label = clean(data.unit_label, 12) or unit_label_for(kind)
    meter.multiplier = multiplier
    meter.installed_on = data.installed_on
    meter.status = str(data.status)
    if current is None:
        session.add(meter)
    await session.flush()
    return meter


async def get_meter(
    session: AsyncSession, meter_id: uuid.UUID, *, lock: bool = False
) -> Meter | None:
    query = select(Meter).where(Meter.id == meter_id)
    found: Meter | None = await session.scalar(query.with_for_update() if lock else query)
    return found


def meters_query(*, include_replaced: bool = False) -> Select[Meter]:
    query = select(Meter)
    if not include_replaced:
        query = query.where(Meter.status != MeterStatus.REPLACED.value)
    return query.order_by(Meter.unit_id.is_(None), Meter.serial, Meter.id)


async def _previous(session: AsyncSession, meter_id: uuid.UUID, period: str) -> MeterReading | None:
    """Önceki dönemlerin son geçerli (endeksli) okuması."""
    found: MeterReading | None = await session.scalar(
        select(MeterReading)
        .where(
            MeterReading.meter_id == meter_id,
            MeterReading.period < period,
            MeterReading.status.in_(VALID_STATUSES),
        )
        .order_by(MeterReading.period.desc(), MeterReading.created_at.desc())
        .limit(1)
    )
    return found


async def _current(session: AsyncSession, meter_id: uuid.UUID, period: str) -> MeterReading | None:
    found: MeterReading | None = await session.scalar(
        select(MeterReading)
        .where(
            MeterReading.meter_id == meter_id,
            MeterReading.period == period,
            MeterReading.status != ReadingStatus.REREAD.value,
        )
        .with_for_update()
    )
    return found


@dataclass(frozen=True, slots=True)
class Replacement:
    old_final: str | None
    new_serial: str | None
    new_initial: str | None


@dataclass(frozen=True, slots=True)
class ReadingData:
    period: str | None
    value: str | None
    unreadable_reason: str | None
    photo: str | None
    replacement: Replacement | None


@dataclass(frozen=True, slots=True)
class Reader:
    name: str
    user_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class ReadResult:
    reading: MeterReading
    new_meter: Meter | None = None


def _index(value: str | None, field: str, errors: dict[str, str]) -> Decimal:
    try:
        return parse_quantity(value, allow_zero=True)
    except ValueError:
        errors[field] = "Endeksi girin (en çok 3 ondalık)."
        return Decimal(0)


async def read(
    session: AsyncSession,
    sink: PhotoSink,
    meter: Meter,
    data: ReadingData,
    *,
    by: Reader,
) -> ReadResult:
    """`meter` kilitli gelmeli."""
    if meter.status == MeterStatus.REPLACED.value:
        raise conflict("meter_replaced", "Bu sayaç değişti; yeni sayacı okuyun.")
    if not valid_period(data.period):
        raise FormFieldError({"period": "Dönem seçin."})
    period = str(data.period)
    existing = await _current(session, meter.id, period)
    if existing is not None and existing.status not in (
        ReadingStatus.ANOMALY.value,
        ReadingStatus.UNREADABLE.value,
    ):
        raise conflict("already_read", "Bu dönem okunmuş.")
    previous = await _previous(session, meter.id, period)
    config = await settings(session)
    reason = clean(data.unreadable_reason, 120)
    if reason:
        if existing is not None:
            existing.status = ReadingStatus.REREAD.value
            await session.flush()
        reading = MeterReading(
            id=uuid.uuid7(),
            meter_id=meter.id,
            period=period,
            value=None,
            prev_value=previous.value if previous else None,
            status=ReadingStatus.UNREADABLE.value,
            reason=reason,
            by_name=by.name,
            by_user_id=by.user_id,
        )
        session.add(reading)
        await session.flush()
        return ReadResult(reading)

    errors: dict[str, str] = {}
    value = _index(data.value, "value", errors)
    if config.meter_photo_required and not data.photo:
        errors["photo"] = "Sayaç fotoğrafı zorunlu (site ayarı)."
    replacement = data.replacement
    old_final = new_initial = Decimal(0)
    new_serial = ""
    if replacement is not None:
        old_final = _index(replacement.old_final, "replacement.old_final", errors)
        new_initial = _index(replacement.new_initial, "replacement.new_initial", errors)
        new_serial = clean(replacement.new_serial, 40)
        if not new_serial:
            errors["replacement.new_serial"] = "Yeni sayacın seri numarasını yazın."
        elif await session.scalar(
            select(Meter.id).where(
                Meter.serial == new_serial, Meter.status != MeterStatus.REPLACED.value
            )
        ):
            errors["replacement.new_serial"] = "Bu seri no kayıtlı."
    if errors:
        raise FormFieldError(errors)
    if (
        replacement is None
        and previous is not None
        and previous.value is not None
        and value < previous.value
    ):
        raise OpsError(
            422,
            "index_decreased",
            f"Endeks önceki okumadan ({format_quantity_tr(previous.value)}) küçük olamaz; "
            "yeniden okuyun ya da sayaç değişimini işaretleyin.",
            {"value": "Önceki endeksten küçük."},
        )
    if (
        replacement is not None
        and previous is not None
        and previous.value is not None
        and old_final < previous.value
    ):
        raise FormFieldError(
            {"replacement.old_final": "Eski sayacın son endeksi önceki okumadan küçük olamaz."}
        )
    if replacement is not None and value < new_initial:
        raise FormFieldError({"value": "Yeni sayacın endeksi ilk endeksinden küçük olamaz."})

    photo_id = await save_photo(session, sink, data.photo, by=by.name, field_name="photo")
    if existing is not None:
        existing.status = ReadingStatus.REREAD.value
        await session.flush()
    own_value = old_final if replacement is not None else value
    used = (
        consumption(own_value, previous.value, meter.multiplier)
        if previous is not None and previous.value is not None
        else None
    )
    new_meter: Meter | None = None
    if replacement is not None:
        new_meter = Meter(
            id=uuid.uuid7(),
            kind=meter.kind,
            serial=new_serial,
            unit_id=meter.unit_id,
            area=meter.area,
            unit_label=meter.unit_label,
            multiplier=meter.multiplier,
            installed_on=None,
            status=MeterStatus.ACTIVE.value,
        )
        meter.status = MeterStatus.REPLACED.value
        await session.flush()
        session.add(new_meter)
        await session.flush()
        meter.replaced_by_id = new_meter.id
        added = consumption(value, new_initial, meter.multiplier)
        used = added if used is None else used + added
    last_used = await _previous_consumption(session, meter.id, period)
    percent = deviation_percent(used, last_used) if used is not None else None
    anomaly = is_anomaly(percent, config.meter_anomaly_percent)
    reading = MeterReading(
        id=uuid.uuid7(),
        meter_id=meter.id,
        period=period,
        value=own_value,
        prev_value=previous.value if previous else None,
        consumption=used,
        status=(ReadingStatus.ANOMALY if anomaly else ReadingStatus.OK).value,
        reason="Sayaç değişti" if replacement is not None else None,
        anomaly_percent=percent,
        photo_file_id=photo_id,
        by_name=by.name,
        by_user_id=by.user_id,
    )
    session.add(reading)
    if new_meter is not None:
        session.add(
            MeterReading(
                id=uuid.uuid7(),
                meter_id=new_meter.id,
                period=period,
                value=value,
                prev_value=new_initial,
                consumption=None,
                status=ReadingStatus.OK.value,
                reason=f"İlk endeks {format_quantity_tr(new_initial)} (sayaç değişimi)",
                by_name=by.name,
                by_user_id=by.user_id,
            )
        )
    await session.flush()
    return ReadResult(reading, new_meter)


async def _previous_consumption(
    session: AsyncSession, meter_id: uuid.UUID, period: str
) -> Decimal | None:
    found: Decimal | None = await session.scalar(
        select(MeterReading.consumption)
        .where(
            MeterReading.meter_id == meter_id,
            MeterReading.period < period,
            MeterReading.status.in_((ReadingStatus.OK.value, ReadingStatus.APPROVED.value)),
            MeterReading.consumption.is_not(None),
        )
        .order_by(MeterReading.period.desc())
        .limit(1)
    )
    return found


async def get_reading(session: AsyncSession, reading_id: uuid.UUID) -> MeterReading | None:
    found: MeterReading | None = await session.scalar(
        select(MeterReading).where(MeterReading.id == reading_id).with_for_update()
    )
    return found


async def decide(session: AsyncSession, reading: MeterReading, *, approve: bool, by: str) -> None:
    if reading.status != ReadingStatus.ANOMALY.value:
        raise conflict("invalid_status", "Yalnız anormal okuma onaylanır ya da yeniden okunur.")
    reading.status = (ReadingStatus.APPROVED if approve else ReadingStatus.REREAD).value
    reading.decided_by_name = by
    reading.reason = f"Onaylayan: {by}" if approve else "Yeniden okuma istendi"
    await session.flush()


@dataclass(frozen=True, slots=True)
class PeriodRow:
    meter: Meter
    reading: MeterReading | None
    last: MeterReading | None


async def period_rows(session: AsyncSession, period: str) -> list[PeriodRow]:
    meters = list(await session.scalars(meters_query()))
    if not meters:
        return []
    ids = [m.id for m in meters]
    current = {
        r.meter_id: r
        for r in await session.scalars(
            select(MeterReading).where(
                MeterReading.meter_id.in_(ids),
                MeterReading.period == period,
                MeterReading.status != ReadingStatus.REREAD.value,
            )
        )
    }
    last: dict[uuid.UUID, MeterReading] = {}
    for r in await session.scalars(
        select(MeterReading)
        .where(
            MeterReading.meter_id.in_(ids),
            MeterReading.period < period,
            MeterReading.status.in_(VALID_STATUSES),
        )
        .ext(distinct_on(MeterReading.meter_id))
        .order_by(MeterReading.meter_id, MeterReading.period.desc(), MeterReading.created_at.desc())
    ):
        last[r.meter_id] = r
    return [PeriodRow(m, current.get(m.id), last.get(m.id)) for m in meters]


def missing_count(rows: list[PeriodRow]) -> int:
    """Okunmamış ya da okunamamış aktif sayaçlar (arızalı dahil — bilgi için)."""
    return sum(
        1 for r in rows if r.reading is None or r.reading.status == ReadingStatus.UNREADABLE.value
    )


async def readings_for_units(
    session: AsyncSession, unit_ids: list[uuid.UUID]
) -> list[tuple[Meter, list[MeterReading]]]:
    if not unit_ids:
        return []
    meters = list(
        await session.scalars(
            select(Meter).where(Meter.unit_id.in_(unit_ids)).order_by(Meter.serial, Meter.id)
        )
    )
    by_meter: dict[uuid.UUID, list[MeterReading]] = {m.id: [] for m in meters}
    if meters:
        for r in await session.scalars(
            select(MeterReading)
            .where(
                MeterReading.meter_id.in_(list(by_meter)),
                MeterReading.status != ReadingStatus.REREAD.value,
            )
            .order_by(MeterReading.period.desc())
            .limit(24 * len(meters))
        ):
            by_meter[r.meter_id].append(r)
    return [(m, by_meter[m.id]) for m in meters]


async def set_settings(
    session: AsyncSession, *, anomaly_percent: int | None, photo_required: bool | None
) -> None:
    config = await settings(session)
    if anomaly_percent is not None:
        if not 1 <= anomaly_percent <= 1000:
            raise FormFieldError({"meter_anomaly_percent": "Eşik %1–%1000 arasında olmalı."})
        config.meter_anomaly_percent = anomaly_percent
    if photo_required is not None:
        config.meter_photo_required = photo_required
    await session.flush()
