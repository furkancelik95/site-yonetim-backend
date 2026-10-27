"""Operasyon ekleri ve teknik varlık — KİRACI tabloları.

Yazılım Dokümanı OPS-01..06 (servis istekleri 53–58), HIZ-06, HIZ-07. PM kararı **K28**: mali
demirbaş (`assets`, `models/management.py`) ile teknik varlık (`technical_assets`) ayrıdır.

- Hiçbiri silinmez: anahtar/şablon/sayaç pasifleştirilir, poliçe ve hasar dosyası durum alır.
- Değişmez (tetikleyici): saha denetimi (`checklist_inspections`), görev kapanışı
  (`task_completions`), yasal muayene kaydı (`asset_inspection_records`).
- Fotoğraflar `stored_files`'ta (dosya deposu, web kökü dışında); rapor ve poliçe belgeleri belge
  arşivinde (`documents.subject_type/subject_id`).
- Kişisel veri: anahtar alanın telefonu ve emanet yetkili listesi denetim kaydına maskeli yazılır.
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
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.facility import (
    STORED_DEPOSIT_STATUSES,
    ClaimStatus,
    CompletionStatus,
    InspectionResult,
    KeyKind,
    MaintenanceRunStatus,
    MeterKind,
    MeterStatus,
    PeriodUnit,
    PolicyKind,
    ReadingStatus,
    StockCountStatus,
    TaskFrequency,
    TechnicalAssetCategory,
    TechnicalAssetStatus,
)
from site_yonetim.models.finance import MONEY

QUANTITY = Numeric(14, 3)
INDEX = Numeric(14, 3)
_DEPOSIT = ", ".join(f"'{s.value}'" for s in STORED_DEPOSIT_STATUSES)


class OperationSettings(TenantMixin, Base):
    """Site başına tek satır: sayaç sapma eşiği (±%X) ve fotoğraf zorunluluğu (OPS-05)."""

    __tablename__ = "operation_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("meter_anomaly_percent BETWEEN 1 AND 1000", name="anomaly_percent"),
    )

    meter_anomaly_percent: Mapped[int] = mapped_column(default=50)
    meter_photo_required: Mapped[bool] = mapped_column(default=False)


# --- OPS-01 Anahtar ve emanet -----------------------------------------------------------


class FacilityKey(TenantMixin, Base):
    """Ortak alan anahtarı / kartı / kumandası. Kod site içinde benzersiz (büyük harf)."""

    __tablename__ = "facility_keys"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "code"),
        CheckConstraint(enum_check("kind", KeyKind), name="kind"),
    )

    kind: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    area: Mapped[str | None] = mapped_column(Text)
    authorized_roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(default=True)


class KeyLoan(TenantMixin, Base):
    """Teslim kaydı. Bir anahtar aynı anda tek kişide: açık teslim tekil (kısmi indeks)."""

    __tablename__ = "key_loans"
    __table_args__ = tenant_table_args(
        tenant_fk("key_id", "facility_keys"),
        Index(
            "uq_key_loans_open",
            "site_id",
            "key_id",
            unique=True,
            postgresql_where=text("returned_at IS NULL"),
        ),
        Index("ix_key_loans_site_created", "site_id", "created_at"),
    )

    key_id: Mapped[uuid.UUID]
    to_name: Mapped[str] = mapped_column(Text)
    to_phone: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    given_by_name: Mapped[str] = mapped_column(Text)
    returned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    returned_to_name: Mapped[str | None] = mapped_column(Text)


class DepositedKey(TenantMixin, Base):
    """Sakinin yönetime bıraktığı bölüm anahtarı; yalnız yetkili listesindeki kişiye verilir."""

    __tablename__ = "deposited_keys"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("deposited_by_person_id", "persons"),
        CheckConstraint(f"status IN ({_DEPOSIT})", name="status"),
        Index("ix_deposited_keys_site_unit", "site_id", "unit_id"),
    )

    unit_id: Mapped[uuid.UUID]
    deposited_by_person_id: Mapped[uuid.UUID | None]
    deposited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    deposited_by_name: Mapped[str] = mapped_column(Text)
    authorized: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)  # [{name, phone}]
    valid_until: Mapped[dt.date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    received_by_name: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    handed_to: Mapped[str | None] = mapped_column(Text)
    handed_by_name: Mapped[str | None] = mapped_column(Text)
    handed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- OPS-02 Stok sayımı -----------------------------------------------------------------


class StockCount(TenantMixin, Base):
    """Sayım. Fark onaya bağlıysa hareketler onaydan sonra yazılır (`approval_pending`)."""

    __tablename__ = "stock_counts"
    __table_args__ = tenant_table_args(
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("status", StockCountStatus), name="status"),
        Index("ix_stock_counts_site_created", "site_id", "created_at"),
    )

    status: Mapped[str] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class StockCountLine(TenantMixin, Base):
    __tablename__ = "stock_count_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("count_id", "stock_counts"),
        tenant_fk("stock_item_id", "stock_items"),
        UniqueConstraint("count_id", "stock_item_id"),
        CheckConstraint("counted_quantity >= 0", name="counted"),
    )

    count_id: Mapped[uuid.UUID] = mapped_column(index=True)
    stock_item_id: Mapped[uuid.UUID]
    item_name: Mapped[str] = mapped_column(Text)
    system_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    counted_quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    diff: Mapped[Decimal] = mapped_column(QUANTITY)


# --- OPS-03 Denetim listeleri -----------------------------------------------------------


class ChecklistTemplate(TenantMixin, Base):
    """Şablon; maddeler `[{text, type, photo_required, min, max}]`. Silinmez, pasifleşir."""

    __tablename__ = "checklist_templates"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id", "name_key"))

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(default=True)


class ChecklistInspection(TenantMixin, Base):
    """Saha denetimi — **değişmez**. Şablon adı ve maddeler o anki haliyle saklanır."""

    __tablename__ = "checklist_inspections"
    __table_args__ = tenant_table_args(
        tenant_fk("template_id", "checklist_templates"),
        CheckConstraint("score BETWEEN 0 AND 100", name="score"),
        Index("ix_checklist_inspections_site_created", "site_id", "created_at"),
    )

    template_id: Mapped[uuid.UUID]
    template_name: Mapped[str] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    answers: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    score: Mapped[int]
    failed: Mapped[int]
    opened_request_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    by_name: Mapped[str] = mapped_column(Text)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


# --- OPS-04 Sigorta ---------------------------------------------------------------------


class InsurancePolicy(TenantMixin, Base):
    __tablename__ = "insurance_policies"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("kind", PolicyKind), name="kind"),
        CheckConstraint("end_date > start_date", name="dates"),
        CheckConstraint("premium >= 0", name="premium"),
        Index("ix_insurance_policies_site_end", "site_id", "end_date"),
    )

    kind: Mapped[str] = mapped_column(Text)
    insurer: Mapped[str] = mapped_column(Text)
    policy_no: Mapped[str] = mapped_column(Text)
    coverage: Mapped[str | None] = mapped_column(Text)
    premium: Mapped[Decimal] = mapped_column(MONEY)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    area: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class InsuranceClaim(TenantMixin, Base):
    """Hasar dosyası. Tazminat ödendi → tutar ve tarih zorunlu (kısıt)."""

    __tablename__ = "insurance_claims"
    __table_args__ = tenant_table_args(
        tenant_fk("policy_id", "insurance_policies"),
        CheckConstraint(enum_check("status", ClaimStatus), name="status"),
        CheckConstraint(
            "status <> 'paid' OR (paid IS NOT NULL AND paid_on IS NOT NULL)", name="paid"
        ),
        CheckConstraint("claimed IS NULL OR claimed >= 0", name="claimed"),
        CheckConstraint("paid IS NULL OR paid > 0", name="paid_positive"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(index=True)
    source: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    incident_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default=ClaimStatus.OPEN.value)
    claimed: Mapped[Decimal | None] = mapped_column(MONEY)
    paid: Mapped[Decimal | None] = mapped_column(MONEY)
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    created_by_name: Mapped[str] = mapped_column(Text)


# --- OPS-05 Sayaç -----------------------------------------------------------------------


class Meter(TenantMixin, Base):
    """Sayaç kartı. Seri no aktif (değişmemiş) sayaçlar içinde benzersiz; bölüm ya da ortak alan."""

    __tablename__ = "meters"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        ForeignKeyConstraint(["site_id", "replaced_by_id"], ["meters.site_id", "meters.id"]),
        CheckConstraint(enum_check("kind", MeterKind), name="kind"),
        CheckConstraint(enum_check("status", MeterStatus), name="status"),
        CheckConstraint("unit_id IS NOT NULL OR area IS NOT NULL", name="target"),
        CheckConstraint("multiplier > 0", name="multiplier"),
        Index(
            "uq_meters_active_serial",
            "site_id",
            "serial",
            unique=True,
            postgresql_where=text("status <> 'replaced'"),
        ),
    )

    kind: Mapped[str] = mapped_column(Text)
    serial: Mapped[str] = mapped_column(Text)
    unit_id: Mapped[uuid.UUID | None]
    area: Mapped[str | None] = mapped_column(Text)
    unit_label: Mapped[str] = mapped_column(Text)
    multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal(1))
    installed_on: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default=MeterStatus.ACTIVE.value)
    replaced_by_id: Mapped[uuid.UUID | None]


class MeterReading(TenantMixin, Base):
    """Dönem okuması. Bir sayaçta dönem başına tek geçerli okuma (yeniden okunan `reread`)."""

    __tablename__ = "meter_readings"
    __table_args__ = tenant_table_args(
        tenant_fk("meter_id", "meters"),
        tenant_fk("photo_file_id", "stored_files"),
        CheckConstraint(enum_check("status", ReadingStatus), name="status"),
        CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period"),
        CheckConstraint(
            "status = 'reread' OR (status = 'unreadable') = (value IS NULL)",
            name="value_unless_unreadable",
        ),
        Index(
            "uq_meter_readings_valid",
            "site_id",
            "meter_id",
            "period",
            unique=True,
            postgresql_where=text("status <> 'reread'"),
        ),
        Index("ix_meter_readings_site_period", "site_id", "period"),
    )

    meter_id: Mapped[uuid.UUID]
    period: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal | None] = mapped_column(INDEX)
    prev_value: Mapped[Decimal | None] = mapped_column(INDEX)
    consumption: Mapped[Decimal | None] = mapped_column(Numeric(16, 3))
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    anomaly_percent: Mapped[int | None]
    photo_file_id: Mapped[uuid.UUID | None]
    by_name: Mapped[str] = mapped_column(Text)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_by_name: Mapped[str | None] = mapped_column(Text)


# --- OPS-06 Rutin görevler --------------------------------------------------------------


class TaskTemplate(TenantMixin, Base):
    """Görev şablonu. K29: personel kartına bağlanabilir (`staff_member_id`) — "görevlerim"."""

    __tablename__ = "task_templates"
    __table_args__ = tenant_table_args(
        tenant_fk("staff_member_id", "staff_members"),
        tenant_fk("checklist_template_id", "checklist_templates"),
        CheckConstraint(enum_check("frequency", TaskFrequency), name="frequency"),
        CheckConstraint("time_to > time_from", name="times"),
        CheckConstraint("month_day IS NULL OR month_day BETWEEN 1 AND 28", name="month_day"),
    )

    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(Text)
    weekdays: Mapped[list[int]] = mapped_column(JSONB, default=list)
    month_day: Mapped[int | None]
    time_from: Mapped[dt.time] = mapped_column(Time)
    time_to: Mapped[dt.time] = mapped_column(Time)
    location: Mapped[str | None] = mapped_column(Text)
    assignee: Mapped[str] = mapped_column(Text)
    staff_member_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    photo_required: Mapped[bool] = mapped_column(default=False)
    checklist_template_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(default=True)


class TaskCompletion(TenantMixin, Base):
    """Görevin o günkü kapanışı — **değişmez**; bir gün bir görev bir kez kapanır."""

    __tablename__ = "task_completions"
    __table_args__ = tenant_table_args(
        tenant_fk("template_id", "task_templates"),
        tenant_fk("photo_file_id", "stored_files"),
        tenant_fk("staff_member_id", "staff_members"),
        UniqueConstraint("template_id", "task_date"),
        CheckConstraint(enum_check("status", CompletionStatus), name="status"),
        CheckConstraint("status <> 'failed' OR reason IS NOT NULL", name="reason"),
        Index("ix_task_completions_site_date", "site_id", "task_date"),
    )

    template_id: Mapped[uuid.UUID]
    task_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    photo_file_id: Mapped[uuid.UUID | None]
    by_name: Mapped[str] = mapped_column(Text)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    staff_member_id: Mapped[uuid.UUID | None]


# --- HIZ-07 Teknik varlık ---------------------------------------------------------------


class TechnicalAsset(TenantMixin, Base):
    """Teknik varlık kartı (asansör, jeneratör…). Kod `TV-0001`, değişmez. Silinmez."""

    __tablename__ = "technical_assets"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "sequence"),
        tenant_fk("contract_id", "contracts"),
        CheckConstraint(enum_check("category", TechnicalAssetCategory), name="category"),
        CheckConstraint(enum_check("status", TechnicalAssetStatus), name="status"),
    )

    sequence: Mapped[int]
    code: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    serial_no: Mapped[str | None] = mapped_column(Text)
    installed_on: Mapped[dt.date | None] = mapped_column(Date)
    warranty_until: Mapped[dt.date | None] = mapped_column(Date)
    contract_id: Mapped[uuid.UUID | None]
    service_company: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=TechnicalAssetStatus.WORKING.value)
    note: Mapped[str | None] = mapped_column(Text)


class AssetInspectionSchedule(TenantMixin, Base):
    """Varlığın yasal muayene takvimi: periyot + son muayene → sonraki tarih (saklanır)."""

    __tablename__ = "asset_inspection_schedules"
    __table_args__ = tenant_table_args(
        tenant_fk("asset_id", "technical_assets"),
        CheckConstraint("period_months BETWEEN 1 AND 120", name="period"),
        Index("ix_asset_inspection_schedules_site_due", "site_id", "next_due_on"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(index=True)
    template_key: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    period_months: Mapped[int]
    last_inspected_on: Mapped[dt.date | None] = mapped_column(Date)
    next_due_on: Mapped[dt.date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(default=True)


class AssetInspectionRecord(TenantMixin, Base):
    """Muayene sonucu — **değişmez**; rapor belgesi zorunlu (belge arşivinde)."""

    __tablename__ = "asset_inspection_records"
    __table_args__ = tenant_table_args(
        tenant_fk("schedule_id", "asset_inspection_schedules"),
        tenant_fk("asset_id", "technical_assets"),
        tenant_fk("report_document_id", "documents"),
        tenant_fk("request_id", "requests"),
        CheckConstraint(enum_check("result", InspectionResult), name="result"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost"),
    )

    schedule_id: Mapped[uuid.UUID]
    asset_id: Mapped[uuid.UUID] = mapped_column(index=True)
    inspected_on: Mapped[dt.date] = mapped_column(Date)
    result: Mapped[str] = mapped_column(Text)
    inspector: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    cost: Mapped[Decimal | None] = mapped_column(MONEY)
    report_document_id: Mapped[uuid.UUID]
    request_id: Mapped[uuid.UUID | None]
    by_name: Mapped[str] = mapped_column(Text)


# --- HIZ-06 Periyodik bakım -------------------------------------------------------------


class MaintenancePlan(TenantMixin, Base):
    __tablename__ = "maintenance_plans"
    __table_args__ = tenant_table_args(
        tenant_fk("asset_id", "technical_assets"),
        tenant_fk("contract_id", "contracts"),
        tenant_fk("checklist_template_id", "checklist_templates"),
        CheckConstraint(enum_check("period_unit", PeriodUnit), name="period_unit"),
        CheckConstraint("period_count BETWEEN 1 AND 60", name="period_count"),
        Index("ix_maintenance_plans_site_due", "site_id", "next_due_on"),
    )

    asset_id: Mapped[uuid.UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    period_unit: Mapped[str] = mapped_column(Text)
    period_count: Mapped[int]
    next_due_on: Mapped[dt.date] = mapped_column(Date)
    responsible: Mapped[str | None] = mapped_column(Text)
    contract_id: Mapped[uuid.UUID | None]
    checklist_template_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(default=True)


class MaintenanceRun(TenantMixin, Base):
    """Planın bir dönemi: planlanan tarihte iş emri (talep) açılır; bitince geçmişe yazılır."""

    __tablename__ = "maintenance_runs"
    __table_args__ = tenant_table_args(
        tenant_fk("plan_id", "maintenance_plans"),
        tenant_fk("asset_id", "technical_assets"),
        tenant_fk("request_id", "requests"),
        tenant_fk("issue_request_id", "requests"),
        UniqueConstraint("plan_id", "due_on"),
        CheckConstraint(enum_check("status", MaintenanceRunStatus), name="status"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="cost"),
        CheckConstraint("status = 'open' OR done_on IS NOT NULL", name="done_on"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(index=True)
    due_on: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default=MaintenanceRunStatus.OPEN.value)
    request_id: Mapped[uuid.UUID | None]
    done_on: Mapped[dt.date | None] = mapped_column(Date)
    cost: Mapped[Decimal | None] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(Text)
    issue_request_id: Mapped[uuid.UUID | None]
    done_by_name: Mapped[str | None] = mapped_column(Text)
