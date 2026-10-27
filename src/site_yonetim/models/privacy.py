"""KVKK — Yazılım Dokümanı RAP-04 (servis isteği 80; PM kararları P6, K8). KİRACI tabloları.

- `privacy_notices`: aydınlatma metni sürümleri (site başına artan `version`). **Değişmez.**
- `privacy_consents`: kullanıcı × sürüm onay kaydı (P6: ilk girişte ve metin değişince).
  Kayıt başvurusu/davet kabulündeki onay da buraya bağlanır (`source`). **Değişmez.**
- `explicit_consents`: açık rıza (aydınlatmadan ayrı, isteğe bağlı); her verme/geri çekme yeni
  satır, amaç başına en sonuncusu geçerli. **Değişmez.**
- `privacy_requests`: ilgili kişi başvurusu ve 30 gün sayacı; aynı kişinin aynı türde tek açık
  başvurusu (kısmi benzersiz indeks).
- `retention_policies`: kategori × süre (K8); ziyaretçi kategorisinin kaynağı güvenlik ayarıdır
  (`security_settings`), burada tekrar tutulmaz.
- `retention_runs`: silme/anonimleştirme raporu (gece işi ve elle uygulama). **Değişmez.**
"""

import datetime as dt
import uuid

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.privacy import (
    ConsentSource,
    ExplicitPurpose,
    PrivacyOutcome,
    PrivacyRequestKind,
    PrivacyRequestStatus,
)
from site_yonetim.domain.retention import RetentionAction, RunTrigger


class PrivacyNotice(TenantMixin, Base):
    __tablename__ = "privacy_notices"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("char_length(text) >= 200", name="text_length"),
    )

    version: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    published_by_name: Mapped[str] = mapped_column(Text)


class PrivacyConsent(TenantMixin, Base):
    __tablename__ = "privacy_consents"
    __table_args__ = tenant_table_args(
        tenant_fk("notice_id", "privacy_notices"),
        UniqueConstraint("site_id", "user_id", "notice_id"),
        CheckConstraint(enum_check("source", ConsentSource), name="source"),
        Index("ix_privacy_consents_user", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    notice_id: Mapped[uuid.UUID]
    version: Mapped[int]
    source: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


class ExplicitConsent(TenantMixin, Base):
    __tablename__ = "explicit_consents"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("purpose", ExplicitPurpose), name="purpose"),
        CheckConstraint(enum_check("source", ConsentSource), name="source"),
        Index("ix_explicit_consents_user", "site_id", "user_id", "purpose", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(Text)
    granted: Mapped[bool]
    source: Mapped[str] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)


class PrivacyRequest(TenantMixin, Base):
    __tablename__ = "privacy_requests"
    __table_args__ = tenant_table_args(
        tenant_fk("person_id", "persons"),
        CheckConstraint(enum_check("kind", PrivacyRequestKind), name="kind"),
        CheckConstraint(enum_check("status", PrivacyRequestStatus), name="status"),
        CheckConstraint(
            f"outcome IS NULL OR {enum_check('outcome', PrivacyOutcome)}", name="outcome"
        ),
        CheckConstraint(
            "(status = 'open') = (answered_at IS NULL AND outcome IS NULL)", name="answered"
        ),
        Index(
            "uq_privacy_requests_open_kind",
            "site_id",
            "user_id",
            "kind",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_privacy_requests_site_status_due", "site_id", "status", "due_on"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    person_id: Mapped[uuid.UUID | None]
    requester_name: Mapped[str] = mapped_column(Text)
    unit_names: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    kind: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default=PrivacyRequestStatus.OPEN.value)
    due_on: Mapped[dt.date] = mapped_column(Date)
    response: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    retained: Mapped[str | None] = mapped_column(Text)
    answered_by_name: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    warned_on: Mapped[dt.date | None] = mapped_column(Date)  # son uyarı günü (günde bir)


class RetentionPolicy(TenantMixin, Base):
    __tablename__ = "retention_policies"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "category"),
        CheckConstraint("value >= 1", name="value_positive"),
    )

    category: Mapped[str] = mapped_column(Text)
    value: Mapped[int]
    auto_apply: Mapped[bool] = mapped_column(default=False)
    updated_by: Mapped[str | None] = mapped_column(Text)


class RetentionRun(TenantMixin, Base):
    __tablename__ = "retention_runs"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("action", RetentionAction), name="action"),
        CheckConstraint(enum_check("trigger", RunTrigger), name="trigger"),
        CheckConstraint("affected >= 0", name="affected"),
        Index("ix_retention_runs_site_created", "site_id", "created_at"),
    )

    category: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text)
    cutoff: Mapped[dt.date] = mapped_column(Date)
    affected: Mapped[int]
    actor_name: Mapped[str] = mapped_column(Text)
