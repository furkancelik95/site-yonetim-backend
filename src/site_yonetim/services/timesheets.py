"""Puantaj, giriş-çıkış ve aktarım — servis istekleri 38–39 (Yazılım Dokümanı IK-04, IK-05).
Açık site kapsamında; transaction'ı çağıran yönetir.

- Puantaj **canlı** hesaplanır: vardiya planı + onaylı izin + İK düzeltmesi + (doğrulama açıksa)
  giriş-çıkış kayıtları. Belgedeki "gece 01:00 puantaj üretimi" işinin yerine geçer: bir önceki
  günün puantajı her an hazırdır; gün **kesinleşince** satırlar `timesheet_lines`'a donar.
- Düzeltme gerekçeli ve denetimli; kesin gün değişmez (409). Bugün/gelecek kesinleşmez (409).
- Giriş-çıkış (IK-04, K29): yöntem yok / QR (kulübedeki sabit kod, yönetici yeniler) / konum
  (tesis noktasına uzaklık ≤ yarıçap). Yalnız okutma anı; konum **saklanmaz**, uzaklık tutulur.
  Doğrulanamayan kayıt reddedilmez, istisna olarak işaretlenir. Çevrimdışı kayıt cihaz saatiyle
  gelir; sunucu saatiyle fark yanıtta görünür.
- Aktarım (Mod P: dosya): ayın tüm günleri kesin, sicil no tam, ay bitmiş olmalı; ay kilitlenir.
  Resmî tatil takvimi (S-03) gelene kadar tatil çalışması 0. Mod K (Kivi) A-01 sonrası.
"""

import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.staffing import (
    REASON_MAX,
    REASON_MIN,
    AttendanceKind,
    AttendanceMethod,
    LeaveType,
    TimesheetStatus,
    check_fix_minutes,
    dates,
    detect,
    distance_m,
    hours,
    leave_on,
    month_bounds,
    night_minutes,
    planned_minutes,
    worked_minutes,
)
from site_yonetim.models import (
    AttendanceEvent,
    AttendanceSettings,
    ShiftAssignment,
    ShiftTemplate,
    StaffMember,
    TimesheetDay,
    TimesheetExport,
    TimesheetFix,
    TimesheetLine,
)
from site_yonetim.models.audit import audit_reason
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.shifts import (
    approved_leaves,
    leave_days,
    template_label,
    template_span,
)

QR_CODE_BYTES = 6
CLOCK_SKEW_MINUTES = 5
MATCH_WINDOW_MINUTES = 12 * 60
DEFAULT_RADIUS_M = 200
DEFAULT_TOLERANCE = 10  # belge IK-04 S1 örneği


class TimesheetRuleError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# --- Ayarlar --------------------------------------------------------------------------------


async def settings(session: AsyncSession) -> AttendanceSettings | None:
    found: AttendanceSettings | None = await session.scalar(select(AttendanceSettings))
    return found


def new_code() -> str:
    return secrets.token_hex(QR_CODE_BYTES).upper()


@dataclass(frozen=True, slots=True)
class SettingsData:
    method: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    radius_m: int | None
    late_tolerance_minutes: int | None
    early_tolerance_minutes: int | None


