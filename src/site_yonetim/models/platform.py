"""Platform ve kiracı tabloları — docs/03-veri-modeli.md §1. Hepsi GLOBAL (site_id yok)."""

import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, enum_check


class PropertyKind(StrEnum):
    RESIDENTIAL = "residential"
    MIXED = "mixed"
    OFFICE = "office"
    SHOPPING_CENTER = "shopping_center"


class Plan(Base):
    __tablename__ = "plans"

    name: Mapped[str] = mapped_column(Text, unique=True)
    max_units: Mapped[int | None]  # NULL = sınırsız
    max_storage_mb: Mapped[int | None]
    allowed_modules: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    sort_order: Mapped[int] = mapped_column(default=0)


class Organization(Base):
    """Yönetim şirketi."""

    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint("char_length(name) BETWEEN 3 AND 150", name="name_length"),)

    name: Mapped[str] = mapped_column(Text)
    tax_number: Mapped[str | None] = mapped_column(Text)  # VKN
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))


class Site(Base):
    """Kiracı (tenant) — izolasyon sınırı."""

    __tablename__ = "sites"
    __table_args__ = (
        CheckConstraint(enum_check("property_kind", PropertyKind), name="property_kind"),
        CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="fiscal_month"),
        CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="slug_format"),
    )

    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(String(60), unique=True)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    district: Mapped[str | None] = mapped_column(Text)
    property_kind: Mapped[str] = mapped_column(Text, default=PropertyKind.RESIDENTIAL.value)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("plans.id"))
    fiscal_year_start_month: Mapped[int] = mapped_column(default=1)
    iban: Mapped[str | None] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(Text)
