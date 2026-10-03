"""Finans: kurallar, bütçe, cari defter, tahakkuk — docs/03 §5–§7. Hepsi KİRACI tablosu.

Değişmez defterler (docs/04 §8): `ledger_entries`, `charges`, `charge_lines`,
`payment_allocations` satırları
güncellenemez ve silinemez — göçteki tetikleyici veritabanı düzeyinde reddeder. Düzeltme
her zaman ters kayıtla yapılır.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from site_yonetim.domain.charging.payments import PaymentMethod, PaymentStatus
from site_yonetim.domain.finance import (
    AllocationKind,
    AreaBasis,
    BudgetStatus,
    ChargeRunStatus,
    ExpenseCategoryKind,
    Frequency,
    LedgerSource,
    PayerRule,
    PeriodStatus,
    ScheduleRunStatus,
    ScopeKind,
)

MONEY = Numeric(18, 2)
WEIGHT = Numeric(12, 4)
PERCENT = Numeric(5, 2)
_COMPONENT_KIND_CHECK = "kind NOT IN ('composite', 'fixed_per_unit')"


# --- Kurallar ve bütçe (§6) ----------------------------------------------------------


class Period(TenantMixin, Base):
    """Mali dönem (ay). Kapalı döneme kayıt yazılamaz."""

    __tablename__ = "periods"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "year", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint(enum_check("status", PeriodStatus), name="status"),
    )

    year: Mapped[int]
    month: Mapped[int]
    status: Mapped[str] = mapped_column(Text, default=PeriodStatus.OPEN.value)


class ExpenseCategory(TenantMixin, Base):
    __tablename__ = "expense_categories"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("kind", ExpenseCategoryKind), name="kind"),
    )

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(default=0)


class ChargeType(TenantMixin, Base):
    """Tahakkuk tipi — kimin ödeyeceğini belirler (KMK m.22)."""

    __tablename__ = "charge_types"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("payer_rule", PayerRule), name="payer_rule"),
    )

    name: Mapped[str] = mapped_column(Text)
    payer_rule: Mapped[str] = mapped_column(Text)
    legal_basis: Mapped[str | None] = mapped_column(Text)
    is_advance: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)


class AllocationRule(TenantMixin, Base):
    __tablename__ = "allocation_rules"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("kind", AllocationKind), name="kind"),
        CheckConstraint(enum_check("area_basis", AreaBasis), name="area_basis"),
        CheckConstraint(
            "(kind = 'fixed_per_unit') = (fixed_amount IS NOT NULL)", name="fixed_amount"
        ),
    )

    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    fixed_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    area_basis: Mapped[str] = mapped_column(Text, default=AreaBasis.GROSS.value)
    note: Mapped[str | None] = mapped_column(Text)  # ör. "tüketim payı sayaç girilene kadar eşit"


class AllocationComponent(TenantMixin, Base):
    """Bileşik kuralın parçası: ör. %70 tüketim + %30 m²."""

    __tablename__ = "allocation_components"
    __table_args__ = tenant_table_args(
        tenant_fk("allocation_rule_id", "allocation_rules"),
        CheckConstraint(enum_check("kind", AllocationKind), name="kind"),
        CheckConstraint(_COMPONENT_KIND_CHECK, name="component_kind"),
        CheckConstraint(enum_check("area_basis", AreaBasis), name="area_basis"),
        CheckConstraint("percent > 0 AND percent <= 100", name="percent"),
    )

    allocation_rule_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text)
    area_basis: Mapped[str] = mapped_column(Text, default=AreaBasis.GROSS.value)
    percent: Mapped[Decimal] = mapped_column(PERCENT)
    sort_order: Mapped[int] = mapped_column(default=0)


class UnitWeight(TenantMixin, Base):
    """`by_custom_weight` için bölüm başına ağırlık."""

    __tablename__ = "unit_weights"
    __table_args__ = tenant_table_args(
        tenant_fk("allocation_rule_id", "allocation_rules"),
        tenant_fk("unit_id", "units"),
        UniqueConstraint("allocation_rule_id", "unit_id"),
        CheckConstraint("weight >= 0", name="weight"),
    )

    allocation_rule_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]
    weight: Mapped[Decimal] = mapped_column(WEIGHT)


class BudgetPlan(TenantMixin, Base):
    """İşletme projesi (KMK m.37). Kesinleşince değiştirilemez."""

    __tablename__ = "budget_plans"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("status", BudgetStatus), name="status"),
        CheckConstraint("fiscal_year BETWEEN 2000 AND 2100", name="fiscal_year"),
    )

    fiscal_year: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=BudgetStatus.DRAFT.value)
    notified_on: Mapped[dt.date | None] = mapped_column(Date)
    objection_deadline: Mapped[dt.date | None] = mapped_column(Date)
    finalized_on: Mapped[dt.date | None] = mapped_column(Date)


class BudgetItem(TenantMixin, Base):
    __tablename__ = "budget_items"
    __table_args__ = tenant_table_args(
        tenant_fk("budget_plan_id", "budget_plans"),
        tenant_fk("expense_category_id", "expense_categories"),
        tenant_fk("charge_type_id", "charge_types"),
        tenant_fk("allocation_rule_id", "allocation_rules"),
        CheckConstraint(enum_check("frequency", Frequency), name="frequency"),
        CheckConstraint(enum_check("scope_kind", ScopeKind), name="scope_kind"),
    )

    budget_plan_id: Mapped[uuid.UUID] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(Text)
    expense_category_id: Mapped[uuid.UUID]
    charge_type_id: Mapped[uuid.UUID]
    allocation_rule_id: Mapped[uuid.UUID]
    annual_amount: Mapped[Decimal] = mapped_column(MONEY)
    frequency: Mapped[str] = mapped_column(Text, default=Frequency.MONTHLY.value)
    scope_kind: Mapped[str] = mapped_column(Text, default=ScopeKind.WHOLE_SITE.value)
    # Dizi öğeleri için FK yok: servis, id'lerin bu sitenin blok/tipi olduğunu doğrular.
    scope_block_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    scope_unit_type_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    scope_usage: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(default=0)


class LateFeePolicy(TenantMixin, Base):
    """Gecikme tazminatı (KMK m.20) — site başına bir kayıt. Oran ve yöntem: docs/12 K2."""

    __tablename__ = "late_fee_policies"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("monthly_rate_percent >= 0", name="rate"),
        CheckConstraint("grace_days >= 0", name="grace_days"),
    )

    monthly_rate_percent: Mapped[Decimal] = mapped_column(PERCENT, default=Decimal(5))
    grace_days: Mapped[int] = mapped_column(default=5)
    minimum_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    is_enabled: Mapped[bool] = mapped_column(default=True)


# --- Cari defter (§5) ---------------------------------------------------------------


class LedgerEntry(TenantMixin, Base):
    """Borç/alacak hareketi — **değişmez**. Bakiye = Σborç − Σalacak (pozitif = borçlu)."""

    __tablename__ = "ledger_entries"
    __table_args__ = tenant_table_args(
        tenant_fk("account_id", "ledger_accounts"),
        ForeignKeyConstraint(
            ["site_id", "reversal_of_entry_id"], ["ledger_entries.site_id", "ledger_entries.id"]
        ),
        CheckConstraint(
            "debit >= 0 AND credit >= 0 AND (debit > 0) <> (credit > 0)", name="one_side"
        ),
        CheckConstraint(enum_check("source", LedgerSource), name="source"),
        Index("ix_ledger_entries_site_account_date", "site_id", "account_id", "date"),
        Index("ix_ledger_entries_source", "site_id", "source_id"),
        Index(  # devir bakiye hesap başına bir kez (düzeltme ters kayıtla)
            "uq_ledger_entries_opening",
            "account_id",
            unique=True,
            postgresql_where=text("source = 'opening'"),
        ),
        Index(
            "uq_ledger_entries_reversal_of",
            "reversal_of_entry_id",
            unique=True,
            postgresql_where=text("reversal_of_entry_id IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID]
    date: Mapped[dt.date] = mapped_column(Date)
    due_date: Mapped[dt.date | None] = mapped_column(Date)
    debit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    credit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    source: Mapped[str] = mapped_column(Text)
    source_id: Mapped[uuid.UUID | None]
    description: Mapped[str] = mapped_column(Text)
    reversal_of_entry_id: Mapped[uuid.UUID | None]


class AccountBalance(TenantMixin, Base):
    """Özet bakiye (docs/08 §2) — hareket yazılırken aynı transaction'da güncellenir.

    Tek doğruluk kaynağı defterdir; bu tablo ondan her an yeniden üretilebilir
    (`services/ledger.rebuild_balances`).
    """

    __tablename__ = "account_balances"
    __table_args__ = tenant_table_args(
        tenant_fk("account_id", "ledger_accounts"),
        UniqueConstraint("account_id"),
        Index("ix_account_balances_site_balance", "site_id", "balance"),
    )

    account_id: Mapped[uuid.UUID]
    debit_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    credit_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    oldest_open_due_date: Mapped[dt.date | None] = mapped_column(Date)


class SiteFinanceSummary(TenantMixin, Base):
    """Site ve dönem başına pano sayıları (docs/08 §2) — tahakkuk, ters kayıt ve tahsilatla aynı
    transaction'da artımlı güncellenir. `charged`: o dönem kesilen (geçerli) borç; `collected`:
    tarihi o ayda olan onaylı tahsilat."""

    __tablename__ = "site_finance_summary"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "year", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
    )

    year: Mapped[int]
    month: Mapped[int]
    charged: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    collected: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))


# --- Tahakkuk (§7) --------------------------------------------------------------------


class ChargeRun(TenantMixin, Base):
    """Bir dönemin tahakkuk koşusu. Bir dönem için en fazla bir geçerli koşu (kısmi indeks)."""

    __tablename__ = "charge_runs"
    __table_args__ = tenant_table_args(
        tenant_fk("period_id", "periods"),
        tenant_fk("budget_plan_id", "budget_plans"),
        ForeignKeyConstraint(
            ["site_id", "reversal_of_run_id"], ["charge_runs.site_id", "charge_runs.id"]
        ),
        CheckConstraint(enum_check("status", ChargeRunStatus), name="status"),
        Index(
            "uq_charge_runs_valid_period",
            "site_id",
            "period_id",
            unique=True,
            postgresql_where=text("status = 'posted' AND reversal_of_run_id IS NULL"),
        ),
        Index(
            "uq_charge_runs_reversal_of",
            "reversal_of_run_id",
            unique=True,
            postgresql_where=text("reversal_of_run_id IS NOT NULL"),
        ),
    )

    period_id: Mapped[uuid.UUID]
    budget_plan_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(Text, default=ChargeRunStatus.DRAFT.value)
    charge_date: Mapped[dt.date] = mapped_column(Date)
    due_date: Mapped[dt.date] = mapped_column(Date)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(Text)  # işlemi yapanın adı
    reversal_of_run_id: Mapped[uuid.UUID | None]
    reason: Mapped[str | None] = mapped_column(Text)  # ters kayıt gerekçesi


class Charge(TenantMixin, Base):
    """Bir bölümün o koşudaki borcu — **değişmez**. Bir bölüm iki borç alabilir (oturan/malik)."""

    __tablename__ = "charges"
    __table_args__ = tenant_table_args(
        tenant_fk("charge_run_id", "charge_runs"),
        tenant_fk("unit_id", "units"),
        tenant_fk("ledger_account_id", "ledger_accounts"),
    )

    charge_run_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID]
    ledger_account_id: Mapped[uuid.UUID] = mapped_column(index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)


class ChargeLine(TenantMixin, Base):
    """Borcun kalem dökümü — "bu tutar nasıl hesaplandı". **Değişmez.**"""

    __tablename__ = "charge_lines"
    __table_args__ = tenant_table_args(
        tenant_fk("charge_id", "charges"),
        tenant_fk("budget_item_id", "budget_items"),
        CheckConstraint(enum_check("allocation_kind", AllocationKind), name="allocation_kind"),
        Index("ix_charge_lines_budget_item", "site_id", "budget_item_id"),
    )

    charge_id: Mapped[uuid.UUID] = mapped_column(index=True)
    budget_item_id: Mapped[uuid.UUID]
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    allocation_kind: Mapped[str] = mapped_column(Text)
    weight: Mapped[Decimal | None] = mapped_column(Numeric)
    weight_total: Mapped[Decimal | None] = mapped_column(Numeric)
    source_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    explanation: Mapped[str | None] = mapped_column(Text)


# --- Tahsilat (§7) ----------------------------------------------------------------------


class Payment(TenantMixin, Base):
    """Tahsilat. Cari hesaba tamamı kadar alacak hareketi yazılır (`source = payment`)."""

    __tablename__ = "payments"
    __table_args__ = tenant_table_args(
        tenant_fk("ledger_account_id", "ledger_accounts"),
        tenant_fk("cash_account_id", "cash_accounts"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(enum_check("method", PaymentMethod), name="method"),
        CheckConstraint(enum_check("status", PaymentStatus), name="status"),
        Index("ix_payments_site_date", "site_id", "date"),
        Index("ix_payments_account", "site_id", "ledger_account_id"),
    )

    ledger_account_id: Mapped[uuid.UUID]
    amount: Mapped[Decimal] = mapped_column(MONEY)
    date: Mapped[dt.date] = mapped_column(Date)
    method: Mapped[str] = mapped_column(Text)
    cash_account_id: Mapped[uuid.UUID | None]
    reference: Mapped[str | None] = mapped_column(Text)  # boşsa hesabın referans kodu
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=PaymentStatus.CONFIRMED.value)
    created_by_name: Mapped[str | None] = mapped_column(Text)


class PaymentAllocation(TenantMixin, Base):
    """Tahsilatın hangi borca sayıldığı — **değişmez**."""

    __tablename__ = "payment_allocations"
    __table_args__ = tenant_table_args(
        tenant_fk("payment_id", "payments"),
        tenant_fk("ledger_entry_id", "ledger_entries"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_payment_allocations_entry", "site_id", "ledger_entry_id"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(index=True)
    ledger_entry_id: Mapped[uuid.UUID]
    amount: Mapped[Decimal] = mapped_column(MONEY)


# --- Idempotency (docs/06 §1.5) --------------------------------------------------------


class IdempotencyKey(TenantMixin, Base):
    """Para yazan isteğin sonucu: aynı anahtarla ikinci istek aynı yanıtı alır (24 saat)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "user_id", "key"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text)  # yöntem + yol + gövde özeti
    status_code: Mapped[int]
    response_body: Mapped[dict[str, object]] = mapped_column(JSONB)


