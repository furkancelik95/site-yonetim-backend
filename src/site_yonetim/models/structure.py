"""Yapı: blok, daire tipi, bağımsız bölüm — docs/03-veri-modeli.md §3. Hepsi KİRACI tablosu."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.invitations import INVITE_DAYS_DEFAULT
from site_yonetim.domain.structure import CommonAreaKind, UnitUsage
from site_yonetim.domain.vehicles import PLATE_LIMIT_DEFAULT


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


class CommonArea(TenantMixin, Base):
    """Ortak alan (havuz, salon, otopark…) — YAP-02. `block_id` boşsa tüm siteye ait.
    Rezerve edilebilir alanlar rezervasyon modülüne bağlanır (REZ-01). Silinmez, pasifleşir."""

    __tablename__ = "common_areas"
    __table_args__ = tenant_table_args(
        tenant_fk("block_id", "blocks"),
        CheckConstraint(enum_check("kind", CommonAreaKind), name="kind"),
        CheckConstraint("char_length(name) BETWEEN 2 AND 80", name="name_length"),
    )

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    block_id: Mapped[uuid.UUID | None]
    is_reservable: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    note: Mapped[str | None] = mapped_column(Text)


class UnitAttributeChange(TenantMixin, Base):
    """Bölümün yürürlük tarihli öznitelik değişikliği (m², arsa payı, tip — YAP-02 S3).

    `changes`: `{"gross_area": {"from": "140.00", "to": "142.50"}}` (tip için kimlik). İleri
    tarihli değişiklik `applied=false` bekler; tarihi gelince zamanlanmış iş bölüme uygular.
    Satır silinmez; uygulandıktan sonra değişmez (tetikleyici: yalnız `applied` işaretlenir).
    """

    __tablename__ = "unit_attribute_changes"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        Index("ix_unit_attribute_changes_unit", "site_id", "unit_id", "effective_from"),
        Index(
            "ix_unit_attribute_changes_pending",
            "site_id",
            "effective_from",
            postgresql_where=text("NOT applied"),
        ),
    )

    unit_id: Mapped[uuid.UUID]
    effective_from: Mapped[date] = mapped_column(Date)
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    changed_by_name: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    applied: Mapped[bool] = mapped_column(default=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StructureSettings(TenantMixin, Base):
    """Site başına bir satır: araç ayarı (YAP-05), kayıt yöntemleri (YAP-07), açılış bakiyesi
    mutabakat işareti (YAP-01). Satır yoksa varsayılanlar geçerlidir."""

    __tablename__ = "structure_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("plate_limit_per_unit BETWEEN 1 AND 10", name="plate_limit"),
        CheckConstraint("invite_valid_days BETWEEN 1 AND 30", name="invite_days"),
    )

    plate_limit_per_unit: Mapped[int] = mapped_column(default=PLATE_LIMIT_DEFAULT)
    vehicle_approval_required: Mapped[bool] = mapped_column(default=False)
    invite_enabled: Mapped[bool] = mapped_column(default=True)
    qr_enabled: Mapped[bool] = mapped_column(default=True)
    code_enabled: Mapped[bool] = mapped_column(default=True)
    # Ortak kararı bekleyen davranışlar — şimdilik yalnız saklanır (istek 23).
    owner_can_approve_tenant: Mapped[bool] = mapped_column(default=False)
    owner_document_required: Mapped[bool] = mapped_column(default=False)
    invite_valid_days: Mapped[int] = mapped_column(default=INVITE_DAYS_DEFAULT)
    kvkk_published: Mapped[bool] = mapped_column(default=False)
    opening_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opening_reconciled_by: Mapped[str | None] = mapped_column(Text)
    opening_reconciled_note: Mapped[str | None] = mapped_column(Text)
    # Sakin olarak görüntüle (PLT-12): oturum açılınca sakine bildirim (varsayılan: bildir)
    view_as_notify_resident: Mapped[bool] = mapped_column(default=True, server_default="true")


class ImportBatch(TenantMixin, Base):
    """Toplu aktarım partisi (YAP-06): önizleme → aktarıldı → (geri alındı).

    Araç ve açılış bakiyesi aktarımında önizleme anında satır açılır (`previewed`, 6 saat);
    bölüm aktarımında onayda yazılır. `created`: geri alma için oluşturulan kayıtların
    kimlikleri (`{"vehicles": […]}`, `{"entries": […]}`, `{"units": […], "persons": […], …}`).
    """

    __tablename__ = "import_batches"
    __table_args__ = tenant_table_args(
        CheckConstraint("type IN ('units', 'vehicles', 'opening_balances')", name="type"),
        CheckConstraint("status IN ('previewed', 'imported', 'rolled_back')", name="status"),
        Index("ix_import_batches_site_created", "site_id", "created_at"),
    )

    type: Mapped[str] = mapped_column(Text)
    file_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="previewed")
    row_count: Mapped[int] = mapped_column(default=0)
    created_count: Mapped[int] = mapped_column(default=0)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    created: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rollback_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
