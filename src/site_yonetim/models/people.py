"""Kişiler, bölüm–kişi ilişkisi ve cari hesaplar — docs/03-veri-modeli.md §4–§5."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
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
from sqlalchemy.orm import Mapped, mapped_column, validates

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.custom_fields import CustomEntity, CustomFieldType, KvkkCategory
from site_yonetim.domain.invitations import HouseholdStatus, InvitationRelation, InvitationStatus
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.domain.text import tr_lower
from site_yonetim.domain.vehicles import VehicleKind, VehicleStatus


class Person(TenantMixin, Base):
    """[K] Kişi. Aynı gerçek kişi iki sitede iki ayrı kayıttır (kiracı sınırı).

    Kişisel veri (telefon, e-posta) yalnız `people.read` izniyle gösterilir ve loga yazılmaz.
    TC kimlik (şifreli) henüz tutulmuyor — docs/09 §5 anahtar yönetimiyle gelecek.
    """

    __tablename__ = "persons"
    __table_args__ = tenant_table_args(
        tenant_fk("merged_into_id", "persons"),
        CheckConstraint("char_length(first_name) BETWEEN 2 AND 40", name="first_name_length"),
        CheckConstraint("char_length(last_name) BETWEEN 2 AND 40", name="last_name_length"),
        CheckConstraint("phone IS NULL OR phone ~ '^\\+90[5][0-9]{9}$'", name="phone_e164"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
    )

    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # Arama anahtarı: Türkçe kurallarla küçük harf "ad soyad" (PostgreSQL lower() Türkçeyi bilmez)
    search_name: Mapped[str] = mapped_column(Text, index=True)
    # Birleştirme (YAP-04, geri alınamaz): bu kayıt `merged_into_id` kişiye taşındı; listelerde
    # görünmez, satır silinmez (geçmiş ve denetim izi kalır).
    merged_into_id: Mapped[uuid.UUID | None]
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @validates("first_name", "last_name")
    def _refresh_search(self, key: str, value: str) -> str:
        first = value if key == "first_name" else (self.first_name or "")
        last = value if key == "last_name" else (self.last_name or "")
        self.search_name = tr_lower(f"{first} {last}".strip())
        return value


class UnitParty(TenantMixin, Base):
    """[K] Bölüm ↔ kişi, **tarih aralıklı**. Silinmez: ilişki `end_date` ile biter."""

    __tablename__ = "unit_parties"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        CheckConstraint(enum_check("role", PartyRole), name="role"),
        CheckConstraint("share_percent > 0 AND share_percent <= 100", name="share_percent"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="date_range"),
    )

    unit_id: Mapped[uuid.UUID] = mapped_column(index=True)
    person_id: Mapped[uuid.UUID] = mapped_column(index=True)
    role: Mapped[str] = mapped_column(Text)
    share_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(100))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)


class LedgerAccount(TenantMixin, Base):
    """[K] Cari hesap — kim borçlu. Hareketler (ledger_entries) finans diliminde."""

    __tablename__ = "ledger_accounts"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        UniqueConstraint("site_id", "reference_code"),
        UniqueConstraint("unit_id", "person_id", "kind"),
        CheckConstraint(enum_check("kind", AccountKind), name="kind"),
    )

    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    reference_code: Mapped[str] = mapped_column(Text)
    is_closed: Mapped[bool] = mapped_column(default=False)


class HouseholdMember(TenantMixin, Base):
    """[K] Hane üyesi (YAP-04 S4): bölümde oturan, finans görmeyen kişi. Davetle gelir; kabul
    edince `active` olur (kişi + oturan ilişkisi + sakin hesabı). Silinmez, `ended` kalır."""

    __tablename__ = "household_members"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        tenant_fk("invitation_id", "invitations"),
        CheckConstraint(enum_check("status", HouseholdStatus), name="status"),
        CheckConstraint("phone ~ '^\\+90[5][0-9]{9}$'", name="phone_e164"),
        Index("ix_household_members_unit", "site_id", "unit_id"),
        Index(
            "uq_household_members_unit_phone",
            "site_id",
            "unit_id",
            "phone",
            unique=True,
            postgresql_where=text("status <> 'ended'"),
        ),
    )

    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID | None]
    invitation_id: Mapped[uuid.UUID | None]
    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    can_visitors: Mapped[bool] = mapped_column(default=True)
    can_packages: Mapped[bool] = mapped_column(default=True)
    can_reservations: Mapped[bool] = mapped_column(default=False)
    can_requests: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default=HouseholdStatus.INVITED.value)
    invited_by_name: Mapped[str | None] = mapped_column(Text)
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Vehicle(TenantMixin, Base):
    """[K] Araç (YAP-05). Plaka normalize saklanır (`34ABC123`). Silinmez: `inactive` geçmişte
    kalır ve kapı ekranında görünmez. Aynı bölümde aynı plaka bir kez etkin olabilir."""

    __tablename__ = "vehicles"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("owner_person_id", "persons"),
        CheckConstraint(enum_check("kind", VehicleKind), name="kind"),
        CheckConstraint(enum_check("status", VehicleStatus), name="status"),
        CheckConstraint("kind <> 'guest' OR valid_until IS NOT NULL", name="guest_valid_until"),
        Index("ix_vehicles_site_plate", "site_id", "plate"),
        Index("ix_vehicles_site_unit", "site_id", "unit_id"),
        Index(
            "uq_vehicles_unit_plate_open",
            "site_id",
            "unit_id",
            "plate",
            unique=True,
            postgresql_where=text("status <> 'inactive'"),
        ),
    )

    unit_id: Mapped[uuid.UUID]
    plate: Mapped[str] = mapped_column(Text)
    brand_model: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default=VehicleKind.PERMANENT.value)
    valid_until: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default=VehicleStatus.ACTIVE.value)
    owner_person_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str | None] = mapped_column(Text)
    import_batch_id: Mapped[uuid.UUID | None]


class Invitation(TenantMixin, Base):
    """[K] Davet (YAP-07): telefona bağlı, tek kullanımlık bağlantı. Belirtecin yalnız özeti
    saklanır. Hane üyesi daveti de aynı kabul akışından geçer (`relation=household`)."""

    __tablename__ = "invitations"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        UniqueConstraint("token_hash"),
        CheckConstraint(enum_check("relation", InvitationRelation), name="relation"),
        CheckConstraint(enum_check("status", InvitationStatus), name="status"),
        CheckConstraint("phone ~ '^\\+90[5][0-9]{9}$'", name="phone_e164"),
        Index("ix_invitations_site_phone", "site_id", "phone"),
        Index("ix_invitations_site_sent", "site_id", "sent_at"),
    )

    full_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    unit_id: Mapped[uuid.UUID]
    relation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=InvitationStatus.SENT.value)
    token_hash: Mapped[str] = mapped_column(Text)
    sent_by_name: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    person_id: Mapped[uuid.UUID | None]


class CustomField(TenantMixin, Base):
    """[K] Özel alan tanımı (YAP-10). Silinmez, arşivlenir; anahtar ve tip değişmez."""

    __tablename__ = "custom_fields"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "entity", "key"),
        CheckConstraint(enum_check("entity", CustomEntity), name="entity"),
        CheckConstraint(enum_check("type", CustomFieldType), name="type"),
        CheckConstraint(enum_check("kvkk_category", KvkkCategory), name="kvkk_category"),
        CheckConstraint("char_length(label) BETWEEN 1 AND 60", name="label_length"),
    )

    entity: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    required: Mapped[bool] = mapped_column(default=False)
    kvkk_category: Mapped[str] = mapped_column(Text)
    view_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    edit_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    resident_can_view: Mapped[bool] = mapped_column(default=False)
    resident_can_edit: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)
    is_archived: Mapped[bool] = mapped_column(default=False)
    # Özel nitelikli veri uyarısı gerekçeyle geçildiyse gerekçe (denetim kaydına da yazılır)
    sensitive_reason: Mapped[str | None] = mapped_column(Text)


class CustomFieldValue(TenantMixin, Base):
    """[K] Özel alan değeri: bölüm ya da kişi başına alan başına bir satır (JSON değer)."""

    __tablename__ = "custom_field_values"
    __table_args__ = tenant_table_args(
        tenant_fk("field_id", "custom_fields"),
        UniqueConstraint("site_id", "field_id", "entity_id"),
        Index("ix_custom_field_values_entity", "site_id", "entity_id"),
    )

    field_id: Mapped[uuid.UUID]
    entity_id: Mapped[uuid.UUID]
    value: Mapped[Any] = mapped_column(JSONB)
