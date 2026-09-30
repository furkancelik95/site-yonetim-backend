"""Kişiler, bölüm–kişi ilişkisi ve cari hesaplar — docs/03-veri-modeli.md §4–§5."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, validates

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.structure import AccountKind, PartyRole
from site_yonetim.domain.text import tr_lower


class Person(TenantMixin, Base):
    """[K] Kişi. Aynı gerçek kişi iki sitede iki ayrı kayıttır (kiracı sınırı).

    Kişisel veri (telefon, e-posta) yalnız `people.read` izniyle gösterilir ve loga yazılmaz.
    TC kimlik (şifreli) henüz tutulmuyor — docs/09 §5 anahtar yönetimiyle gelecek.
    """

    __tablename__ = "persons"
    __table_args__ = tenant_table_args(
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
