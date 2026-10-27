"""Hizmetler ve talep akışı — Yazılım Dokümanı §12 (HIZ-01..05), servis istekleri 47–52.
Hepsi KİRACI tablosu.

Değişmez (göçteki `forbid_history_change` tetikleyicisi): `request_internal_notes`,
`request_feedback` — iç not ve sakin değerlendirmesi sonradan yazılamaz/silinemez.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.operations import RequestPriority
from site_yonetim.domain.service_desk import HourType, PermitStatus, PermitType, QuoteStatus
from site_yonetim.models.finance import MONEY

_HOURS = "BETWEEN 1 AND 2160"


class RequestCategory(TenantMixin, Base):
    """Site tanımlı talep kategorisi (HIZ-03, docs/12 D-26). `key` değişmez — talepler ona bağlı;
    silinmez, pasifleştirilir (pasife yeni talep açılmaz)."""

    __tablename__ = "request_categories"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "key"),
        UniqueConstraint("site_id", "name_key"),
        tenant_fk("department_id", "request_departments"),
        CheckConstraint(enum_check("hour_type", HourType), name="hour_type"),
        CheckConstraint(enum_check("default_priority", RequestPriority), name="default_priority"),
        CheckConstraint(
            f"hours_low {_HOURS} AND hours_normal {_HOURS} AND hours_high {_HOURS} "
            f"AND hours_urgent {_HOURS}",
            name="hours",
        ),
    )

    key: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    hour_type: Mapped[str] = mapped_column(Text)
    hours_low: Mapped[int]
    hours_normal: Mapped[int]
    hours_high: Mapped[int]
    hours_urgent: Mapped[int]
    default_priority: Mapped[str] = mapped_column(Text, default=RequestPriority.NORMAL.value)
    department_id: Mapped[uuid.UUID | None]  # atanacak ekip (HIZ-03 adım 1)
    is_active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)


class RequestSettings(TenantMixin, Base):
    """Sitenin talep ayarları — tek satır (servis isteği 47)."""

    __tablename__ = "request_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("auto_close_days BETWEEN 1 AND 30", name="auto_close_days"),
        CheckConstraint("merge_window_hours BETWEEN 1 AND 168", name="merge_window_hours"),
        CheckConstraint("low_rating_threshold BETWEEN 1 AND 4", name="low_rating_threshold"),
    )

    calendar: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)  # 7 gün, 0 = pazartesi
    auto_close_days: Mapped[int]
    merge_window_hours: Mapped[int]
    low_rating_threshold: Mapped[int]


class ReplyTemplate(TenantMixin, Base):
    """Hazır yanıt şablonu (HIZ-02, §12.3). `category` boş = tüm kategoriler."""

    __tablename__ = "reply_templates"
    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["site_id", "category"], ["request_categories.site_id", "request_categories.key"]
        ),
    )

    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class RequestInternalNote(TenantMixin, Base):
    """İç not (K26) — yalnız personel; sakin kanalında hiç dönmez. **Değişmez.**"""

    __tablename__ = "request_internal_notes"
    __table_args__ = tenant_table_args(tenant_fk("request_id", "requests"))

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    body: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    author_name: Mapped[str] = mapped_column(Text)


class RequestFeedback(TenantMixin, Base):
    """Sakin kapanış onayı ve puanı (HIZ-01 adım 7, §12.3). **Değişmez**; talep başına birden çok
    olabilir (devam ediyor → yeniden çözüldü → onay); en sonuncusu geçerli."""

    __tablename__ = "request_feedback"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "requests"),
        tenant_fk("person_id", "persons"),
        CheckConstraint(
            "(solved AND score BETWEEN 1 AND 5) OR (NOT solved AND score IS NULL)", name="score"
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    solved: Mapped[bool]
    score: Mapped[int | None]
    comment: Mapped[str | None] = mapped_column(Text)
    person_id: Mapped[uuid.UUID | None]
    by_name: Mapped[str] = mapped_column(Text)


class Outage(TenantMixin, Base):
    """Ana olay (HIZ-02, §12.3; K27: `incidents` güvenlik olayı olarak kalır). Bağlı talepler
    `requests.outage_id`; çözülünce bağlı açık talepler "çözüldü" olur."""

    __tablename__ = "outages"
    __table_args__ = tenant_table_args(
        ForeignKeyConstraint(
            ["site_id", "category"], ["request_categories.site_id", "request_categories.key"]
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="status"),
        CheckConstraint(
            "status <> 'resolved' OR (resolution IS NOT NULL AND resolved_at IS NOT NULL)",
            name="resolution_required",
        ),
        Index("ix_outages_site_status", "site_id", "status"),
    )

    title: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    public_description: Mapped[str] = mapped_column(Text)  # sakinlerin gördüğü genel açıklama
    status: Mapped[str] = mapped_column(Text, default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_name: Mapped[str] = mapped_column(Text)
    resolved_by_name: Mapped[str | None] = mapped_column(Text)


class ServiceTariff(TenantMixin, Base):
    """Ücretli hizmet tarifesi (HIZ-04): kalem, birim, birim fiyat."""

    __tablename__ = "service_tariffs"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "name_key"),
        CheckConstraint("price > 0", name="price_positive"),
    )

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(MONEY)
    is_active: Mapped[bool] = mapped_column(default=True)


class ServiceQuote(TenantMixin, Base):
    """Ücretli hizmet teklifi (HIZ-04). Ücret yalnız sakin onayıyla, iş bitince cariye yazılır
    (`ledger_entry_id`, kaynak `service`). Hesap teklif anında belirlenir."""

    __tablename__ = "service_quotes"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "requests"),
        tenant_fk("unit_id", "units"),
        tenant_fk("account_id", "ledger_accounts"),
        tenant_fk("ledger_entry_id", "ledger_entries"),
        CheckConstraint(enum_check("status", QuoteStatus), name="status"),
        CheckConstraint("total > 0", name="total_positive"),
        CheckConstraint(
            "status <> 'billed' OR (billed_total IS NOT NULL AND ledger_entry_id IS NOT NULL)",
            name="billed",
        ),
        # Talepte tek açık teklif (servis isteği 51: 409 quote_open).
        Index(
            "uq_service_quotes_open",
            "request_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'accepted', 'extra_pending')"),
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    total: Mapped[Decimal] = mapped_column(MONEY)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=QuoteStatus.PENDING.value)
    actual_total: Mapped[Decimal | None] = mapped_column(MONEY)
    billed_total: Mapped[Decimal | None] = mapped_column(MONEY)
    ledger_entry_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_name: Mapped[str | None] = mapped_column(Text)


class ServiceQuoteLine(TenantMixin, Base):
    __tablename__ = "service_quote_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("quote_id", "service_quotes"),
        UniqueConstraint("quote_id", "line_no"),
        CheckConstraint("qty > 0 AND unit_price > 0 AND total > 0", name="positive"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(index=True)
    line_no: Mapped[int]
    description: Mapped[str] = mapped_column(Text)
    qty: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)


class PermitSettings(TenantMixin, Base):
    """Tadilat kuralları — tek satır (HIZ-05 "tesis ayarı")."""

    __tablename__ = "permit_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("deposit_amount >= 0", name="deposit_not_negative"),
    )

    weekday_hours: Mapped[str] = mapped_column(Text)  # "09:00-18:00"
    saturday_hours: Mapped[str | None] = mapped_column(Text)  # boş: cumartesi yasak
    sunday_allowed: Mapped[bool] = mapped_column(default=False)
    deposit_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    checklist: Mapped[list[str]] = mapped_column(ARRAY(Text))


class Permit(TenantMixin, Base):
    """Tadilat / taşınma başvurusu (HIZ-05). Onay "Tadilat / taşınma izni" onay kuralından geçer
    (kural varsa); depozito malik hesabına `deposit` kaynağıyla, iadesi ters kayıtla."""

    __tablename__ = "permits"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        tenant_fk("deposit_entry_id", "ledger_entries"),
        tenant_fk("refund_entry_id", "ledger_entries"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("type", PermitType), name="type"),
        CheckConstraint(enum_check("status", PermitStatus), name="status"),
        CheckConstraint("date_to >= date_from", name="dates"),
        CheckConstraint(
            "status <> 'rejected' OR reject_reason IS NOT NULL", name="reject_reason_required"
        ),
        Index("ix_permits_site_status", "site_id", "status"),
        Index("ix_permits_unit", "site_id", "unit_id"),
    )

    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID | None]  # başvuran sakin
    type: Mapped[str] = mapped_column(Text)
    date_from: Mapped[dt.date] = mapped_column(Date)
    date_to: Mapped[dt.date] = mapped_column(Date)
    contractor: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=PermitStatus.PENDING.value)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    checklist: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    deposit: Mapped[Decimal | None] = mapped_column(MONEY)
    deposit_entry_id: Mapped[uuid.UUID | None]
    refund_entry_id: Mapped[uuid.UUID | None]
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_by_name: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