async def save_settings(
    session: AsyncSession, data: SettingsData, *, now: datetime
) -> AttendanceSettings:
    errors: dict[str, str] = {}
    try:
        method = AttendanceMethod(data.method or "")
    except ValueError:
        errors["method"] = "Doğrulama yöntemini seçin."
        method = AttendanceMethod.NONE
    if data.latitude is not None and not -90 <= data.latitude <= 90:
        errors["latitude"] = "Enlem −90 ile 90 arasında olmalı."
    if data.longitude is not None and not -180 <= data.longitude <= 180:
        errors["longitude"] = "Boylam −180 ile 180 arasında olmalı."
    if method is AttendanceMethod.LOCATION and (data.latitude is None or data.longitude is None):
        errors["latitude"] = "Konum doğrulaması için tesisin konumunu girin."
    radius = data.radius_m if data.radius_m is not None else DEFAULT_RADIUS_M
    if not 20 <= radius <= 5000:
        errors["radius_m"] = "Yarıçap 20–5000 metre."
    late = data.late_tolerance_minutes
    early = data.early_tolerance_minutes
    late = DEFAULT_TOLERANCE if late is None else late
    early = DEFAULT_TOLERANCE if early is None else early
    if not 0 <= late <= 120:
        errors["late_tolerance_minutes"] = "Tolerans 0–120 dakika."
    if not 0 <= early <= 120:
        errors["early_tolerance_minutes"] = "Tolerans 0–120 dakika."
    if errors:
        raise FormFieldError(errors)
    item = await settings(session)
    if item is None:
        item = AttendanceSettings(qr_code=new_code(), qr_rotated_at=now)
        session.add(item)
    item.method = method.value
    item.latitude = (
        data.latitude.quantize(Decimal("0.000001")) if data.latitude is not None else None
    )
    item.longitude = (
        data.longitude.quantize(Decimal("0.000001")) if data.longitude is not None else None
    )
    item.radius_m = radius
    item.late_tolerance_minutes = late
    item.early_tolerance_minutes = early
    await session.flush()
    return item


async def rotate_code(session: AsyncSession, *, now: datetime) -> AttendanceSettings:
    item = await settings(session)
    if item is None:
        item = AttendanceSettings(
            method=AttendanceMethod.NONE.value,
            qr_code=new_code(),
            qr_rotated_at=now,
            radius_m=DEFAULT_RADIUS_M,
            late_tolerance_minutes=DEFAULT_TOLERANCE,
            early_tolerance_minutes=DEFAULT_TOLERANCE,
        )
        session.add(item)
    else:
        item.qr_code = new_code()
        item.qr_rotated_at = now
    await session.flush()
    return item


# --- Puantaj satırı -------------------------------------------------------------------------


@dataclass(slots=True)
class Row:
    id: uuid.UUID | None  # vardiya ataması; izinli ve vardiyasız satırda boş
    work_date: date
    staff_id: uuid.UUID
    staff_name: str
    position: str | None
    payroll_no: str | None
    template: str
    planned_minutes: int
    night_minutes: int
    status: TimesheetStatus
    leave_type: str | None
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    worked_minutes: int
    reason: str | None
    fixed_by: str | None
    is_exception: bool
    flags: list[str] = field(default_factory=list)


def _local_minutes(at: datetime, day: date) -> int:
    local = at.astimezone(BUSINESS_TZ)
    return (local.date() - day).days * 1440 + local.hour * 60 + local.minute