# --- Borçsuzluk belgesi (servis isteği 01) ----------------------------------------------


class ClearanceCertificate(TenantMixin, Base):
    """Borçsuzluk belgesi — **değişmez** (tetikleyici). Belge anındaki hesap bilgisi ve
    defterden hesaplanan bakiye satıra yazılır; sonradan hesap/kişi değişse de belge aynı kalır.

    Numara site ve yıl bazında boşluksuz artar: `BB-{yıl}-{5 hane}`.
    """

    __tablename__ = "clearance_certificates"
    __table_args__ = tenant_table_args(
        tenant_fk("ledger_account_id", "ledger_accounts"),
        UniqueConstraint("site_id", "year", "sequence"),
        UniqueConstraint("site_id", "number"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint("balance <= 0.005", name="no_debt"),
        CheckConstraint("valid_until >= as_of", name="valid_after_as_of"),
    )

    ledger_account_id: Mapped[uuid.UUID] = mapped_column(index=True)
    year: Mapped[int]
    sequence: Mapped[int]
    number: Mapped[str] = mapped_column(Text)
    # belge anındaki görüntü
    reference_code: Mapped[str] = mapped_column(Text)
    account_kind: Mapped[str] = mapped_column(Text)
    unit_name: Mapped[str] = mapped_column(Text)
    person_name: Mapped[str] = mapped_column(Text)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    as_of: Mapped[dt.date] = mapped_column(Date)
    valid_until: Mapped[dt.date] = mapped_column(Date)
    issued_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    issued_by_name: Mapped[str] = mapped_column(Text)


# --- Otomatik aylık tahakkuk (servis isteği 04) -----------------------------------------


class ChargeSchedule(TenantMixin, Base):
    """Site başına otomatik tahakkuk ayarı. Gece işi `charge_day` geldiğinde elle kaydetmeyle
    aynı servisle keser (`services/charge_schedule.py`)."""

    __tablename__ = "charge_schedules"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("charge_day BETWEEN 1 AND 28", name="charge_day_range"),
        CheckConstraint("due_days BETWEEN 0 AND 60", name="due_days_range"),
    )

    enabled: Mapped[bool] = mapped_column(default=False)
    charge_day: Mapped[int] = mapped_column(default=1)
    due_days: Mapped[int] = mapped_column(default=14)
    notify_on_run: Mapped[bool] = mapped_column(default=True)
    # Açıldığı gün (İstanbul): o günden önceki kesim günleri geriye dönük kesilmez.
    enabled_on: Mapped[dt.date | None] = mapped_column(Date)


class ChargeScheduleRun(TenantMixin, Base):
    """Otomatik tahakkukun ay başına sonucu — ay başına bir satır (iş iki kez çalışsa da)."""

    __tablename__ = "charge_schedule_runs"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "year", "month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint(enum_check("status", ScheduleRunStatus), name="status"),
        tenant_fk("charge_run_id", "charge_runs"),
    )

    year: Mapped[int]
    month: Mapped[int]
    status: Mapped[str] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    charge_run_id: Mapped[uuid.UUID | None]
    ran_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
