"""Belge arşivi — Yazılım Dokümanı RAP-03 (servis isteği 78). KİRACI tabloları.

- `documents`: belge kartı (ad, tür, kapsam, gizlilik, geçerlilik, saklama süresi). Aynı ad +
  aynı kapsam tek kart: `title_key` (Türkçe küçük harf, boşluklar tekleştirilmiş) benzersiz.
  Silinmez (K8: otomatik silme yok).
- `document_versions`: her yükleme bir sürüm; dosya `stored_files`'ta (içerik imzası, SHA-256,
  web kökü dışı). **Değişmez** (forbid_history_change).
- `document_access_logs`: gizli belgenin her açılışı. **Değişmez.**
- `subject_type/subject_id`: diğer modüllerin belgeyi bir kayda bağlaması için (ör. sözleşme,
  poliçe, tutanak). İsteğe bağlı; bugün yalnız şema.
"""

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.documents import Confidentiality, DocumentKind, DocumentScope


class Document(TenantMixin, Base):
    __tablename__ = "documents"
    __table_args__ = tenant_table_args(
        tenant_fk("block_id", "blocks"),
        tenant_fk("unit_id", "units"),
        CheckConstraint(enum_check("kind", DocumentKind), name="kind"),
        CheckConstraint(enum_check("scope", DocumentScope), name="scope"),
        CheckConstraint(enum_check("confidentiality", Confidentiality), name="confidentiality"),
        CheckConstraint(
            "(scope = 'site' AND block_id IS NULL AND unit_id IS NULL)"
            " OR (scope = 'block' AND block_id IS NOT NULL AND unit_id IS NULL)"
            " OR (scope = 'unit' AND unit_id IS NOT NULL AND block_id IS NULL)",
            name="scope_target",
        ),
        CheckConstraint("confidentiality <> 'unit' OR scope = 'unit'", name="unit_private"),
        CheckConstraint("char_length(title) BETWEEN 3 AND 120", name="title_length"),
        CheckConstraint(
            "retention_years IS NULL OR retention_years BETWEEN 1 AND 100", name="retention"
        ),
        CheckConstraint("(subject_type IS NULL) = (subject_id IS NULL)", name="subject_pair"),
        CheckConstraint("current_version >= 1", name="current_version"),
        UniqueConstraint(
            "site_id",
            "title_key",
            "scope",
            "block_id",
            "unit_id",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_documents_site_valid_until", "site_id", "valid_until"),
        Index("ix_documents_site_subject", "site_id", "subject_type", "subject_id"),
    )

    title: Mapped[str] = mapped_column(Text)
    title_key: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    block_id: Mapped[uuid.UUID | None]
    unit_id: Mapped[uuid.UUID | None]
    confidentiality: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[dt.date | None] = mapped_column(Date)
    retention_years: Mapped[int | None]
    subject_type: Mapped[str | None] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None]
    current_version: Mapped[int] = mapped_column(default=1)
    created_by_name: Mapped[str | None] = mapped_column(Text)


class DocumentVersion(TenantMixin, Base):
    """Bir yükleme. Değişmez; dosya bilgisi (ad, tür, boyut, SHA-256) `stored_files`'ta."""

    __tablename__ = "document_versions"
    __table_args__ = tenant_table_args(
        tenant_fk("document_id", "documents"),
        tenant_fk("stored_file_id", "stored_files"),
        UniqueConstraint("document_id", "version"),
        UniqueConstraint("stored_file_id"),
        CheckConstraint("version >= 1", name="version"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(index=True)
    version: Mapped[int]
    stored_file_id: Mapped[uuid.UUID]
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    uploaded_by_name: Mapped[str] = mapped_column(Text)


class DocumentAccessLog(TenantMixin, Base):
    """Gizli belgenin her açılışı (kim, ne zaman, hangi sürüm). Değişmez."""

    __tablename__ = "document_access_logs"
    __table_args__ = tenant_table_args(
        tenant_fk("document_id", "documents"),
        Index("ix_document_access_logs_document_created", "document_id", "created_at"),
    )

    document_id: Mapped[uuid.UUID]
    version: Mapped[int]
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    user_name: Mapped[str] = mapped_column(Text)