async def live_rows(
    session: AsyncSession, first: date, last: date, *, today: date
) -> dict[date, list[Row]]:
    """[first, last] günlerinin canlı puantajı (kesinleşmemiş günler için)."""
    rows_db = (
        await session.execute(
            select(ShiftAssignment, ShiftTemplate)
            .join(ShiftTemplate, ShiftTemplate.id == ShiftAssignment.template_id)
            .where(ShiftAssignment.work_date.between(first, last))
            .order_by(ShiftAssignment.work_date, ShiftTemplate.start_time, ShiftAssignment.id)
        )
    ).all()
    leaves = await approved_leaves(session, first, last)
    days_off = leave_days(leaves)
    staff_ids = {a.staff_id for a, _ in rows_db} | {x.staff_id for x in leaves}
    staff = {
        s.id: s
        for s in await session.scalars(select(StaffMember).where(StaffMember.id.in_(staff_ids)))
    }
    ids = [a.id for a, _ in rows_db]
    fixes = {
        f.assignment_id: f
        for f in await session.scalars(
            select(TimesheetFix).where(TimesheetFix.assignment_id.in_(ids))
        )
    }
    conf = await settings(session)
    verify = conf is not None and conf.method != AttendanceMethod.NONE.value
    events: dict[uuid.UUID, list[AttendanceEvent]] = {}
    if verify:
        for e in await session.scalars(
            select(AttendanceEvent)
            .where(AttendanceEvent.assignment_id.in_(ids))
            .order_by(AttendanceEvent.occurred_at)
        ):
            if e.assignment_id is not None:
                events.setdefault(e.assignment_id, []).append(e)
    out: dict[date, list[Row]] = {d: [] for d in dates(first, last)}
    for a, t in rows_db:
        person = staff[a.staff_id]
        s = template_span(t)
        planned = planned_minutes(s, t.break_minutes)
        night = night_minutes(s)
        leave = leave_on(days_off, a.staff_id, a.work_date)
        fix = fixes.get(a.id)
        status, late, early, over = TimesheetStatus.WORKED, 0, 0, 0
        flags: list[str] = []
        exception = False
        if fix is not None:
            status = TimesheetStatus(fix.status)
            late, early, over = fix.late_minutes, fix.early_leave_minutes, fix.overtime_minutes
            exception = True
        elif leave is not None:
            status = TimesheetStatus.LEAVE
            exception = True
        elif verify and conf is not None and a.work_date < today:
            mine = events.get(a.id, [])
            ins = [e for e in mine if e.kind == AttendanceKind.IN.value]
            outs = [e for e in mine if e.kind == AttendanceKind.OUT.value]
            found = detect(
                s,
                check_in=_local_minutes(ins[0].occurred_at, a.work_date) if ins else None,
                check_out=_local_minutes(outs[-1].occurred_at, a.work_date) if outs else None,
                unverified=any(not e.verified for e in mine),
                late_tolerance=conf.late_tolerance_minutes,
                early_tolerance=conf.early_tolerance_minutes,
            )
            status, late, early, over = found.status, found.late, found.early, found.overtime
            flags = [f.value for f in found.flags]
            exception = bool(flags)
        worked = status is TimesheetStatus.WORKED
        out[a.work_date].append(
            Row(
                id=a.id,
                work_date=a.work_date,
                staff_id=a.staff_id,
                staff_name=person.full_name,
                position=person.position,
                payroll_no=person.payroll_no,
                template=template_label(t),
                planned_minutes=planned,
                night_minutes=night if worked else 0,
                status=status,
                leave_type=leave.leave_type.value if leave is not None else None,
                late_minutes=late if worked else 0,
                early_leave_minutes=early if worked else 0,
                overtime_minutes=over if worked else 0,
                worked_minutes=worked_minutes(planned, late, early, over) if worked else 0,
                reason=fix.reason if fix is not None else None,
                fixed_by=fix.fixed_by_name if fix is not None else None,
                is_exception=exception,
                flags=flags,
            )
        )
    # Vardiyası olmayan ama izinli personel de satır olarak görünür (IK-04 adım 2).
    for leave_row in leaves:
        on_leave = staff.get(leave_row.staff_id)
        if on_leave is None:  # pragma: no cover
            continue
        for day in dates(max(first, leave_row.date_from), min(last, leave_row.date_to)):
            if any(r.staff_id == on_leave.id for r in out[day]):
                continue
            out[day].append(
                Row(
                    id=None,
                    work_date=day,
                    staff_id=on_leave.id,
                    staff_name=on_leave.full_name,
                    position=on_leave.position,
                    payroll_no=on_leave.payroll_no,
                    template="—",
                    planned_minutes=0,
                    night_minutes=0,
                    status=TimesheetStatus.LEAVE,
                    leave_type=leave_row.leave_type,
                    late_minutes=0,
                    early_leave_minutes=0,
                    overtime_minutes=0,
                    worked_minutes=0,
                    reason=None,
                    fixed_by=None,
                    is_exception=True,
                )
            )
    for day_rows in out.values():
        day_rows.sort(key=lambda r: (r.staff_name.casefold(), r.template, str(r.id)))
    return out


def _line_row(line: TimesheetLine, person: StaffMember | None) -> Row:
    return Row(
        id=line.assignment_id,
        work_date=line.work_date,
        staff_id=line.staff_id,
        staff_name=person.full_name if person else "—",
        position=person.position if person else None,
        payroll_no=person.payroll_no if person else None,
        template=line.template_label or "—",
        planned_minutes=line.planned_minutes,
        night_minutes=line.night_minutes,
        status=TimesheetStatus(line.status),
        leave_type=line.leave_type,
        late_minutes=line.late_minutes,
        early_leave_minutes=line.early_leave_minutes,
        overtime_minutes=line.overtime_minutes,
        worked_minutes=line.worked_minutes,
        reason=line.reason,
        fixed_by=line.fixed_by_name,
        is_exception=line.is_exception,
    )


