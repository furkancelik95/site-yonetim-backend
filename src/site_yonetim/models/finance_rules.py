"""Finans motoru, ikinci yarı (P03b): aidat kuralları (FIN-02), tahakkuk satır düzeltmesi
(FIN-05), gecikme tazminatı tahakkuku (FIN-03, K2), yıllık bütçe ve bütçe–gerçekleşme (FIN-17).
Hepsi KİRACI tablosu (RLS).

Değişmez: `late_fee_accruals`, `budget_alerts` (tetikleyici, göç 0028).
"""

import datetime as dt
import uuid
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
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.finance import AllocationKind, AreaBasis, Frequency, ScopeKind

MONEY = Numeric(18, 2)

BILLING_RULE_STATUSES = ("draft", "approval_pending", "active", "rejected")
AMOUNT_MODES = ("per_unit", "total")
ADJUSTMENT_STATUSES = ("approval_pending", "applied", "rejected")
BUDGET_STATUSES = ("draft", "approval_pending", "active", "rejected", "superseded")


# --- Aidat kuralları (FIN-02, K23) ---------------------------------------------------------


class BillingRule(TenantMixin, Base):
    """Aidat kuralının kimliği: ad, kalem tipi (kim öder) ve "malik kalemi" işareti.
    Hesap alanları sürümdedir; değişiklik yeni sürümdür ve yalnız yürürlüğünden sonraki
    dönemleri etkiler."""

    __tablename__ = "billing_rules"
    __table_args__ = tenant_table_args(
        tenant_fk("charge_type_id", "charge_types"),
    )

    name: Mapped[str] = mapped_column(Text)
    charge_type_id: Mapped[uuid.UUID]
    # Malik kalemi: kim öder kuralından bağımsız olarak malike tahakkuk eder, kiracıya görünmez.
    owner_item: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[str | None] = mapped_column(Text)


class BillingRuleVersion(TenantMixin, Base):
    """Kuralın bir sürümü. `active` olan ve yürürlüğü dönemden önce başlamış en son sürüm o
    dönemde geçerlidir; `discontinued` sürüm kuralı o dönemden itibaren durdurur."""

    __tablename__ = "billing_rule_versions"
    __table_args__ = tenant_table_args(
        tenant_fk("rule_id", "billing_rules"),
        tenant_fk("approval_request_id", "approval_requests"),
        tenant_fk("basis_document_id", "documents"),
        UniqueConstraint("rule_id", "version_no"),
        CheckConstraint(
            "status IN ('draft', 'approval_pending', 'active', 'rejected')", name="status"
        ),
        CheckConstraint(enum_check("kind", AllocationKind), name="kind"),
        CheckConstraint(enum_check("area_basis", AreaBasis), name="area_basis"),
        CheckConstraint("amount_mode IN ('per_unit', 'total')", name="amount_mode"),
        CheckConstraint(enum_check("frequency", Frequency), name="frequency"),
        CheckConstraint(enum_check("scope_kind", ScopeKind), name="scope_kind"),
        CheckConstraint("amount >= 0", name="amount"),
        CheckConstraint("effective_month BETWEEN 1 AND 12", name="effective_month"),
        Index("ix_billing_rule_versions_rule", "site_id", "rule_id", "status"),
    )

    rule_id: Mapped[uuid.UUID]
    version_no: Mapped[int]
    status: Mapped[str] = mapped_column(Text, default="draft")
    effective_year: Mapped[int]
    effective_month: Mapped[int]
    kind: Mapped[str] = mapped_column(Text)  # eşit · m² · arsa payı · bölüm tipi · sabit · bileşik
    area_basis: Mapped[str] = mapped_column(Text, default=AreaBasis.GROSS.value)
    amount_mode: Mapped[str] = mapped_column(Text)  # per_unit: bölüm başına · total: dağıtılacak
    amount: Mapped[Decimal] = mapped_column(MONEY)  # bir dönemin tutarı
    frequency: Mapped[str] = mapped_column(Text, default=Frequency.MONTHLY.value)
    # bileşik: [{"kind", "percent", "area_basis"}]
    components: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # bölüm tipi katsayıları: {"<unit_type_id>": "1.50"}
    type_coefficients: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # muafiyetler: [{"unit_id", "percent", "reason"}]
    exemptions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    scope_kind: Mapped[str] = mapped_column(Text, default=ScopeKind.WHOLE_SITE.value)
    scope_block_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    scope_unit_type_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    scope_usage: Mapped[str | None] = mapped_column(Text)
    discontinued: Mapped[bool] = mapped_column(default=False)
    basis_note: Mapped[str] = mapped_column(Text)  # dayanak: yönetim planı maddesi / kurul kararı
    basis_document_id: Mapped[uuid.UUID | None]
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- Kesinleşmiş tahakkukta satır düzeltmesi (FIN-05, D-10) ---------------------------------


