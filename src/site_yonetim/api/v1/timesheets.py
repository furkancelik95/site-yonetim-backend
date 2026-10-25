"""Puantaj, aktarım ve giriş-çıkış ayarları — servis istekleri 38–39 (Yazılım Dokümanı IK-04/05).

Modül `staff` (kapalıysa 404). Puantaj ve ayarlar `staff.manage`. Aktarım önizleme, geçmiş ve
dosya `staff.manage` ya da `finance.read` (muhasebe okur); aktarımı başlatmak `staff.manage`.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import CurrentUserDep, NowDep, SiteContext, TodayDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.api.v1.staff import Manager, Module
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FormFieldError
from site_yonetim.domain.staffing import (
    AttendanceFlag,
    AttendanceMethod,
    LeaveType,
    TimesheetStatus,
    export_csv,
)
from site_yonetim.models import ShiftAssignment, TimesheetExport
from site_yonetim.services import timesheets as svc

router = APIRouter(prefix="/sites/{slug}", tags=["puantaj"])
Paging = Annotated[PageParams, Depends()]


async def _export_reader(ctx: Module) -> SiteContext:
    if not (ctx.access.can(Permission.STAFF_MANAGE) or ctx.access.can(Permission.FINANCE_READ)):
        raise ForbiddenError
    return ctx


ExportReader = Annotated[SiteContext, Depends(_export_reader)]


def _rule(exc: svc.TimesheetRuleError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


# --- Günlük puantaj -------------------------------------------------------------------------


class TimesheetRowOut(BaseModel):
    id: uuid.UUID | None = Field(description="vardiya ataması; izinli ve vardiyasız satırda boş")
    staff_id: uuid.UUID
    staff_name: str
    position: str | None
    payroll_no: str | None
    template: str
    planned_minutes: int = Field(description="vardiya süresi − mola")
    night_minutes: int = Field(description="22:00–06:00 arası")
    status: TimesheetStatus
    leave_type: LeaveType | None
    late_minutes: int
    early_leave_minutes: int
    overtime_minutes: int
    worked_minutes: int = Field(description="planlanan − geç − erken + fazla mesai")
    reason: str | None
    fixed_by: str | None
    is_exception: bool
    flags: list[AttendanceFlag] = Field(description="giriş-çıkış karşılaştırmasının işaretleri")

    @classmethod
    def of(cls, r: svc.Row) -> TimesheetRowOut:
        return cls(
            id=r.id,
            staff_id=r.staff_id,
            staff_name=r.staff_name,
            position=r.position,
            payroll_no=r.payroll_no,
            template=r.template,
            planned_minutes=r.planned_minutes,
            night_minutes=r.night_minutes,
            status=r.status,
            leave_type=LeaveType(r.leave_type) if r.leave_type else None,
            late_minutes=r.late_minutes,
            early_leave_minutes=r.early_leave_minutes,
            overtime_minutes=r.overtime_minutes,
            worked_minutes=r.worked_minutes,
            reason=r.reason,
            fixed_by=r.fixed_by,
            is_exception=r.is_exception,
            flags=[AttendanceFlag(f) for f in r.flags],
        )


class TimesheetDayOut(BaseModel):
    date: dt.date
    is_final: bool
    finalized_by: str | None
    finalized_at: dt.datetime | None
    attendance_method: AttendanceMethod
    rows: list[TimesheetRowOut]


@router.get("/timesheets", summary="Günlük puantaj (boş → dün)")
async def get_day(ctx: Manager, today: TodayDep, date: dt.date | None = None) -> TimesheetDayOut:
    work_date = date or today - dt.timedelta(days=1)
    found = await svc.day(ctx.session, work_date, today=today)
    conf = await svc.settings(ctx.session)
    return TimesheetDayOut(
        date=work_date,
        is_final=found.record is not None,
        finalized_by=found.record.finalized_by_name if found.record else None,
        finalized_at=found.record.created_at if found.record else None,
        attendance_method=AttendanceMethod(conf.method) if conf else AttendanceMethod.NONE,
        rows=[TimesheetRowOut.of(r) for r in found.rows],
    )


class TimesheetFixIn(BaseModel):
    status: str | None = Field(default=None, max_length=20, description="worked · absent")
    late_minutes: int | None = Field(default=None, ge=-1, le=100_000)
    early_leave_minutes: int | None = Field(default=None, ge=-1, le=100_000)
    overtime_minutes: int | None = Field(default=None, ge=-1, le=100_000)
    reason: str | None = Field(default=None, max_length=1000)


@router.patch("/timesheets/{assignment_id}", summary="İstisnayı düzelt (gerekçe zorunlu)")
async def fix_row(
    assignment_id: uuid.UUID,
    body: TimesheetFixIn,
    ctx: Manager,
    current: CurrentUserDep,
    today: TodayDep,
) -> Written[None]:
    assignment = await ctx.session.get(ShiftAssignment, assignment_id, with_for_update=True)
    if assignment is None:
        raise NotFoundError("Puantaj satırı bulunamadı.")
    try:
        await svc.fix(
            ctx.session,
            assignment,
            svc.FixData(
                body.status,
                body.late_minutes,
                body.early_leave_minutes,
                body.overtime_minutes,
                body.reason,
            ),
            actor_name=current.user.full_name,
            today=today,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.TimesheetRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.commit()
    return Written(data=None, message="Puantaj düzeltildi; denetim kaydına yazıldı.")


class TimesheetFinalizeIn(BaseModel):
    date: dt.date | None = None


class TimesheetFinalizedOut(BaseModel):
    date: dt.date
    rows: int


@router.post("/timesheets/finalize", summary="Günü kesinleştir (sonra değişmez)")
async def finalize(
    body: TimesheetFinalizeIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[TimesheetFinalizedOut]:
    if body.date is None:
        raise form_error(FormFieldError({"date": "Gün seçin."}))
    try:
        count = await svc.finalize(
            ctx.session, body.date, today=today, actor_name=current.user.full_name
        )
    except svc.TimesheetRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.commit()
    return Written(
        data=TimesheetFinalizedOut(date=body.date, rows=count),
        message=f"{body.date:%d.%m.%Y} puantajı kesinleşti.",
    )


# --- Aylık aktarım --------------------------------------------------------------------------


class TimesheetExportRowOut(BaseModel):
    staff_id: uuid.UUID
    payroll_no: str | None
    name: str
    worked_days: int
    worked_hours: str = Field(description='saat, 2 ondalık metin: "6.67"')
    overtime_hours: str
    night_hours: str
    absent_days: int
    leave_annual: int
    leave_excuse: int
    leave_sick: int
    leave_unpaid: int
    holiday_hours: str = Field(description="resmî tatil takvimi (S-03) gelene kadar 0")

    @classmethod
    def of(cls, row: dict[str, Any]) -> TimesheetExportRowOut:
        data = {k: format(v, "f") if isinstance(v, Decimal) else v for k, v in row.items()}
        return cls.model_validate(data)


class TimesheetExportedRefOut(BaseModel):
    id: uuid.UUID
    created_by: str
    created_at: dt.datetime


class TimesheetPreviewOut(BaseModel):
    month: str
    rows: list[TimesheetExportRowOut]
    missing_days: list[dt.date]
    missing_payroll: list[str]
    exported: TimesheetExportedRefOut | None


@router.get("/timesheet-exports/preview", summary="Ay özeti ve aktarım engelleri")
async def export_preview(
    ctx: ExportReader,
    today: TodayDep,
    month: Annotated[
        str | None, Query(max_length=7, description='"2026-10"; boş → geçen ay')
    ] = None,
) -> TimesheetPreviewOut:
    value = month or (today.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    try:
        pv = await svc.preview(ctx.session, value, today=today)
    except FormFieldError as exc:
        raise form_error(exc) from exc
    return TimesheetPreviewOut(
        month=pv.month,
        rows=[TimesheetExportRowOut.of(r) for r in pv.rows],
        missing_days=pv.missing_days,
        missing_payroll=pv.missing_payroll,
        exported=TimesheetExportedRefOut(
            id=pv.exported.id,
            created_by=pv.exported.created_by_name,
            created_at=pv.exported.created_at,
        )
        if pv.exported
        else None,
    )


class TimesheetExportOut(BaseModel):
    id: uuid.UUID
    month: str
    staff_count: int
    created_by: str
    created_at: dt.datetime
    file_name: str

    @classmethod
    def of(cls, x: TimesheetExport, slug: str) -> TimesheetExportOut:
        return cls(
            id=x.id,
            month=x.month,
            staff_count=len(x.rows),
            created_by=x.created_by_name,
            created_at=x.created_at,
            file_name=f"puantaj-{slug}-{x.month}.csv",
        )


class TimesheetExportIn(BaseModel):
    month: str | None = Field(default=None, max_length=7)


@router.post(
    "/timesheet-exports", status_code=status.HTTP_201_CREATED, summary="Aktar (ay kilitlenir)"
)
async def create_export(
    body: TimesheetExportIn, ctx: Manager, current: CurrentUserDep, today: TodayDep
) -> Written[TimesheetExportOut]:
    try:
        item = await svc.export(
            ctx.session, body.month or "", today=today, actor_name=current.user.full_name
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.TimesheetRuleError as exc:
        raise _rule(exc) from exc
    await ctx.session.refresh(item, ["created_at"])
    out = TimesheetExportOut.of(item, ctx.site.slug)
    await ctx.session.commit()
    return Written(
        data=out,
        message=f"{item.month} puantajı {len(item.rows)} personel için dosyaya yazıldı "
        "ve kilitlendi.",
    )


@router.get("/timesheet-exports", summary="Aktarım geçmişi (yeni üstte)")
async def list_exports(ctx: ExportReader, paging: Paging) -> Page[TimesheetExportOut]:
    query = select(TimesheetExport).order_by(TimesheetExport.month.desc())
    total = await ctx.session.scalar(select(func.count()).select_from(TimesheetExport)) or 0
    rows = await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size))
    return Page(
        items=[TimesheetExportOut.of(x, ctx.site.slug) for x in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


@router.get(
    "/timesheet-exports/{export_id}/file",
    summary="Aktarım dosyası (CSV, `;`, UTF-8 BOM)",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_file(export_id: uuid.UUID, ctx: ExportReader) -> Response:
    item = await ctx.session.get(TimesheetExport, export_id)
    if item is None:
        raise NotFoundError("Aktarım bulunamadı.")
    name = f"puantaj-{ctx.site.slug}-{item.month}.csv"
    return Response(
        content=export_csv(item.month, item.rows),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- Giriş-çıkış ayarları -------------------------------------------------------------------


class AttendanceSettingsIn(BaseModel):
    method: str | None = Field(default=None, max_length=20, description="none · qr · location")
    latitude: Decimal | None = Field(default=None, max_digits=12, decimal_places=8)
    longitude: Decimal | None = Field(default=None, max_digits=12, decimal_places=8)
    radius_m: int | None = Field(default=None, ge=0, le=100_000)
    late_tolerance_minutes: int | None = Field(default=None, ge=-1, le=10_000)
    early_tolerance_minutes: int | None = Field(default=None, ge=-1, le=10_000)


class AttendanceSettingsOut(BaseModel):
    method: AttendanceMethod
    qr_code: str | None = Field(description="kulübedeki koda basılacak değer (yalnız staff.manage)")
    qr_rotated_at: dt.datetime | None
    latitude: str | None
    longitude: str | None
    radius_m: int
    late_tolerance_minutes: int
    early_tolerance_minutes: int

    @classmethod
    def of(cls, item: Any) -> AttendanceSettingsOut:
        if item is None:
            return cls(
                method=AttendanceMethod.NONE,
                qr_code=None,
                qr_rotated_at=None,
                latitude=None,
                longitude=None,
                radius_m=svc.DEFAULT_RADIUS_M,
                late_tolerance_minutes=svc.DEFAULT_TOLERANCE,
                early_tolerance_minutes=svc.DEFAULT_TOLERANCE,
            )
        return cls(
            method=AttendanceMethod(item.method),
            qr_code=item.qr_code,
            qr_rotated_at=item.qr_rotated_at,
            latitude=format(item.latitude, "f") if item.latitude is not None else None,
            longitude=format(item.longitude, "f") if item.longitude is not None else None,
            radius_m=item.radius_m,
            late_tolerance_minutes=item.late_tolerance_minutes,
            early_tolerance_minutes=item.early_tolerance_minutes,
        )


@router.get("/attendance-settings", summary="Vardiya giriş-çıkış doğrulama ayarı")
async def get_settings(ctx: Manager) -> AttendanceSettingsOut:
    return AttendanceSettingsOut.of(await svc.settings(ctx.session))


@router.put("/attendance-settings", summary="Doğrulama yöntemi, konum, tolerans")
async def put_settings(
    body: AttendanceSettingsIn, ctx: Manager, now: NowDep
) -> Written[AttendanceSettingsOut]:
    try:
        item = await svc.save_settings(
            ctx.session,
            svc.SettingsData(
                body.method,
                body.latitude,
                body.longitude,
                body.radius_m,
                body.late_tolerance_minutes,
                body.early_tolerance_minutes,
            ),
            now=now,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    out = AttendanceSettingsOut.of(item)
    await ctx.session.commit()
    return Written(data=out, message="Giriş-çıkış ayarı kaydedildi.")


@router.post("/attendance-settings/rotate-code", summary="Kulübe kodunu yenile")
async def rotate_code(ctx: Manager, now: NowDep) -> Written[AttendanceSettingsOut]:
    item = await svc.rotate_code(ctx.session, now=now)
    out = AttendanceSettingsOut.of(item)
    await ctx.session.commit()
    return Written(
        data=out, message="Kod yenilendi; eski kod artık doğrulanmaz. Yeni kodu kulübeye asın."
    )