async def final_rows(session: AsyncSession, first: date, last: date) -> dict[date, list[Row]]:
    lines = list(
        await session.scalars(
            select(TimesheetLine)
            .where(TimesheetLine.work_date.between(first, last))
            .order_by(TimesheetLine.work_date, TimesheetLine.id)
        )
    )
    ids = {x.staff_id for x in lines}
    staff = {
        s.id: s for s in await session.scalars(select(StaffMember).where(StaffMember.id.in_(ids)))
    }
    out: dict[date, list[Row]] = {}
    for line in lines:
        out.setdefault(line.work_date, []).append(_line_row(line, staff.get(line.staff_id)))
    for day_rows in out.values():
        day_rows.sort(key=lambda r: (r.staff_name.casefold(), r.template, str(r.id)))
    return out


@dataclass(frozen=True, slots=True)
class Day:
    work_date: date
    record: TimesheetDay | None
    rows: list[Row]


async def day(session: AsyncSession, work_date: date, *, today: date) -> Day:
    record = await session.scalar(select(TimesheetDay).where(TimesheetDay.work_date == work_date))
    if record is not None:
        rows = (await final_rows(session, work_date, work_date)).get(work_date, [])
    else:
        rows = (await live_rows(session, work_date, work_date, today=today))[work_date]
    return Day(work_date, record, rows)


# --- Düzeltme ve kesinleştirme --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FixData:
    status: str | None
    late_minutes: int | None
    early_leave_minutes: int | None
    overtime_minutes: int | None
    reason: str | None


async def fix(
    session: AsyncSession,
    assignment: ShiftAssignment,
    data: FixData,
    *,
    actor_name: str,
    today: date,
) -> TimesheetFix:
    if await session.scalar(
        select(TimesheetDay.id).where(TimesheetDay.work_date == assignment.work_date)
    ):
        raise TimesheetRuleError(
            409, "timesheet_final", "Bu günün puantajı kesinleşti; düzeltme yapılamaz."
        )
    if assignment.work_date > today:
        raise TimesheetRuleError(409, "day_not_started", "Gelecek günün puantajı düzeltilemez.")
    errors: dict[str, str] = {}
    if data.status not in {TimesheetStatus.WORKED.value, TimesheetStatus.ABSENT.value}:
        errors["status"] = "Durum seçin."
    minutes: dict[str, int] = {}
    for key in ("late_minutes", "early_leave_minutes", "overtime_minutes"):
        try:
            minutes[key] = check_fix_minutes(getattr(data, key))
        except ValueError as exc:
            errors[key] = str(exc)
    reason = " ".join((data.reason or "").split())[:REASON_MAX]
    if len(reason) < REASON_MIN:
        errors["reason"] = "Düzeltme gerekçesi zorunlu."
    if errors:
        raise FormFieldError(errors)
    worked = data.status == TimesheetStatus.WORKED.value
    item = await session.scalar(
        select(TimesheetFix).where(TimesheetFix.assignment_id == assignment.id).with_for_update()
    )
    if item is None:
        item = TimesheetFix(assignment_id=assignment.id)
        session.add(item)
    item.status = data.status or ""
    item.late_minutes = minutes["late_minutes"] if worked else 0
    item.early_leave_minutes = minutes["early_leave_minutes"] if worked else 0
    item.overtime_minutes = minutes["overtime_minutes"] if worked else 0
    item.reason = reason
    item.fixed_by_name = actor_name
    with audit_reason(reason):
        await session.flush()
    return item


