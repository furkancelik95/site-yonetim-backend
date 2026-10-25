"""İnsan kaynakları — Yazılım Dokümanı IK-02..05 (servis istekleri 36–39). KİRACI tabloları.

- Vardiya şablonu silinmez (kapatılır); atama, puantajı kesinleşmemiş günde kaldırılabilir.
- İzin silinmez; reddedilir ya da iptal edilir. **Raporlu izinde tanı tutulmaz**; izin türü ve
  açıklaması denetim kaydına maskeli yazılır (sağlık verisi olabilir — `domain/masking.py`).
- Puantaj canlı hesaplanır (plan + onaylı izin + düzeltme + giriş-çıkış); gün kesinleşince
  satırlar `timesheet_lines`'a donar ve **değişmez** (tetikleyici). Aktarım da değişmez.
- Giriş-çıkış kaydı değişmez; konum saklanmaz, yalnız tesise uzaklık (metre) tutulur.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.staffing import (
    AttendanceKind,
    AttendanceMethod,
    LeaveStatus,
    LeaveType,
    TimesheetStatus,
)

COORDINATE = Numeric(9, 6)


class ShiftTemplate(TenantMixin, Base):
    __tablename__ = "shift_templates"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "name"),
        CheckConstraint("break_minutes BETWEEN 0 AND 180", name="break_minutes"),
        CheckConstraint("start_time <> end_time", name="times"),
    )

    name: Mapped[str] = mapped_column(Text)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    break_minutes: Mapped[int]
    position: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class ShiftAssignment(TenantMixin, Base):
    """Personel × gün × şablon. Yayından sonra eklenen `changed_after_publish`."""

    __tablename__ = "shift_assignments"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "staff_id", "work_date", "template_id"),
        tenant_fk("staff_id", "staff_members"),
        tenant_fk("template_id", "shift_templates"),
        Index("ix_shift_assignments_site_date", "site_id", "work_date"),
    )

    staff_id: Mapped[uuid.UUID]
    template_id: Mapped[uuid.UUID]
    work_date: Mapped[dt.date] = mapped_column(Date)
    changed_after_publish: Mapped[bool] = mapped_column(default=False)
    created_by_name: Mapped[str | None] = mapped_column(Text)


class ShiftWeek(TenantMixin, Base):
    """Haftanın yayın kaydı (pazartesi). Yayından sonra kaldırılan atamalar sayılır."""

    __tablename__ = "shift_weeks"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "week_start"),
        CheckConstraint("extract(isodow from week_start) = 1", name="monday"),
    )

    week_start: Mapped[dt.date] = mapped_column(Date)
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    published_by_name: Mapped[str] = mapped_column(Text)
    removed_after_publish: Mapped[int] = mapped_column(default=0)


class LeaveRequest(TenantMixin, Base):
    __tablename__ = "leave_requests"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("leave_type", LeaveType), name="leave_type"),
        CheckConstraint(enum_check("status", LeaveStatus), name="status"),
        CheckConstraint("date_to >= date_from", name="dates"),
        CheckConstraint("leave_type <> 'sick' OR leave_note IS NULL", name="sick_no_note"),
        tenant_fk("staff_id", "staff_members"),
        tenant_fk("approval_request_id", "approval_requests"),
        Index("ix_leave_requests_site_staff", "site_id", "staff_id", "date_from"),
    )

    staff_id: Mapped[uuid.UUID]
    leave_type: Mapped[str] = mapped_column(Text)
    date_from: Mapped[dt.date] = mapped_column(Date)
    date_to: Mapped[dt.date] = mapped_column(Date)
    days: Mapped[int]
    leave_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=LeaveStatus.PENDING.value)
    source: Mapped[str] = mapped_column(Text)  # manager (İK adına) · staff (personel kendisi)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]


class TimesheetFix(TenantMixin, Base):
    """İK düzeltmesi (gerekçeli, denetimli). Gün kesinleşince değişmez (servis kontrol eder)."""

    __tablename__ = "timesheet_fixes"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "assignment_id"),
        CheckConstraint("status IN ('worked', 'absent')", name="status"),
        CheckConstraint(
            "late_minutes BETWEEN 0 AND 960 AND early_leave_minutes BETWEEN 0 AND 960 "
            "AND overtime_minutes BETWEEN 0 AND 960",
            name="minutes",
        ),
        tenant_fk("assignment_id", "shift_assignments"),
    )

    assignment_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(Text)
    late_minutes: Mapped[int] = mapped_column(default=0)
    early_leave_minutes: Mapped[int] = mapped_column(default=0)
    overtime_minutes: Mapped[int] = mapped_column(default=0)
    reason: Mapped[str] = mapped_column(Text)
    fixed_by_name: Mapped[str] = mapped_column(Text)


class TimesheetDay(TenantMixin, Base):
    """Kesinleşen gün. Değişmez."""

    __tablename__ = "timesheet_days"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id", "work_date"))

    work_date: Mapped[dt.date] = mapped_column(Date)
    finalized_by_name: Mapped[str] = mapped_column(Text)


class TimesheetLine(TenantMixin, Base):
    """Kesinleşen günün donmuş satırı (aktarım bunları okur). Değişmez."""

    __tablename__ = "timesheet_lines"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("status", TimesheetStatus), name="status"),
        CheckConstraint(
            "leave_type IS NULL OR " + enum_check("leave_type", LeaveType), name="leave_type"
        ),
        tenant_fk("staff_id", "staff_members"),
        Index("ix_timesheet_lines_site_date", "site_id", "work_date"),
    )

    work_date: Mapped[dt.date] = mapped_column(Date)
    staff_id: Mapped[uuid.UUID]
    assignment_id: Mapped[uuid.UUID | None]  # izinli ve vardiyasız satırda boş
    template_label: Mapped[str | None] = mapped_column(Text)
    planned_minutes: Mapped[int]
    night_minutes: Mapped[int]
    status: Mapped[str] = mapped_column(Text)
    leave_type: Mapped[str | None] = mapped_column(Text)
    late_minutes: Mapped[int]
    early_leave_minutes: Mapped[int]
    overtime_minutes: Mapped[int]
    worked_minutes: Mapped[int]
    reason: Mapped[str | None] = mapped_column(Text)
    fixed_by_name: Mapped[str | None] = mapped_column(Text)
    is_exception: Mapped[bool]


class TimesheetExport(TenantMixin, Base):
    """Aylık puantaj aktarımı (Mod P: dosya). Ay kilitlenir; kayıt değişmez."""

    __tablename__ = "timesheet_exports"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id", "month"))

    month: Mapped[str] = mapped_column(Text)  # "2026-10"
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    created_by_name: Mapped[str] = mapped_column(Text)


class AttendanceSettings(TenantMixin, Base):
    """Vardiya giriş-çıkış doğrulaması (IK-04 Ayarlar). Site başına tek satır."""

    __tablename__ = "attendance_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint(enum_check("method", AttendanceMethod), name="method"),
        CheckConstraint(
            "method <> 'location' OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="location",
        ),
        CheckConstraint("radius_m BETWEEN 20 AND 5000", name="radius"),
        CheckConstraint(
            "late_tolerance_minutes BETWEEN 0 AND 120 "
            "AND early_tolerance_minutes BETWEEN 0 AND 120",
            name="tolerance",
        ),
    )

    method: Mapped[str] = mapped_column(Text, default=AttendanceMethod.NONE.value)
    qr_code: Mapped[str] = mapped_column(Text)
    qr_rotated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    latitude: Mapped[Decimal | None] = mapped_column(COORDINATE)
    longitude: Mapped[Decimal | None] = mapped_column(COORDINATE)
    radius_m: Mapped[int] = mapped_column(default=200)
    late_tolerance_minutes: Mapped[int] = mapped_column(default=10)
    early_tolerance_minutes: Mapped[int] = mapped_column(default=10)


class AttendanceEvent(TenantMixin, Base):
    """Personelin vardiya giriş/çıkışı (mobil). Değişmez; konum saklanmaz."""

    __tablename__ = "attendance_events"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("kind", AttendanceKind), name="kind"),
        CheckConstraint(enum_check("method", AttendanceMethod), name="method"),
        tenant_fk("staff_id", "staff_members"),
        tenant_fk("assignment_id", "shift_assignments"),
        Index("ix_attendance_events_site_assignment", "site_id", "assignment_id"),
    )

    staff_id: Mapped[uuid.UUID]
    assignment_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # cihaz saati
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))  # sunucu saati
    verified: Mapped[bool]
    distance_m: Mapped[int | None]
