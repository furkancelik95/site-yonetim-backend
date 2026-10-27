"""Otopark — park yerleri ve tarihçeli tahsis (PM kararı K30; Yazılım Dokümanı OTP-01).
KİRACI tabloları. Tahsis silinmez; bitirilir."""

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, Date, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.parking import ParkingKind


class ParkingSpace(TenantMixin, Base):
    __tablename__ = "parking_spaces"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "code"),
        CheckConstraint(enum_check("kind", ParkingKind), name="kind"),
    )

    code: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default=ParkingKind.STANDARD.value)
    location: Mapped[str | None] = mapped_column(Text)
    is_paid: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(Text)


class ParkingAllocation(TenantMixin, Base):
    __tablename__ = "parking_allocations"
    __table_args__ = tenant_table_args(
        tenant_fk("space_id", "parking_spaces"),
        tenant_fk("unit_id", "units"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
        Index("ix_parking_allocations_site_space", "site_id", "space_id", "start_date"),
        Index("ix_parking_allocations_site_unit", "site_id", "unit_id"),
    )

    space_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    ended_by: Mapped[str | None] = mapped_column(Text)
