"""Denetim kurulu paketi — Yazılım Dokümanı RAP-05 (servis isteği 81). KİRACI tablosu.

Paket hazırlandığı anda dönemin finansal özeti (gelir, gider, kategori dökümü, bütçe–gerçekleşme,
kasa/banka), karar defteri kayıtları ve seçilen belgeler **dondurulur** (`snapshot`, `decisions`):
sonradan girilen ters kayıt ya da karar paketi değiştirmez. Denetçi raporunu ekleyince paket
"denetlendi" olur ve arşivlenir (ikinci rapor eklenmez).
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ARRAY, CheckConstraint, Date, DateTime, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, tenant_fk, tenant_table_args


class AuditPackage(TenantMixin, Base):
    __tablename__ = "audit_packages"
    __table_args__ = tenant_table_args(
        tenant_fk("report_document_id", "documents"),
        CheckConstraint("status IN ('open', 'reviewed')", name="status"),
        CheckConstraint("period_to >= period_from", name="period"),
        CheckConstraint(
            "(status = 'reviewed') = (report_document_id IS NOT NULL)", name="reviewed_report"
        ),
    )

    title: Mapped[str] = mapped_column(Text)
    period_from: Mapped[dt.date] = mapped_column(Date)
    period_to: Mapped[dt.date] = mapped_column(Date)
    document_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    decisions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="open")
    created_by_name: Mapped[str] = mapped_column(Text)
    report_document_id: Mapped[uuid.UUID | None]
    reviewed_by_name: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
