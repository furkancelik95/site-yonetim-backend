"""Satın alma — Yazılım Dokümanı §15 (SAT-01..06), servis istekleri 62–67. Hepsi KİRACI tablosu.

- `vendors`: tedarikçi kartı. Vergi no site içinde tekil; IBAN okumada maskeli döner, denetim
  kaydına maskeli yazılır (`tax_number`, `iban`, `phone`, `email` sütun adları maskelenir).
- `purchase_requests` → `purchase_request_items`, `purchase_quotes` (→ `purchase_quote_lines`),
  `purchase_quote_links` (tedarikçi linki: yalnız belirtecin SHA-256 özeti saklanır),
  `purchase_decisions` (**değişmez**: istisna/satın alma/sipariş kararları).
- `purchase_orders` → `purchase_order_lines` (teslim alınan miktar kabulle artar),
  `purchase_receipts` → `purchase_receipt_lines` (**değişmez**).
- `vendor_invoices` → `vendor_invoice_lines` (**değişmez**): üçlü eşleşme sonucu faturada
  saklanır; "ödendi" aynı işlemde gider + kasa çıkışı yazar (`expense_id`).
- Belgeler (`stored_file_id`): teklif PDF'i, kabul fotoğrafı/irsaliye, fatura görüntüsü —
  `stored_files` (içerik imzası, web kökü dışı), indirme `GET …/files/{id}` (`expenses.read`).
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.purchasing import (
    DecisionKind,
    DecisionStage,
    InvoiceStatus,
    OrderStatus,
    PurchaseStatus,
    QuoteSource,
    VendorStatus,
)
from site_yonetim.models.finance import MONEY
from site_yonetim.models.management import QUANTITY

PERCENT = Numeric(5, 2)


class Vendor(TenantMixin, Base):
    __tablename__ = "vendors"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "tax_number"),
        CheckConstraint(enum_check("status", VendorStatus), name="status"),
        CheckConstraint("char_length(name) BETWEEN 2 AND 120", name="name_length"),
        Index("ix_vendors_site_name", "site_id", "name_key"),
    )

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)  # arama ve sıralama (Türkçe küçük harf)
    tax_number: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    iban: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=VendorStatus.ACTIVE.value)
    note: Mapped[str | None] = mapped_column(Text)


class PurchaseSettings(TenantMixin, Base):
    """Satın alma ayarları — site başına tek satır; yoksa varsayılan (servis isteği 63)."""

    __tablename__ = "purchase_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("above_quotes BETWEEN 1 AND 10", name="above_quotes"),
        CheckConstraint("tolerance_percent BETWEEN 0 AND 10", name="tolerance_percent"),
        CheckConstraint("link_days BETWEEN 1 AND 30", name="link_days"),
    )

    thresholds: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)  # [{max: "…", quotes: n}]
    above_quotes: Mapped[int]
    tolerance_percent: Mapped[Decimal] = mapped_column(PERCENT)
    link_days: Mapped[int]


class PurchaseRequest(TenantMixin, Base):
    __tablename__ = "purchase_requests"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "number"),
        UniqueConstraint("site_id", "number_year", "sequence"),
        tenant_fk("expense_category_id", "expense_categories"),
        tenant_fk("approval_request_id", "approval_requests"),
        ForeignKeyConstraint(
            ["site_id", "selected_quote_id"],
            ["purchase_quotes.site_id", "purchase_quotes.id"],
            use_alter=True,
            name="fk_purchase_requests_site_id_selected_quote_id_purchase_quotes",
        ),
        CheckConstraint(enum_check("status", PurchaseStatus), name="status"),
        CheckConstraint("estimated_total > 0", name="estimated_total"),
        CheckConstraint("required_quotes BETWEEN 1 AND 10", name="required_quotes"),
        Index("ix_purchase_requests_site_status", "site_id", "status"),
    )

    number_year: Mapped[int]
    sequence: Mapped[int]
    number: Mapped[str] = mapped_column(Text)  # ST-2026-0001
    title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    expense_category_id: Mapped[uuid.UUID]
    estimated_total: Mapped[Decimal] = mapped_column(MONEY)
    # Talep anındaki bütçe görüntüsü (kategoride kalem yoksa boş)
    budget_annual: Mapped[Decimal | None] = mapped_column(MONEY)
    budget_spent: Mapped[Decimal | None] = mapped_column(MONEY)
    budget_committed: Mapped[Decimal | None] = mapped_column(MONEY)
    over_budget: Mapped[bool] = mapped_column(default=False)
    required_quotes: Mapped[int]
    status: Mapped[str] = mapped_column(Text, default=PurchaseStatus.QUOTING.value)
    selected_quote_id: Mapped[uuid.UUID | None]
    exception_reason: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    # Açık onay talebi (teklif istisnası / satın alma / sipariş — onay kuralı varsa)
    approval_request_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)


class PurchaseRequestItem(TenantMixin, Base):
    __tablename__ = "purchase_request_items"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "purchase_requests"),
        tenant_fk("stock_item_id", "stock_items"),
        UniqueConstraint("request_id", "position"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("est_unit_price > 0", name="est_unit_price"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position: Mapped[int]
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit: Mapped[str] = mapped_column(Text)
    est_unit_price: Mapped[Decimal] = mapped_column(MONEY)
    stock_item_id: Mapped[uuid.UUID | None]


class PurchaseQuote(TenantMixin, Base):
    __tablename__ = "purchase_quotes"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "purchase_requests"),
        tenant_fk("vendor_id", "vendors"),
        tenant_fk("stored_file_id", "stored_files"),
        UniqueConstraint("request_id", "vendor_id"),
        CheckConstraint(enum_check("source", QuoteSource), name="source"),
        CheckConstraint("total > 0", name="total"),
        CheckConstraint("delivery_days BETWEEN 0 AND 365", name="delivery_days"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    vendor_id: Mapped[uuid.UUID]
    total: Mapped[Decimal] = mapped_column(MONEY)
    delivery_days: Mapped[int]
    payment_terms: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[dt.date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    stored_file_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)


class PurchaseQuoteLine(TenantMixin, Base):
    __tablename__ = "purchase_quote_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("quote_id", "purchase_quotes"),
        UniqueConstraint("quote_id", "position"),
        CheckConstraint("unit_price > 0", name="unit_price"),
    )

    quote_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position: Mapped[int]
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    line_total: Mapped[Decimal] = mapped_column(MONEY)


class PurchaseQuoteLink(TenantMixin, Base):
    """Tedarikçi teklif linki: tek talebe bağlı, süreli, tek kullanımlık. Belirtecin kendisi
    saklanmaz (yalnız SHA-256 özeti); sitedeki tüm linkler arasında tekil."""

    __tablename__ = "purchase_quote_links"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "purchase_requests"),
        tenant_fk("vendor_id", "vendors"),
        tenant_fk("quote_id", "purchase_quotes"),
        UniqueConstraint("token_hash"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    vendor_id: Mapped[uuid.UUID]
    token_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    quote_id: Mapped[uuid.UUID | None]
    created_by_name: Mapped[str] = mapped_column(Text)


class PurchaseDecision(TenantMixin, Base):
    """Talep üzerindeki karar (doğrudan ya da onay mekanizmasından). **Değişmez.**"""

    __tablename__ = "purchase_decisions"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "purchase_requests"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("stage", DecisionStage), name="stage"),
        CheckConstraint(enum_check("decision", DecisionKind), name="decision"),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    stage: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]


class PurchaseOrder(TenantMixin, Base):
    __tablename__ = "purchase_orders"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "number"),
        UniqueConstraint("site_id", "number_year", "sequence"),
        UniqueConstraint("site_id", "request_id"),
        tenant_fk("request_id", "purchase_requests"),
        tenant_fk("quote_id", "purchase_quotes"),
        tenant_fk("vendor_id", "vendors"),
        tenant_fk("expense_category_id", "expense_categories"),
        CheckConstraint(enum_check("status", OrderStatus), name="status"),
        CheckConstraint("total > 0", name="total"),
        Index("ix_purchase_orders_site_vendor", "site_id", "vendor_id"),
    )

    number_year: Mapped[int]
    sequence: Mapped[int]
    number: Mapped[str] = mapped_column(Text)  # SIP-2026-0001
    request_id: Mapped[uuid.UUID]
    quote_id: Mapped[uuid.UUID]
    vendor_id: Mapped[uuid.UUID]
    expense_category_id: Mapped[uuid.UUID]
    total: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(Text, default=OrderStatus.SENT.value)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)


class PurchaseOrderLine(TenantMixin, Base):
    __tablename__ = "purchase_order_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("order_id", "purchase_orders"),
        tenant_fk("stock_item_id", "stock_items"),
        UniqueConstraint("order_id", "position"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("received_quantity BETWEEN 0 AND quantity", name="received_quantity"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position: Mapped[int]
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit: Mapped[str] = mapped_column(Text)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    line_total: Mapped[Decimal] = mapped_column(MONEY)
    stock_item_id: Mapped[uuid.UUID | None]
    received_quantity: Mapped[Decimal] = mapped_column(QUANTITY, default=Decimal(0))


class PurchaseReceipt(TenantMixin, Base):
    """Mal/hizmet kabulü. Satır değişmez; yalnız sonradan eklenen belge (bir kez) yazılır."""

    __tablename__ = "purchase_receipts"
    __table_args__ = tenant_table_args(
        tenant_fk("order_id", "purchase_orders"),
        tenant_fk("stored_file_id", "stored_files"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(index=True)
    note: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[int | None]
    stored_file_id: Mapped[uuid.UUID | None]
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(Text)


class PurchaseReceiptLine(TenantMixin, Base):
    """Kabul satırı. **Değişmez.**"""

    __tablename__ = "purchase_receipt_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("receipt_id", "purchase_receipts"),
        tenant_fk("order_line_id", "purchase_order_lines"),
        CheckConstraint("quantity > 0", name="quantity"),
    )

    receipt_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order_line_id: Mapped[uuid.UUID]
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)


class VendorInvoice(TenantMixin, Base):
    __tablename__ = "vendor_invoices"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "vendor_id", "number"),
        tenant_fk("order_id", "purchase_orders"),
        tenant_fk("vendor_id", "vendors"),
        tenant_fk("stored_file_id", "stored_files"),
        tenant_fk("cash_account_id", "cash_accounts"),
        tenant_fk("expense_id", "expenses"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("status", InvoiceStatus), name="status"),
        CheckConstraint("total > 0", name="total"),
        CheckConstraint("due_date >= invoice_date", name="due_date"),
        CheckConstraint(
            "(status = 'paid') = (paid_on IS NOT NULL AND expense_id IS NOT NULL)", name="paid"
        ),
        Index("ix_vendor_invoices_site_status_due", "site_id", "status", "due_date"),
        Index("ix_vendor_invoices_order", "site_id", "order_id"),
        # Bir siparişte aynı anda tek bekleyen fark onayı
        Index(
            "uq_vendor_invoices_pending",
            "site_id",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'diff_pending'"),
        ),
    )

    order_id: Mapped[uuid.UUID]
    vendor_id: Mapped[uuid.UUID]
    number: Mapped[str] = mapped_column(Text)
    invoice_date: Mapped[dt.date] = mapped_column(Date)
    due_date: Mapped[dt.date] = mapped_column(Date)
    total: Mapped[Decimal] = mapped_column(MONEY)
    expected_total: Mapped[Decimal] = mapped_column(MONEY)
    diffs: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approval_request_id: Mapped[uuid.UUID | None]
    stored_file_id: Mapped[uuid.UUID | None]
    paid_on: Mapped[dt.date | None] = mapped_column(Date)
    cash_account_id: Mapped[uuid.UUID | None]
    expense_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)


class VendorInvoiceLine(TenantMixin, Base):
    """Fatura satırı (sipariş kalemi sırasıyla). **Değişmez.**"""

    __tablename__ = "vendor_invoice_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("invoice_id", "vendor_invoices"),
        tenant_fk("order_line_id", "purchase_order_lines"),
        UniqueConstraint("invoice_id", "order_line_id"),
        CheckConstraint("quantity >= 0", name="quantity"),
        CheckConstraint("unit_price >= 0", name="unit_price"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order_line_id: Mapped[uuid.UUID]
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_price: Mapped[Decimal] = mapped_column(MONEY)
    line_total: Mapped[Decimal] = mapped_column(MONEY)