async def finalize(session: AsyncSession, work_date: date, *, today: date, actor_name: str) -> int:
    if work_date >= today:
        raise TimesheetRuleError(409, "day_not_ended", "Gün bitmeden puantaj kesinleşmez.")
    try:
        async with session.begin_nested():
            session.add(TimesheetDay(work_date=work_date, finalized_by_name=actor_name))
            await session.flush()
    except IntegrityError:
        raise TimesheetRuleError(409, "already_final", "Bu gün zaten kesinleşti.") from None
    rows = (await live_rows(session, work_date, work_date, today=today))[work_date]
    session.add_all(
        TimesheetLine(
            work_date=work_date,
            staff_id=r.staff_id,
            assignment_id=r.id,
            template_label=None if r.id is None else r.template,
            planned_minutes=r.planned_minutes,
            night_minutes=r.night_minutes,
            status=r.status.value,
            leave_type=r.leave_type,
            late_minutes=r.late_minutes,
            early_leave_minutes=r.early_leave_minutes,
            overtime_minutes=r.overtime_minutes,
            worked_minutes=r.worked_minutes,
            reason=r.reason,
            fixed_by_name=r.fixed_by,
            is_exception=r.is_exception,
        )
        for r in rows
    )
    await session.flush()
    return len(rows)


# --- Aylık aktarım (IK-05) ------------------------------------------------------------------


def _blank(row: Row) -> dict[str, Any]:
    return {
        "staff_id": str(row.staff_id),
        "payroll_no": row.payroll_no,
        "name": row.staff_name,
        "worked": set(),
        "worked_minutes": 0,
        "overtime_minutes": 0,
        "night_minutes": 0,
        "absent": set(),
        **{f"leave_{k.value}": set() for k in LeaveType},
    }


def summarize(rows: list[Row]) -> list[dict[str, Any]]:
    """Personel bazında ay özeti; gün sayıları tekil günlerdir, saatler 2 ondalık (Decimal)."""
    acc: dict[uuid.UUID, dict[str, Any]] = {}
    for r in rows:
        o = acc.setdefault(r.staff_id, _blank(r))
        if r.status is TimesheetStatus.WORKED:
            o["worked"].add(r.work_date)
            o["worked_minutes"] += r.worked_minutes
            o["overtime_minutes"] += r.overtime_minutes
            o["night_minutes"] += r.night_minutes
        elif r.status is TimesheetStatus.ABSENT:
            o["absent"].add(r.work_date)
        elif r.leave_type is not None:
            o[f"leave_{r.leave_type}"].add(r.work_date)
    out = [
        {
            "staff_id": o["staff_id"],
            "payroll_no": o["payroll_no"],
            "name": o["name"],
            "worked_days": len(o["worked"]),
            "worked_hours": hours(o["worked_minutes"]),
            "overtime_hours": hours(o["overtime_minutes"]),
            "night_hours": hours(o["night_minutes"]),
            "absent_days": len(o["absent"]),
            **{f"leave_{k.value}": len(o[f"leave_{k.value}"]) for k in LeaveType},
            "holiday_hours": Decimal("0.00"),  # resmî tatil takvimi (S-03) gelince
        }
        for o in acc.values()
    ]
    return sorted(out, key=lambda x: (str(x["name"]).casefold(), x["staff_id"]))


@dataclass(frozen=True, slots=True)
class Preview:
    month: str
    rows: list[dict[str, Any]]
    missing_days: list[date]
    missing_payroll: list[str]
    exported: TimesheetExport | None


def check_month(month: str) -> tuple[date, date]:
    bounds = month_bounds(month)
    if bounds is None:
        raise FormFieldError({"month": "Ay seçin (YYYY-AA)."})
    return bounds


async def preview(session: AsyncSession, month: str, *, today: date) -> Preview:
    first, last = check_month(month)
    final = await final_rows(session, first, last)
    live = await live_rows(session, first, last, today=today)
    rows: list[Row] = []
    missing: list[date] = []
    for d in dates(first, last):
        if d in final:
            rows.extend(final[d])
        elif live[d]:
            rows.extend(live[d])
            missing.append(d)
    finalized = set(
        await session.scalars(
            select(TimesheetDay.work_date).where(TimesheetDay.work_date.between(first, last))
        )
    )
    missing = [d for d in missing if d not in finalized]
    summary = summarize(rows)
    exported = await session.scalar(select(TimesheetExport).where(TimesheetExport.month == month))
    return Preview(
        month,
        summary,
        missing,
        [str(r["name"]) for r in summary if not r["payroll_no"]],
        exported,
    )


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in row.items()}


