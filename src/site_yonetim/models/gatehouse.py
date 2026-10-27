"""Kapı işlerinin genişlemesi — servis istekleri 40–45 (Yazılım Dokümanı GUV-02..08, OTP-02).
Hepsi KİRACI tablosu.

Kişisel veri: ziyaretçi adı, plaka, not. Saklama süresi site ayarıdır (`security_settings`,
K8 varsayılanı 6 ay); süre dolunca gece işi anonimleştirir (`anonymized_at`) — iş varsayılan
kapalıdır (`auto_anonymize`). Ön davet kodu **yalnız özeti** (HMAC) olarak saklanır.
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.gatehouse import (
    DecisionSource,
    GateDirection,
    GateKind,
    InviteStatus,
    InviteType,
    ParcelMethod,
    PlateResult,
    ShiftLogKind,
    UnannouncedMethod,
    ViolationNotice,
    VisitRequestStatus,
)
from site_yonetim.domain.security import VisitorKind


class Gate(TenantMixin, Base):
    """Kapı: ad (site içinde tekil, harf duyarsız), tip, bağlı bloklar (boş = tümü), 7/24."""

    __tablename__ = "gates"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "name_key"),
        CheckConstraint(enum_check("kind", GateKind), name="kind"),
    )

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    block_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    always_open: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)


class SecuritySettings(TenantMixin, Base):
    """Sitenin güvenlik ayarları — site başına tek satır, her kayıtta `version` artar (geçmiş
    denetim kaydında). Satır yoksa varsayılanlar geçerlidir."""

    __tablename__ = "security_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint(enum_check("unannounced_method", UnannouncedMethod), name="method"),
        CheckConstraint(enum_check("parcel_method", ParcelMethod), name="parcel_method"),
        CheckConstraint("approval_timeout_seconds BETWEEN 30 AND 600", name="timeout"),
        CheckConstraint("retention_months BETWEEN 1 AND 24", name="retention"),
        CheckConstraint("parcel_long_wait_days BETWEEN 1 AND 60", name="long_wait"),
        CheckConstraint("guest_parking_capacity BETWEEN 0 AND 500", name="guest_parking"),
    )

    unannounced_method: Mapped[str] = mapped_column(Text)
    approval_timeout_seconds: Mapped[int]
    gate_methods: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    parcel_method: Mapped[str] = mapped_column(Text)
    visitor_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    retention_months: Mapped[int]
    auto_anonymize: Mapped[bool] = mapped_column(default=False)
    parcel_long_wait_days: Mapped[int]
    guest_parking_capacity: Mapped[int]
    version: Mapped[int] = mapped_column(default=1)
    updated_by: Mapped[str | None] = mapped_column(Text)


class VisitorInvite(TenantMixin, Base):
    """Sakinin ön daveti (GUV-02). Kod yalnız özet (`code_hash`); etkin davetler arasında tekil."""

    __tablename__ = "visitor_invites"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("created_by_person_id", "persons"),
        tenant_fk("visitor_id", "visitors"),
        CheckConstraint(enum_check("type", InviteType), name="type"),
        CheckConstraint(enum_check("status", InviteStatus), name="status"),
        CheckConstraint("date_to >= date_from", name="dates"),
        CheckConstraint("(time_from IS NULL) = (time_to IS NULL)", name="times"),
        Index(
            "uq_visitor_invites_active_code",
            "site_id",
            "code_hash",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_visitor_invites_site_unit", "site_id", "unit_id"),
        Index("ix_visitor_invites_site_dates", "site_id", "date_to", "date_from"),
    )

    unit_id: Mapped[uuid.UUID]
    created_by_person_id: Mapped[uuid.UUID | None]
    created_by_user_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)
    visitor_name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    date_from: Mapped[dt.date] = mapped_column(Date)
    date_to: Mapped[dt.date] = mapped_column(Date)
    weekdays: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)
    time_from: Mapped[dt.time | None] = mapped_column(Time)
    time_to: Mapped[dt.time | None] = mapped_column(Time)
    plate: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    code_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=InviteStatus.ACTIVE.value)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    visitor_id: Mapped[uuid.UUID | None]  # son girişin ziyaretçi kaydı
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class VisitRequest(TenantMixin, Base):
    """Habersiz ziyaretçi (GUV-03): bilgi yönteminde hemen `allowed`; sakin onayında `awaiting`
    → sakin (süre içinde) ya da görevli (süre dolunca, gerekçeli) karar verir."""

    __tablename__ = "visit_requests"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("gate_id", "gates"),
        tenant_fk("visitor_id", "visitors"),
        CheckConstraint(enum_check("kind", VisitorKind), name="kind"),
        CheckConstraint(enum_check("method", UnannouncedMethod), name="method"),
        CheckConstraint(enum_check("status", VisitRequestStatus), name="status"),
        CheckConstraint(
            f"decision_source IS NULL OR {enum_check('decision_source', DecisionSource)}",
            name="decision_source",
        ),
        CheckConstraint(
            "decision_source IS DISTINCT FROM 'guard' OR reason IS NOT NULL", name="guard_reason"
        ),
        Index("ix_visit_requests_site_day", "site_id", "visit_date"),
        Index("ix_visit_requests_site_status", "site_id", "status"),
    )

    unit_id: Mapped[uuid.UUID]
    gate_id: Mapped[uuid.UUID | None]
    full_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default=VisitorKind.GUEST.value)
    plate: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    visit_date: Mapped[dt.date] = mapped_column(Date)  # tesis yerel günü
    created_by: Mapped[str] = mapped_column(Text)
    decision_source: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_user_id: Mapped[uuid.UUID | None]
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    visitor_id: Mapped[uuid.UUID | None]
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class GateLog(TenantMixin, Base):
    """Plaka giriş-çıkış kaydı (GUV-04). Sorgu sonucu kayıt anında dondurulur."""

    __tablename__ = "gate_logs"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("gate_id", "gates"),
        CheckConstraint(enum_check("result", PlateResult), name="result"),
        CheckConstraint(enum_check("direction", GateDirection), name="direction"),
        Index("ix_gate_logs_site_day", "site_id", "log_date", "at"),
    )

    plate: Mapped[str | None] = mapped_column(Text)  # anonimleşince boş
    result: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    unit_id: Mapped[uuid.UUID | None]
    gate_id: Mapped[uuid.UUID | None]
    direction: Mapped[str] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    log_date: Mapped[dt.date] = mapped_column(Date)
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ParkingViolation(TenantMixin, Base):
    """Otopark ihlali (OTP-02): güvenlik doğrulamasıyla kaydedilir; bölüme bağlı plakada sakine,
    değilse yönetime uygulama içi bildirim (dış kanal K5)."""

    __tablename__ = "parking_violations"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        CheckConstraint(enum_check("result", PlateResult), name="result"),
        CheckConstraint(enum_check("notified", ViolationNotice), name="notified"),
        Index("ix_parking_violations_site_at", "site_id", "occurred_at"),
    )

    plate: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    unit_id: Mapped[uuid.UUID | None]
    notified: Mapped[str] = mapped_column(Text)
    recorded_by: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ParcelDelegate(TenantMixin, Base):
    """Kargo teslim yetkilisi (GUV-05): bölüm başına en fazla 3 etkin kişi; yalnız ad."""

    __tablename__ = "parcel_delegates"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("created_by_person_id", "persons"),
        Index("ix_parcel_delegates_site_unit", "site_id", "unit_id", "is_active"),
    )

    unit_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(Text)
    created_by_person_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
    removed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[str | None] = mapped_column(Text)


class ShiftLogEntry(TenantMixin, Base):
    """Nöbet defteri kaydı (GUV-06). **Değişmez, silinmez** (forbid_history_change); düzeltme ek
    notla, devir onayı ayrı satırla."""

    __tablename__ = "shift_log_entries"
    __table_args__ = tenant_table_args(
        tenant_fk("gate_id", "gates"),
        CheckConstraint(enum_check("kind", ShiftLogKind), name="kind"),
        CheckConstraint("char_length(text) BETWEEN 5 AND 1000", name="text_length"),
        Index("ix_shift_log_entries_site_day", "site_id", "log_date", "at"),
    )

    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    gate_id: Mapped[uuid.UUID | None]
    author_user_id: Mapped[uuid.UUID]
    author_name: Mapped[str] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    log_date: Mapped[dt.date] = mapped_column(Date)


class ShiftLogAddendum(TenantMixin, Base):
    """Nöbet defteri ek notu — değişmez."""

    __tablename__ = "shift_log_addenda"
    __table_args__ = tenant_table_args(
        tenant_fk("entry_id", "shift_log_entries"),
        Index("ix_shift_log_addenda_site_entry", "site_id", "entry_id"),
    )

    entry_id: Mapped[uuid.UUID]
    text: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[uuid.UUID]
    author_name: Mapped[str] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class ShiftHandoverAck(TenantMixin, Base):
    """Vardiya devrinin onayı ("Okudum, devraldım") — devir başına bir kez; değişmez."""

    __tablename__ = "shift_handover_acks"
    __table_args__ = tenant_table_args(
        tenant_fk("entry_id", "shift_log_entries"),
        UniqueConstraint("site_id", "entry_id"),
    )

    entry_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    user_name: Mapped[str] = mapped_column(Text)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
