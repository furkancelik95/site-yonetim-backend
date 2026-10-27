"""İletişim, acil durum ve şeffaflık — Yazılım Dokümanı ILT-02, ILT-04, ILT-06, RAP-02.
Hepsi KİRACI tablosu (RLS).

- `notification_template_versions`: site şablon sürümleri — **değişmez** (her kayıt yeni sürüm;
  sistem metnine dönüş de yeni sürümdür). Satır yoksa sürüm 1 = sistem metni.
- `emergencies`: acil durum modu; aynı anda tek aktif (kısmi benzersiz dizin). Başlatma/bitirme
  denetim kaydına yazılır. `emergency_units`: kapsam başlatma anında sabitlenir (bölüm ve blok
  adı kopyası); "güvendeyim" ve yoklama verisi bitişten `retention_days` gün sonra silinir
  (gece işi, yalnız `auto_purge` açıksa — K8 / A-04 hukuk teyidi bekliyor).
- `emergency_info_cards`: site başına tek kart; her yayında `version` artar.
- `transparency_reports`: kişi/bölüm içermeyen aylık gelir–gider özeti; içerik hazırlanırken
  donar (`content`). Yayın "Şeffaflık raporu" onay kuralından geçer.
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.communication import (
    RETENTION_DAYS_MAX,
    RETENTION_DAYS_MIN,
    EmergencyKind,
    EmergencyScope,
    SafetyMethod,
    TransparencyStatus,
    UnitSafety,
)


class NotificationTemplateVersion(TenantMixin, Base):
    __tablename__ = "notification_template_versions"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "code", "version"),
        CheckConstraint("version >= 1", name="version"),
    )

    code: Mapped[str] = mapped_column(Text)
    version: Mapped[int]
    variants: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)  # [{lang, subject, body}]
    customized: Mapped[bool]
    created_by: Mapped[str] = mapped_column(Text)


class Emergency(TenantMixin, Base):
    __tablename__ = "emergencies"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("kind", EmergencyKind), name="kind"),
        CheckConstraint(enum_check("scope", EmergencyScope), name="scope"),
        Index(
            "uq_emergencies_one_active",
            "site_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    kind: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    block_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    message: Mapped[str] = mapped_column(Text)
    started_by: Mapped[str] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict[str, int] | None] = mapped_column(JSONB)  # bitişte: durum → sayı
    purge_on: Mapped[dt.date | None] = mapped_column(Date)
    purged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EmergencyUnit(TenantMixin, Base):
    """Kapsamdaki bölüm ve son durumu (son yanıt geçerli). Kişisel veri: yanıtlayan adı, not."""

    __tablename__ = "emergency_units"
    __table_args__ = tenant_table_args(
        tenant_fk("emergency_id", "emergencies"),
        tenant_fk("unit_id", "units"),
        UniqueConstraint("emergency_id", "unit_id"),
        CheckConstraint(enum_check("status", UnitSafety), name="status"),
        CheckConstraint(f"method IS NULL OR {enum_check('method', SafetyMethod)}", name="method"),
    )

    emergency_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID]
    unit_name: Mapped[str] = mapped_column(Text)
    block_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    block_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=UnitSafety.NONE.value)
    note: Mapped[str | None] = mapped_column(Text)
    muster_point: Mapped[str | None] = mapped_column(Text)
    responded_by: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EmergencySettings(TenantMixin, Base):
    """Acil durum verisi saklama (K8): süre doküman varsayılanı 30 gün; otomatik silme hukuk
    teyidine kadar kapalı başlar."""

    __tablename__ = "emergency_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint(
            f"retention_days BETWEEN {RETENTION_DAYS_MIN} AND {RETENTION_DAYS_MAX}",
            name="retention",
        ),
    )

    retention_days: Mapped[int]
    auto_purge: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[str | None] = mapped_column(Text)


class EmergencyInfoCard(TenantMixin, Base):
    __tablename__ = "emergency_info_cards"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id"))

    muster_points: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    phones: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    instructions: Mapped[dict[str, str]] = mapped_column(JSONB)
    block_notes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    version: Mapped[int]
    updated_by: Mapped[str] = mapped_column(Text)


class TransparencyReport(TenantMixin, Base):
    __tablename__ = "transparency_reports"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("status", TransparencyStatus), name="status"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month"),
        Index(
            "uq_transparency_reports_period",
            "site_id",
            "year",
            "month",
            unique=True,
            postgresql_where=text("status <> 'rejected'"),
        ),
    )

    year: Mapped[int]
    month: Mapped[int]
    status: Mapped[str] = mapped_column(Text, default=TransparencyStatus.DRAFT.value)
    scope: Mapped[dict[str, bool]] = mapped_column(JSONB)  # YK'nın belirlediği kapsam
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)  # donmuş özet; kişi/bölüm yok
    note: Mapped[str | None] = mapped_column(Text)
    prepared_by: Mapped[str] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