async def export(
    session: AsyncSession, month: str, *, today: date, actor_name: str
) -> TimesheetExport:
    first, _ = check_month(month)
    if first > today.replace(day=1) or first == today.replace(day=1):
        raise TimesheetRuleError(409, "month_not_ended", "Ay bitmeden puantaj aktarılmaz.")
    pv = await preview(session, month, today=today)
    if pv.exported is not None:
        raise TimesheetRuleError(
            409,
            "already_exported",
            "Bu ayın puantajı aktarıldı ve kilitli; düzeltme sonraki dönemde ayrı kayıtla yapılır.",
        )
    if pv.missing_days:
        shown = ", ".join(d.strftime("%d.%m.%Y") for d in pv.missing_days[:10])
        more = "…" if len(pv.missing_days) > 10 else ""
        raise TimesheetRuleError(
            409, "days_not_final", f"Kesinleşmemiş günler var: {shown}{more}. Aktarım başlatılamaz."
        )
    if pv.missing_payroll:
        raise TimesheetRuleError(
            409,
            "payroll_missing",
            f"Bordro sicil no eksik: {', '.join(pv.missing_payroll)}. Personel kartından girin.",
        )
    if not pv.rows:
        raise TimesheetRuleError(409, "empty_month", "Bu ay puantaj yok.")
    item = TimesheetExport(
        month=month, rows=[_json_row(r) for r in pv.rows], created_by_name=actor_name
    )
    try:
        async with session.begin_nested():
            session.add(item)
            await session.flush()
    except IntegrityError:
        raise TimesheetRuleError(
            409, "already_exported", "Bu ayın puantajı aktarıldı ve kilitli."
        ) from None
    return item


# --- Giriş-çıkış (personel, K29) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventData:
    kind: str | None
    assignment_id: uuid.UUID | None
    occurred_at: datetime | None
    code: str | None
    latitude: Decimal | None
    longitude: Decimal | None


@dataclass(frozen=True, slots=True)
class Recorded:
    event: AttendanceEvent
    skew_minutes: int
    message: str


async def candidate_shifts(
    session: AsyncSession, staff_id: uuid.UUID, today: date
) -> list[tuple[ShiftAssignment, ShiftTemplate]]:
    """Dün, bugün ve yarının vardiyaları (gece vardiyası ertesi güne taşar)."""
    rows = await session.execute(
        select(ShiftAssignment, ShiftTemplate)
        .join(ShiftTemplate, ShiftTemplate.id == ShiftAssignment.template_id)
        .where(
            ShiftAssignment.staff_id == staff_id,
            ShiftAssignment.work_date.between(today - timedelta(days=1), today + timedelta(days=1)),
        )
        .order_by(ShiftAssignment.work_date, ShiftTemplate.start_time)
    )
    return [(a, t) for a, t in rows.all()]


def _nearest(
    shifts: list[tuple[ShiftAssignment, ShiftTemplate]], kind: AttendanceKind, at: datetime
) -> ShiftAssignment | None:
    best: tuple[int, ShiftAssignment] | None = None
    for a, t in shifts:
        s = template_span(t)
        point = s.start if kind is AttendanceKind.IN else s.end
        gap = abs(_local_minutes(at, a.work_date) - point)
        if gap <= MATCH_WINDOW_MINUTES and (best is None or gap < best[0]):
            best = (gap, a)
    return best[1] if best else None


