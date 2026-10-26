"""Plaka sorgu, giriş-çıkış kaydı ve otopark ihlali (servis isteği 43; GUV-04, OTP-02).

Kaynaklar: kayıtlı araçlar (servis isteği 22 — **P02 araç tablosu gelince bağlanacak**; o zamana
kadar "kayıtlı" sonucu dönmez), geçerli ön davet plakaları, bugün kullanılmış tek kullanımlık davet
plakaları ve bugün içeride olan ziyaretçilerin plakaları. Görevli telefon görmez; yalnız bölüm
ve ad.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.gatehouse import (
    PLATE_FORMAT_MESSAGE,
    PLATE_MIN_LENGTH,
    GateDirection,
    InviteStatus,
    PlateResult,
    ViolationNotice,
    check_violation,
    display_plate,
    normalize_plate,
)
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.domain.operations import OperationRuleError
from site_yonetim.domain.security import VisitorStatus
from site_yonetim.models import GateLog, ParkingViolation, Site, Visitor, VisitorInvite
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.gatehouse import manager_user_ids, unit_user_ids
from site_yonetim.services.security import unit_names
from site_yonetim.services.visits import window


@dataclass(frozen=True, slots=True)
class PlateInfo:
    plate: str
    plate_display: str
    result: PlateResult
    unit_id: uuid.UUID | None = None
    unit_name: str | None = None
    owner_name: str | None = None
    valid_until: date | None = None
    detail: str | None = None


def _clean(raw: str | None) -> str:
    plate = normalize_plate(raw)
    if len(plate) < PLATE_MIN_LENGTH:
        raise FormFieldError({"plate": PLATE_FORMAT_MESSAGE})
    return plate


async def registered_vehicle(session: AsyncSession, plate: str, today: date) -> PlateInfo | None:
    """Kayıtlı araç (YAP-05, servis isteği 22). Araç tablosu P02 paketiyle gelir; bağlanınca
    bölüm ve sahip adı buradan döner (misafir aracı `valid_until`'a kadar)."""
    del session, plate, today
    return None


async def lookup(session: AsyncSession, raw: str | None, now: datetime) -> PlateInfo:
    plate = _clean(raw)
    display = display_plate(plate)
    today = now.astimezone(BUSINESS_TZ).date()
    vehicle = await registered_vehicle(session, plate, today)
    if vehicle is not None:
        return vehicle
    day_start = datetime.combine(today, time.min, BUSINESS_TZ)
    invites = list(
        await session.scalars(
            select(VisitorInvite)
            .where(
                VisitorInvite.plate == plate,
                VisitorInvite.status != InviteStatus.CANCELLED.value,
                VisitorInvite.date_from <= today,
                VisitorInvite.date_to >= today,
            )
            .order_by(VisitorInvite.created_at.desc())
            .limit(20)
        )
    )
    for invite in invites:
        valid = invite.status == InviteStatus.ACTIVE.value and window(invite).valid_on(today)
        used_today = invite.used_at is not None and invite.used_at >= day_start
        if valid or used_today:
            unit = (await unit_names(session, {invite.unit_id})).get(invite.unit_id, "—")
            until = invite.date_to.strftime("%d.%m.%Y")
            return PlateInfo(
                plate,
                display,
                PlateResult.GUEST,
                invite.unit_id,
                unit,
                None,
                invite.date_to,
                f"{unit} misafiri ({invite.visitor_name}) · {until} tarihine kadar",
            )
    visitor = await session.scalar(
        select(Visitor)
        .where(
            Visitor.plate_number == plate,
            Visitor.visit_date == today,
            Visitor.status == VisitorStatus.ENTERED.value,
        )
        .order_by(Visitor.entered_at.desc())
        .limit(1)
    )
    if visitor is not None:
        unit = (await unit_names(session, {visitor.unit_id})).get(visitor.unit_id, "—")
        return PlateInfo(
            plate,
            display,
            PlateResult.GUEST,
            visitor.unit_id,
            unit,
            None,
            today,
            f"{unit} ziyaretçisi ({visitor.full_name}) · bugün içeride",
        )
    return PlateInfo(plate, display, PlateResult.UNKNOWN)


# --- Giriş-çıkış kaydı ---------------------------------------------------------------------


async def record_gate_log(
    session: AsyncSession,
    *,
    plate: str | None,
    direction: GateDirection,
    gate_id: uuid.UUID | None,
    now: datetime,
    actor: str,
) -> tuple[GateLog, PlateInfo]:
    info = await lookup(session, plate, now)
    log = GateLog(
        plate=info.plate,
        result=info.result.value,
        detail=info.detail,
        unit_id=info.unit_id,
        gate_id=gate_id,
        direction=direction.value,
        recorded_by=actor,
        at=now,
        log_date=now.astimezone(BUSINESS_TZ).date(),
    )
    session.add(log)
    await session.flush()
    return log, info


def gate_logs_query(day: date) -> Select[GateLog]:
    return (
        select(GateLog)
        .where(GateLog.log_date == day)
        .order_by(GateLog.at.desc(), GateLog.id.desc())
    )


# --- Otopark ihlali -------------------------------------------------------------------------


async def record_violation(
    session: AsyncSession,
    site: Site,
    *,
    plate: str | None,
    location: str | None,
    note: str | None,
    now: datetime,
    actor: str,
) -> ParkingViolation:
    clean, where, text = check_violation(plate, location, note)
    info = await lookup(session, clean, now)
    if info.result is not PlateResult.UNKNOWN and not text:
        raise OperationRuleError(
            "plate_authorized",
            f"{info.plate_display} yetkili ({info.detail}). Yine de ihlal kaydedilecekse "
            "nedenini yazın (ör. yanlış yere park).",
            conflict=True,
        )
    notice = ViolationNotice.RESIDENT if info.unit_id else ViolationNotice.MANAGEMENT
    violation = ParkingViolation(
        plate=clean,
        location=where,
        note=text,
        result=info.result.value,
        detail=info.detail,
        unit_id=info.unit_id,
        notified=notice.value,
        recorded_by=actor,
        occurred_at=now,
    )
    session.add(violation)
    await session.flush()
    today = now.astimezone(BUSINESS_TZ).date()
    users = (
        await unit_user_ids(session, {info.unit_id}, today)
        if info.unit_id
        else await manager_user_ids(session, site)
    )
    await notify_svc.notify(
        session,
        users,
        notify_svc.Message(
            category=NotificationCategory.SECURITY,
            kind="parking.violation",
            title=f"Otopark ihlali: {info.plate_display}",
            body=f"{where}{f' — {text}' if text else ''}",
            link_entity="parking_violations",
            link_id=violation.id,
        ),
        dedupe_key=f"parking-violation:{violation.id}",
    )
    return violation


def violations_query() -> Select[ParkingViolation]:
    return select(ParkingViolation).order_by(
        ParkingViolation.occurred_at.desc(), ParkingViolation.id.desc()
    )
