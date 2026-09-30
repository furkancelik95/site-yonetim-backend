"""Yapı: blok, daire tipi, bağımsız bölüm — docs/03-veri-modeli.md §3. Hepsi KİRACI tablosu."""

import uuid
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import CheckConstraint, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args


class UnitUsage(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    STORAGE = "storage"
    PARKING = "parking"


class Block(TenantMixin, Base):
    __tablename__ = "blocks"
    __table_args__ = tenant_table_args()

    name: Mapped[str] = mapped_column(Text)  # "A", "Kule 1"; tek bloklu sitede boş olabilir
    has_elevator: Mapped[bool] = mapped_column(default=False)
    floor_count: Mapped[int | None]
    sort_order: Mapped[int] = mapped_column(default=0)


class UnitType(TenantMixin, Base):
    __tablename__ = "unit_types"
    __table_args__ = tenant_table_args()

    name: Mapped[str] = mapped_column(Text)  # "1+1", "2+1"
    weight: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(1))
    sort_order: Mapped[int] = mapped_column(default=0)


class Unit(TenantMixin, Base):
    """Bağımsız bölüm: daire, dükkan, depo, otopark."""

    __tablename__ = "units"
    __table_args__ = tenant_table_args(
        tenant_fk("block_id", "blocks"),
        tenant_fk("unit_type_id", "unit_types"),
        UniqueConstraint("site_id", "block_id", "number"),
        CheckConstraint(enum_check("usage", UnitUsage), name="usage"),
        CheckConstraint("floor BETWEEN -5 AND 100", name="floor_range"),
        CheckConstraint(
            "(land_share_numerator IS NULL) = (land_share_denominator IS NULL)",
            name="land_share_pair",
        ),
        CheckConstraint(
            "land_share_denominator IS NULL OR land_share_denominator > 0",
            name="land_share_denominator",
        ),
    )

    block_id: Mapped[uuid.UUID]  # (site_id, block_id, number) benzersizliği indeks de sağlar
    number: Mapped[str] = mapped_column(Text)  # "12", "A-12", "Z03"
    floor: Mapped[int | None]
    unit_type_id: Mapped[uuid.UUID | None]
    gross_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    net_area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    land_share_numerator: Mapped[int | None]
    land_share_denominator: Mapped[int | None]
    usage: Mapped[str] = mapped_column(Text, default=UnitUsage.RESIDENTIAL.value)
    is_active: Mapped[bool] = mapped_column(default=True)  # pasif bölüme tahakkuk kesilmez
    commercial_title: Mapped[str | None] = mapped_column(Text)

    # İlişki yalnız block_id'yi eşler; site_id kiracı kapsamından damgalanır.
    block: Mapped[Block] = relationship(
        primaryjoin="Unit.block_id == Block.id", foreign_keys="Unit.block_id", lazy="raise"
    )
    unit_type: Mapped[UnitType | None] = relationship(
        primaryjoin="Unit.unit_type_id == UnitType.id",
        foreign_keys="Unit.unit_type_id",
        lazy="raise",
    )

    @property
    def land_share(self) -> Decimal | None:
        if self.land_share_numerator is None or not self.land_share_denominator:
            return None
        return Decimal(self.land_share_numerator) / Decimal(self.land_share_denominator)

    def display_name(self, block_name: str | None) -> str:
        """`A-12`; blok adı boşsa yalnız numara."""
        return f"{block_name}-{self.number}" if block_name else self.number
