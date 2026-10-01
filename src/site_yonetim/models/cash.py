"""Kasa/banka, gider, dosya — docs/03 §7–§8. Hepsi KİRACI tablosu.

Değişmezlik (docs/04 §8), göçteki tetikleyicilerle veritabanında:
- `cash_movements`: UPDATE/DELETE yok.
- `expenses`: DELETE yok; yalnız `paid_on`/`cash_account_id` (bir kez, boşken) ve
  `is_reversed` (false → true) değişebilir. Tutar, tarih, açıklama değişmez.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.cash import CashAccountKind, CashSource
from site_yonetim.models.finance import MONEY


class CashAccount(TenantMixin, Base):
    """Kasa / banka hesabı — "para nerede". Kapalı hesaba hareket yazılamaz."""

    __tablename__ = "cash_accounts"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "name"),
        CheckConstraint(enum_check("kind", CashAccountKind), name="kind"),
        CheckConstraint("char_length(name) BETWEEN 2 AND 60", name="name_length"),
    )

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    bank_name: Mapped[str | None] = mapped_column(Text)
    iban: Mapped[str | None] = mapped_column(Text)
    opening_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    opening_date: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(default=0)
    note: Mapped[str | None] = mapped_column(Text)


class CashMovement(TenantMixin, Base):
    """Kasa/banka hareketi — **değişmez**. Bakiye = Σgiren − Σçıkan."""

    __tablename__ = "cash_movements"
    __table_args__ = tenant_table_args(
        tenant_fk("cash_account_id", "cash_accounts"),
        ForeignKeyConstraint(
            ["site_id", "reversal_of_id"], ["cash_movements.site_id", "cash_movements.id"]
        ),
        CheckConstraint(
            "inflow >= 0 AND outflow >= 0 AND (inflow > 0) <> (outflow > 0)", name="one_side"
        ),
        CheckConstraint(enum_check("source", CashSource), name="source"),
        Index("ix_cash_movements_site_account_date", "site_id", "cash_account_id", "date"),
        Index("ix_cash_movements_source", "site_id", "source_id"),
        Index(
            "uq_cash_movements_reversal_of",
            "reversal_of_id",
            unique=True,
            postgresql_where="reversal_of_id IS NOT NULL",
        ),
    )

    cash_account_id: Mapped[uuid.UUID]
    date: Mapped[dt.date] = mapped_column(Date)
    inflow: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    outflow: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    description: Mapped[str] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None]  # tahsilat/gider; aktarımda karşı hareket
    reversal_of_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str | None] = mapped_column(Text)


class CashBalance(TenantMixin, Base):
    """Kasa hesabı başına özet bakiye (docs/08 §2) — hareketle aynı transaction'da güncellenir."""

    __tablename__ = "cash_balances"
    __table_args__ = tenant_table_args(
        tenant_fk("cash_account_id", "cash_accounts"),
        UniqueConstraint("cash_account_id"),
    )

    cash_account_id: Mapped[uuid.UUID]
    inflow_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    outflow_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))


class StoredFile(TenantMixin, Base):
    """Yüklenen belge. Disk adı sunucu üretir: `{site_id}/{file_id}.{uzantı}`."""

    __tablename__ = "stored_files"
    __table_args__ = tenant_table_args(
        CheckConstraint(
            "content_type IN ('application/pdf', 'image/jpeg', 'image/png', 'image/webp')",
            name="content_type",
        ),
        CheckConstraint("byte_size > 0 AND byte_size <= 10485760", name="byte_size"),
        CheckConstraint("storage_path NOT LIKE '%..%'", name="storage_path"),
    )

    file_name: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(Text)
    uploaded_by_name: Mapped[str | None] = mapped_column(Text)


class Expense(TenantMixin, Base):
    """Gider. Ödenmemiş gider kasayı etkilemez (`paid_on IS NULL`)."""

    __tablename__ = "expenses"
    __table_args__ = tenant_table_args(
        tenant_fk("expense_category_id", "expense_categories"),
        tenant_fk("period_id", "periods"),
        tenant_fk("stored_file_id", "stored_files"),
        tenant_fk("cash_account_id", "cash_accounts"),
        ForeignKeyConstraint(["site_id", "reversal_of_id"], ["expenses.site_id", "expenses.id"]),
        CheckConstraint("amount <> 0", name="amount_nonzero"),
        CheckConstraint("(reversal_of_id IS NULL) = (amount > 0)", name="reversal_negative"),
        CheckConstraint("(paid_on IS NULL) = (cash_account_id IS NULL)", name="paid_with_account"),
        CheckConstraint("char_length(description) BETWEEN 3 AND 200", name="description_length"),
        Index("ix_expenses_site_date", "site_id", "date"),
        Index(
            "uq_expenses_reversal_of",
            "reversal_of_id",
            unique=True,
            postgresql_where="reversal_of_id IS NOT NULL",
        ),
    )

    expense_category_id: Mapped[uuid.UUID]
    period_id: Mapped[uuid.UUID | None]
    description: Mapped[str] = mapped_column(Text)  # sakinler bu metni görür
    amount: Mapped[Decimal] = mapped_column(MONEY)  # ters kayıtta negatif
    date: Mapped[dt.date] = mapped_column(Date)  # belge tarihi
    vendor: Mapped[str | None] = mapped_column(Text)
    document_number: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    stored_file_id: Mapped[uuid.UUID | None]
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    cash_account_id: Mapped[uuid.UUID | None]
    reversal_of_id: Mapped[uuid.UUID | None]
    is_reversed: Mapped[bool] = mapped_column(default=False)
    created_by_name: Mapped[str | None] = mapped_column(Text)