async def record_event(
    session: AsyncSession,
    staff: StaffMember,
    data: EventData,
    *,
    now: datetime,
    today: date,
) -> Recorded:
    try:
        kind = AttendanceKind(data.kind or "")
    except ValueError:
        raise FormFieldError({"kind": "Giriş mi çıkış mı seçin."}) from None
    occurred = data.occurred_at or now
    if occurred.tzinfo is None:
        raise FormFieldError({"occurred_at": "Saat dilimiyle gönderin (ISO 8601)."})
    if occurred > now + timedelta(minutes=CLOCK_SKEW_MINUTES):
        raise FormFieldError({"occurred_at": "Cihaz saati ileride; saatinizi kontrol edin."})
    if occurred < now - timedelta(days=2):
        raise FormFieldError({"occurred_at": "Çevrimdışı kayıt en çok 2 gün geriden gönderilir."})
    shifts = await candidate_shifts(session, staff.id, today)
    assignment: ShiftAssignment | None
    if data.assignment_id is not None:
        assignment = next((a for a, _ in shifts if a.id == data.assignment_id), None)
        if assignment is None:
            raise TimesheetRuleError(
                404, "not_found", "Bu vardiya size ait değil ya da bugün değil."
            )
    else:
        assignment = _nearest(shifts, kind, occurred)
    if assignment is not None:
        if await session.scalar(
            select(TimesheetDay.id).where(TimesheetDay.work_date == assignment.work_date)
        ):
            raise TimesheetRuleError(
                409, "timesheet_final", "Bu günün puantajı kesinleşti; kayıt eklenemez."
            )
        previous = set(
            await session.scalars(
                select(AttendanceEvent.kind).where(AttendanceEvent.assignment_id == assignment.id)
            )
        )
        if kind is AttendanceKind.IN and AttendanceKind.IN.value in previous:
            raise TimesheetRuleError(409, "already_checked_in", "Bu vardiyaya zaten başladınız.")
        if kind is AttendanceKind.OUT and AttendanceKind.IN.value not in previous:
            raise TimesheetRuleError(409, "not_checked_in", "Önce vardiyaya başlayın.")
        if kind is AttendanceKind.OUT and AttendanceKind.OUT.value in previous:
            raise TimesheetRuleError(409, "already_checked_out", "Bu vardiyayı zaten bitirdiniz.")
    conf = await settings(session)
    method = AttendanceMethod(conf.method) if conf is not None else AttendanceMethod.NONE
    verified = True
    distance: int | None = None
    if method is AttendanceMethod.QR and conf is not None:
        code = (data.code or "").strip().upper()
        if not code:
            raise FormFieldError({"code": "Kulübedeki kodu okutun ya da yazın."})
        verified = hmac.compare_digest(code.encode(), conf.qr_code.encode())
    elif method is AttendanceMethod.LOCATION and conf is not None:
        if data.latitude is None or data.longitude is None:
            raise FormFieldError({"latitude": "Konum izni verin; yalnız bu anki konum alınır."})
        if conf.latitude is None or conf.longitude is None:  # pragma: no cover — kısıt
            verified = False
        else:
            distance = distance_m(data.latitude, data.longitude, conf.latitude, conf.longitude)
            verified = distance <= conf.radius_m
    event = AttendanceEvent(
        staff_id=staff.id,
        assignment_id=assignment.id if assignment else None,
        kind=kind.value,
        method=method.value,
        occurred_at=occurred,
        received_at=now,
        verified=verified,
        distance_m=distance,
    )
    session.add(event)
    await session.flush()
    skew = round(abs((now - occurred).total_seconds()) / 60)
    action = "Giriş" if kind is AttendanceKind.IN else "Çıkış"
    if not verified:
        message = f"{action} kaydedildi ama doğrulanamadı; İK incelemesine düşer."
    elif assignment is None:
        message = f"{action} kaydedildi; bu saatte planlı vardiyanız görünmüyor."
    else:
        message = f"{action} kaydedildi."
    if skew > CLOCK_SKEW_MINUTES:
        message += f" Cihaz saati sunucudan {skew} dk farklı; işaretlendi."
    return Recorded(event, skew, message)


async def own_events(
    session: AsyncSession, staff_id: uuid.UUID, assignment_ids: list[uuid.UUID]
) -> list[AttendanceEvent]:
    return list(
        await session.scalars(
            select(AttendanceEvent)
            .where(
                AttendanceEvent.staff_id == staff_id,
                AttendanceEvent.assignment_id.in_(assignment_ids),
            )
            .order_by(AttendanceEvent.occurred_at)
        )
    )
