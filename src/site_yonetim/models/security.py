"""Kargo ve ziyaretçi — docs/03 §9. KİRACI tablosu.

Ziyaretçi adı, telefonu ve plakası kişisel veridir: yalnız güvenlik izniyle görünür, loga
yazılmaz. Saklama süresi açık karar (docs/12 K8).
"""

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.security import PackageStatus, VisitorKind, VisitorStatus


class Package(TenantMixin, Base):
    __tablename__ = "packages"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        CheckConstraint(enum_check("status", PackageStatus), name="status"),
        CheckConstraint("pickup_code ~ '^[1-9][0-9]{3}$'", name="pickup_code"),
        Index("ix_packages_site_status", "site_id", "status"),
        Index("ix_packages_site_unit", "site_id", "unit_id"),
    )

    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID | None]  # alıcı
    carrier: Mapped[str | None] = mapped_column(Text)
    pickup_code: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=PackageStatus.WAITING.value)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_by: Mapped[str | None] = mapped_column(Text)
    delivered_to: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    notification_sent: Mapped[bool] = mapped_column(default=False)  # K5: gönderim yok
    notification_channel: Mapped[str | None] = mapped_column(Text)


class Visitor(TenantMixin, Base):
    __tablename__ = "visitors"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("host_person_id", "persons"),
        CheckConstraint(enum_check("kind", VisitorKind), name="kind"),
        CheckConstraint(enum_check("status", VisitorStatus), name="status"),
        CheckConstraint("char_length(full_name) BETWEEN 2 AND 80", name="full_name_length"),
        Index("ix_visitors_site_day", "site_id", "visit_date"),
    )

    unit_id: Mapped[uuid.UUID]
    host_person_id: Mapped[uuid.UUID | None]
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    plate_number: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default=VisitorKind.GUEST.value)
    qr_token: Mapped[str | None] = mapped_column(Text)  # ön davet — henüz kullanılmıyor
    expected_on: Mapped[dt.date | None] = mapped_column(Date)
    # Listeleme günü: beklenen gün, yoksa kaydın günü (Türkiye saati) — sorgu indeksli olsun.
    visit_date: Mapped[dt.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default=VisitorStatus.EXPECTED.value)
    entered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    exited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
