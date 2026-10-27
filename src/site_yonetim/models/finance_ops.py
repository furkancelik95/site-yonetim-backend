"""Finans Mod P işlemleri (Yazılım Dokümanı FIN-06, 11–14, 16; servis istekleri 27–33).
Hepsi KİRACI tablosu (RLS).

Değişmez (tetikleyici `forbid_history_change`): `period_events`, `collection_actions`,
`payment_plan_installments`, `payment_plan_closures`, `accounting_mapping_versions`,
`accounting_exports`, `accounting_export_items`. Diğerlerinde yalnız durum alanları değişir;
para tutarı hiçbir zaman güncellenmez — düzeltme ters kayıtla (docs/04 §8).

Onaya bağlı kayıtlar (`approval_request_id`): kural varsa kayıt `approval_pending` açılır, onayda
yürütücü kesinleştirir; ret/geri çekmede `rejected` olur (`services/approvals.register_closer`).
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
    LargeBinary,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.debt_tracking import (
    ActionKind,
    CollectionChannel,
    LetterMethod,
    LetterStatus,
    PlanStatus,
)
from site_yonetim.domain.structure import AccountKind
from site_yonetim.models.finance import MONEY

# --- Dönem kapanışı (FIN-13) -------------------------------------------------------------


class PeriodEvent(TenantMixin, Base):
    """Dönem durum değişikliği geçmişi — **değişmez**."""

    __tablename__ = "period_events"
    __table_args__ = tenant_table_args(
        tenant_fk("period_id", "periods"),
        CheckConstraint(
            "action IN ('start_close', 'reopen', 'lock', 'unlock_requested', 'unlock', "
            "'unlock_rejected', 'relocked')",
            name="action",
        ),
    )

    period_id: Mapped[uuid.UUID] = mapped_column(index=True)
    action: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(Text)


# --- Kasa gün sonu (FIN-11) --------------------------------------------------------------

CASH_DAY_STATUSES = ("approval_pending", "closed", "rejected")


class CashDay(TenantMixin, Base):
    """Kasa günü sayımı. Kapanan (ya da onay bekleyen) güne yeni kasa hareketi yazılamaz.
    Ret edilen sayım geçmişte kalır; gün yeniden sayılabilir (kısmi benzersiz indeks)."""

    __tablename__ = "cash_days"
    __table_args__ = tenant_table_args(
        tenant_fk("cash_account_id", "cash_accounts"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint("status IN ('approval_pending', 'closed', 'rejected')", name="status"),
        CheckConstraint("counted >= 0", name="counted"),
        CheckConstraint("difference = counted - system_balance", name="difference"),
        Index(
            "uq_cash_days_account_date",
            "cash_account_id",
            "date",
            unique=True,
            postgresql_where=text("status <> 'rejected'"),
        ),
    )

    cash_account_id: Mapped[uuid.UUID]
    date: Mapped[dt.date] = mapped_column(Date)
    system_balance: Mapped[Decimal] = mapped_column(MONEY)
    counted: Mapped[Decimal] = mapped_column(MONEY)
    difference: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    closed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    closed_by: Mapped[str] = mapped_column(Text)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approval_request_id: Mapped[uuid.UUID | None]


# --- Borç takibi (FIN-12) ----------------------------------------------------------------


class CollectionAction(TenantMixin, Base):
    """Hatırlatma / arama / not kaydı — **değişmez**. Gece işinin kademe kaydı (`stage`) aynı
    açık vade (`due_ref`) için bir kez yazılır (kısmi benzersiz indeks)."""

    __tablename__ = "collection_actions"
    __table_args__ = tenant_table_args(
        tenant_fk("account_id", "ledger_accounts"),
        CheckConstraint(enum_check("kind", ActionKind), name="kind"),
        CheckConstraint(
            f"channel IS NULL OR {enum_check('channel', CollectionChannel)}", name="channel"
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('app', 'sms', 'call', 'demand_letter')", name="stage"
        ),
        Index("ix_collection_actions_account", "site_id", "account_id", "created_at"),
        Index(
            "uq_collection_actions_stage",
            "account_id",
            "due_ref",
            "stage",
            unique=True,
            postgresql_where=text("stage IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)  # gece işi kademesi
    due_ref: Mapped[dt.date | None] = mapped_column(Date)  # kademenin bağlı olduğu açık vade
    by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    by_name: Mapped[str] = mapped_column(Text)


class DemandLetter(TenantMixin, Base):
    """İhtarname gönderim kaydı ve tebliğ. "İhtarname" onay kuralı varsa onaydan sonra `sent`."""

    __tablename__ = "demand_letters"
    __table_args__ = tenant_table_args(
        tenant_fk("account_id", "ledger_accounts"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("method", LetterMethod), name="method"),
        CheckConstraint(enum_check("status", LetterStatus), name="status"),
        CheckConstraint("served_on IS NULL OR served_on >= sent_on", name="served_after_sent"),
        Index("ix_demand_letters_account", "site_id", "account_id"),
    )

    account_id: Mapped[uuid.UUID]
    method: Mapped[str] = mapped_column(Text)
    sent_on: Mapped[dt.date] = mapped_column(Date)
    tracking_no: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    served_on: Mapped[dt.date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(Text)


# --- Ödeme planı (FIN-12, §8.9) ---------------------------------------------------------

_STORED_PLAN = ("approval_pending", "active", "cancelled", "rejected")


class PaymentPlan(TenantMixin, Base):
    """Borç yapılandırma. Yürürlüğe girince (onaydan sonra) yapılandırılan açık kalemler kapanır
    (`payment_plan_closures` + alacak hareketi), taksitler ayrı vadeli borç hareketleridir.
    Saklanan durum: onay bekliyor / yürürlükte / iptal / reddedildi; "bozuldu" ve "tamamlandı"
    taksitlerden hesaplanır (`broken_at`: bozulma bildirimi bir kez)."""

    __tablename__ = "payment_plans"
    __table_args__ = tenant_table_args(
        tenant_fk("account_id", "ledger_accounts"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(
            "status IN ('approval_pending', 'active', 'cancelled', 'rejected')", name="status"
        ),
        CheckConstraint("total > 0", name="total_positive"),
        CheckConstraint("installment_count BETWEEN 2 AND 36", name="installment_count"),
        Index("ix_payment_plans_account", "site_id", "account_id"),
    )

    account_id: Mapped[uuid.UUID]
    total: Mapped[Decimal] = mapped_column(MONEY)
    installment_count: Mapped[int]
    first_due_date: Mapped[dt.date] = mapped_column(Date)
    tolerance_days: Mapped[int]
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=PlanStatus.APPROVAL_PENDING.value)
    activated_on: Mapped[dt.date | None] = mapped_column(Date)
    broken_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(Text)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_by: Mapped[str | None] = mapped_column(Text)


class PaymentPlanInstallment(TenantMixin, Base):
    """Taksit — **değişmez**; cari defterdeki borç hareketine bağlı."""

    __tablename__ = "payment_plan_installments"
    __table_args__ = tenant_table_args(
        tenant_fk("plan_id", "payment_plans"),
        tenant_fk("ledger_entry_id", "ledger_entries"),
        UniqueConstraint("plan_id", "no"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(index=True)
    no: Mapped[int]
    due_date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    ledger_entry_id: Mapped[uuid.UUID]


class PaymentPlanClosure(TenantMixin, Base):
    """Plan alacağının hangi borç hareketini kapattığı — **değişmez**. Açık borç hesabı
    (`payments._OPEN_DEBTS`) tahsilat mahsuplarıyla birlikte bunları da düşer.
    `restructure`: yapılandırılan eski kalemler · `cancel`: iptalde ödenmemiş taksitler."""

    __tablename__ = "payment_plan_closures"
    __table_args__ = tenant_table_args(
        tenant_fk("plan_id", "payment_plans"),
        tenant_fk("ledger_entry_id", "ledger_entries"),
        CheckConstraint("kind IN ('restructure', 'cancel')", name="kind"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_payment_plan_closures_entry", "site_id", "ledger_entry_id"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(index=True)
    ledger_entry_id: Mapped[uuid.UUID]
    kind: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)


# --- Dönem dışı ücretler (FIN-06) -------------------------------------------------------

AD_HOC_SOURCES = ("reservation", "service", "renovation", "penalty", "other")
AD_HOC_STATUSES = ("approval_pending", "posted", "waiting_next_run", "cancelled", "rejected")


class AdHocChargeSettings(TenantMixin, Base):
    """Kaynak bazında yansıma ayarı (site başına bir satır): `{kaynak: {mode, due_days}}`."""

    __tablename__ = "ad_hoc_charge_settings"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id"))

    sources: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AdHocCharge(TenantMixin, Base):
    """Dönem dışı ücret. `immediate`: tek kalem borç hareketi (`source = ad_hoc_charge`);
    `next_period`: bir sonraki tahakkukta "ek kalemler" başlığıyla yazılır. İptal silmez:
    yazılmış ücret ters kayıtla kapanır."""

    __tablename__ = "ad_hoc_charges"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("account_id", "ledger_accounts"),
        tenant_fk("ledger_entry_id", "ledger_entries"),
        tenant_fk("reversal_entry_id", "ledger_entries"),
        tenant_fk("charge_run_id", "charge_runs"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(
            "source IN ('reservation', 'service', 'renovation', 'penalty', 'other')",
            name="source",
        ),
        CheckConstraint("mode IN ('immediate', 'next_period')", name="mode"),
        CheckConstraint(
            "status IN ('approval_pending', 'posted', 'waiting_next_run', 'cancelled', 'rejected')",
            name="status",
        ),
        CheckConstraint(enum_check("account_kind", AccountKind), name="account_kind"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_ad_hoc_charges_site_status", "site_id", "status"),
    )

    unit_id: Mapped[uuid.UUID]
    account_id: Mapped[uuid.UUID]
    account_kind: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    due_date: Mapped[dt.date | None] = mapped_column(Date)  # boşsa yazılırken: bugün + gün
    due_days: Mapped[int]
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    ledger_entry_id: Mapped[uuid.UUID | None]
    reversal_entry_id: Mapped[uuid.UUID | None]
    charge_run_id: Mapped[uuid.UUID | None]  # sonraki tahakkukta yazıldığı koşu
    approval_request_id: Mapped[uuid.UUID | None]
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(Text)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)


# --- Muhasebe aktarımı (FIN-14) ---------------------------------------------------------


class AccountingMappingVersion(TenantMixin, Base):
    """Hesap kodu eşlemesi — **versiyonlu, değişmez**; son sürüm geçerli. Değişiklik yalnız
    sonraki dosyaları etkiler (aktarılan fiş kendi kodlarını taşır)."""

    __tablename__ = "accounting_mapping_versions"
    __table_args__ = tenant_table_args()

    items: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(Text)


class AccountingExport(TenantMixin, Base):
    """Üretilen fiş dosyası — **değişmez**; dosya yeniden indirilebilir."""

    __tablename__ = "accounting_exports"
    __table_args__ = tenant_table_args(
        CheckConstraint("month BETWEEN 1 AND 12", name="month_range"),
        Index("ix_accounting_exports_period", "site_id", "year", "month"),
    )

    year: Mapped[int]
    month: Mapped[int]
    voucher_count: Mapped[int]
    total: Mapped[Decimal] = mapped_column(MONEY)
    file_name: Mapped[str] = mapped_column(Text)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(Text)


class AccountingExportItem(TenantMixin, Base):
    """Aktarılan fiş — **değişmez**. `voucher_key` sitede tekil: aynı fiş iki kez aktarılmaz."""

    __tablename__ = "accounting_export_items"
    __table_args__ = tenant_table_args(
        tenant_fk("export_id", "accounting_exports"),
        UniqueConstraint("site_id", "voucher_key"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )

    export_id: Mapped[uuid.UUID] = mapped_column(index=True)
    voucher_key: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    date: Mapped[dt.date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    debit_account: Mapped[str] = mapped_column(Text)
    credit_account: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)


# --- Finans parametreleri (FIN-16) ------------------------------------------------------


class FinanceParameterVersion(TenantMixin, Base):
    """Parametre sürümü: tüm değerlerin tam kopyası. Bir dönemde geçerli = yürürlüğü o döneme
    kadar başlamış son **aktif** sürüm. Finansal etkili değişiklik onay kuralından geçer."""

    __tablename__ = "finance_parameter_versions"
    __table_args__ = tenant_table_args(
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint("effective_month BETWEEN 1 AND 12", name="month_range"),
        CheckConstraint("status IN ('approval_pending', 'active', 'rejected')", name="status"),
        Index(
            "ix_finance_parameter_versions_effective",
            "site_id",
            "status",
            "effective_year",
            "effective_month",
        ),
    )

    effective_year: Mapped[int]
    effective_month: Mapped[int]
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    changed: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    changed_by: Mapped[str] = mapped_column(Text)