class ChargeAdjustment(TenantMixin, Base):
    """Bir tahakkuk satırının düzeltmesi. Orijinal satır değişmez; borcun defter hareketi ters
    kaydedilir ve düzeltilmiş toplamla yeni borç yazılır. Orijinal dönem kilitliyse ikisi de
    açık döneme "önceki döneme ait" işaretiyle yazılır (`prior_period`)."""

    __tablename__ = "charge_adjustments"
    __table_args__ = tenant_table_args(
        tenant_fk("charge_id", "charges"),
        tenant_fk("charge_line_id", "charge_lines"),
        tenant_fk("ledger_account_id", "ledger_accounts"),
        tenant_fk("approval_request_id", "approval_requests"),
        tenant_fk("reversal_entry_id", "ledger_entries"),
        tenant_fk("new_entry_id", "ledger_entries"),
        CheckConstraint("status IN ('approval_pending', 'applied', 'rejected')", name="status"),
        CheckConstraint("new_amount >= 0", name="new_amount"),
        Index(
            "uq_charge_adjustments_pending",
            "charge_id",
            unique=True,
            postgresql_where=text("status = 'approval_pending'"),
        ),
    )

    charge_id: Mapped[uuid.UUID] = mapped_column(index=True)
    charge_line_id: Mapped[uuid.UUID]
    ledger_account_id: Mapped[uuid.UUID]
    old_amount: Mapped[Decimal] = mapped_column(MONEY)
    new_amount: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    prior_period: Mapped[bool] = mapped_column(default=False)
    applied_on: Mapped[dt.date | None] = mapped_column(Date)
    reversal_entry_id: Mapped[uuid.UUID | None]
    new_entry_id: Mapped[uuid.UUID | None]
    approval_request_id: Mapped[uuid.UUID | None]
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    requested_by: Mapped[str | None] = mapped_column(Text)


# --- Gecikme tazminatı (FIN-03, K2, docs/04 §6) ------------------------------------------


class LateFeeAccrual(TenantMixin, Base):
    """Gecikme hareketinin (`ledger_entries.source = late_fee`) ana borç dökümü — **değişmez**.
    Bir ana borca o güne kadar işlemiş toplam = bu satırların toplamı; yeni hesap yalnız farkı
    yazar (aynı gün ikinci çalışma bir şey yazmaz)."""

    __tablename__ = "late_fee_accruals"
    __table_args__ = tenant_table_args(
        tenant_fk("ledger_entry_id", "ledger_entries"),
        tenant_fk("principal_entry_id", "ledger_entries"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_late_fee_accruals_principal", "site_id", "principal_entry_id"),
    )

    ledger_entry_id: Mapped[uuid.UUID]
    principal_entry_id: Mapped[uuid.UUID]
    amount: Mapped[Decimal] = mapped_column(MONEY)
    as_of: Mapped[dt.date] = mapped_column(Date)


class LateFeeRun(TenantMixin, Base):
    """Ay sonu gecikme işinin dönem başına sonucu (bir kez)."""

    __tablename__ = "late_fee_runs"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "year", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
    )

    year: Mapped[int]
    month: Mapped[int]
    account_count: Mapped[int] = mapped_column(default=0)
    total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    message: Mapped[str | None] = mapped_column(Text)
    ran_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


# --- Yıllık bütçe ve bütçe–gerçekleşme (FIN-17, §8.10) -----------------------------------


class Budget(TenantMixin, Base):
    """Mali yıl bütçesi — kategori × ay. Revizyon yeni sürümdür ve aynı "Bütçe" onayından geçer;
    yürürlüğe giren sürüm aynı yılın önceki yürürlükteki sürümünü `superseded` yapar."""

    __tablename__ = "budgets"
    __table_args__ = tenant_table_args(
        tenant_fk("budget_plan_id", "budget_plans"),
        tenant_fk("approval_request_id", "approval_requests"),
        UniqueConstraint("site_id", "fiscal_year", "version"),
        CheckConstraint(
            "status IN ('draft', 'approval_pending', 'active', 'rejected', 'superseded')",
            name="status",
        ),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 2100", name="fiscal_year"),
        CheckConstraint("income_target IS NULL OR income_target >= 0", name="income_target"),
        Index(
            "uq_budgets_active_year",
            "site_id",
            "fiscal_year",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    fiscal_year: Mapped[int]
    version: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="draft")
    income_target: Mapped[Decimal | None] = mapped_column(MONEY)  # aidat ve diğer gelir hedefi
    budget_plan_id: Mapped[uuid.UUID | None]  # öneri kaynağı işletme projesi
    approval_request_id: Mapped[uuid.UUID | None]
    created_by: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetLine(TenantMixin, Base):
    __tablename__ = "budget_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("budget_id", "budgets"),
        tenant_fk("expense_category_id", "expense_categories"),
        UniqueConstraint("budget_id", "expense_category_id", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("amount >= 0", name="amount"),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(index=True)
    expense_category_id: Mapped[uuid.UUID]
    month: Mapped[int]
    amount: Mapped[Decimal] = mapped_column(MONEY)


class BudgetAlert(TenantMixin, Base):
    """Kategori eşik uyarısı (%90 / %100) — bütçe, kategori ve eşik başına bir kez. **Değişmez.**"""

    __tablename__ = "budget_alerts"
    __table_args__ = tenant_table_args(
        tenant_fk("budget_id", "budgets"),
        tenant_fk("expense_category_id", "expense_categories"),
        UniqueConstraint("budget_id", "expense_category_id", "threshold"),
    )

    budget_id: Mapped[uuid.UUID]
    expense_category_id: Mapped[uuid.UUID]
    threshold: Mapped[int]
    budget_amount: Mapped[Decimal] = mapped_column(MONEY)
    actual_amount: Mapped[Decimal] = mapped_column(MONEY)
