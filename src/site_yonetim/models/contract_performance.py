"""Sözleşme performansı — servis istekleri 68–70 (Yazılım Dokümanı 15.3, SAT-06).

KİRACI tabloları: bedel geçmişi, KPI ve ceza kuralları, aylık karne, ceza kesintisi, artış kararı.
GLOBAL tablo: endeks değerleri (TÜFE/ÜFE, tüm tesislerde ortak; platform yöneticisi yazar).

Silinmez: bedel geçmişi ve endeks değeri **değişmez** (yeni satır; düzeltme de yeni satırdır);
kesinleşen karne değişmez; ceza kesintisi ve artış kararı yalnız durum değiştirir. KPI ve ceza
**tanımları** ayardır: kaldırılabilir (karne satırları anlık görüntüyle saklandığı için geçmiş
bozulmaz).
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
from site_yonetim.domain.contract_performance import (
    AmountSource,
    CutKind,
    EscalationStatus,
    KpiSource,
    PenaltyStatus,
    ScorecardStatus,
)
from site_yonetim.models.finance import MONEY

RATE = Numeric(6, 2)
PERCENT = Numeric(5, 2)


class ContractAmount(TenantMixin, Base):
    """Bedel geçmişi — **değişmez**. `effective_from` gününden itibaren geçerli bedel; aynı güne
    ikinci satır yazılırsa son yazılan geçerlidir. Önceki tarihlerin hesabı eski bedelle yapılır."""

    __tablename__ = "contract_amounts"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        tenant_fk("escalation_id", "contract_escalations"),
        CheckConstraint(enum_check("source", AmountSource), name="source"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount"),
        CheckConstraint("period IS NULL OR period IN ('monthly', 'yearly', 'once')", name="period"),
        Index("ix_contract_amounts_effective", "site_id", "contract_id", "effective_from"),
    )

    contract_id: Mapped[uuid.UUID]
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    period: Mapped[str | None] = mapped_column(Text)
    effective_from: Mapped[dt.date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(Text)
    escalation_id: Mapped[uuid.UUID | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by: Mapped[str] = mapped_column(Text)


class ContractKpi(TenantMixin, Base):
    """Sözleşmenin aylık KPI'ı. Hedef %, ağırlık; toplam ağırlık 100 (uygulamada doğrulanır)."""

    __tablename__ = "contract_kpis"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        CheckConstraint(enum_check("source", KpiSource), name="source"),
        CheckConstraint("target > 0 AND target <= 100", name="target"),
        CheckConstraint("weight BETWEEN 1 AND 100", name="weight"),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(index=True)
    position: Mapped[int]
    name: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    target: Mapped[Decimal] = mapped_column(PERCENT)
    weight: Mapped[int]
    request_category: Mapped[str | None] = mapped_column(Text)


class ContractPenaltyRule(TenantMixin, Base):
    """ "KPI hedefin %Y fazla altındaysa bedelden %Z / sabit tutar kesinti". KPI kaldırılınca
    kuralı da kaldırılır (ON DELETE CASCADE)."""

    __tablename__ = "contract_penalty_rules"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        ForeignKeyConstraint(
            ["site_id", "kpi_id"],
            ["contract_kpis.site_id", "contract_kpis.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(enum_check("cut_kind", CutKind), name="cut_kind"),
        CheckConstraint("below_percent >= 0 AND below_percent < 100", name="below_percent"),
        CheckConstraint(
            "cut_value > 0 AND (cut_kind = 'fixed' OR cut_value <= 100)", name="cut_value"
        ),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kpi_id: Mapped[uuid.UUID]
    position: Mapped[int]
    below_percent: Mapped[Decimal] = mapped_column(PERCENT)
    cut_kind: Mapped[str] = mapped_column(Text)
    cut_value: Mapped[Decimal] = mapped_column(MONEY)


class ContractScorecard(TenantMixin, Base):
    """Aylık hizmet karnesi. Taslakta yalnız elle girilen değerler ve yorum saklanır (kaynaklı
    değerler her açılışta yeniden hesaplanır); kesinleşince satırlar anlık görüntüyle donar."""

    __tablename__ = "contract_scorecards"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        UniqueConstraint("site_id", "contract_id", "period"),
        CheckConstraint(enum_check("status", ScorecardStatus), name="status"),
        CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period_format"),
        CheckConstraint(
            "status = 'draft' OR (finalized_at IS NOT NULL AND rows IS NOT NULL)",
            name="finalized_snapshot",
        ),
    )

    contract_id: Mapped[uuid.UUID]
    period: Mapped[str] = mapped_column(Text)  # YYYY-MM
    status: Mapped[str] = mapped_column(Text, default=ScorecardStatus.DRAFT.value)
    manual_values: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    comment: Mapped[str | None] = mapped_column(Text)
    rows: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    total: Mapped[int | None]
    monthly_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    finalized_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    finalized_by: Mapped[str | None] = mapped_column(Text)
    finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ContractPenalty(TenantMixin, Base):
    """Ceza kesintisi — finansal kayıt, **silinmez**; yalnız onaya bağlı durum değişir. Onaylanan
    kesinti o dönemin tedarikçi faturasının beklenen tutarını düşürür (P10 fatura bağlantısı
    satın alma paketi birleşince)."""

    __tablename__ = "contract_penalties"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        tenant_fk("scorecard_id", "contract_scorecards"),
        tenant_fk("approval_request_id", "approval_requests"),
        UniqueConstraint("site_id", "scorecard_id"),
        CheckConstraint(enum_check("status", PenaltyStatus), name="status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status = 'pending' OR decided_at IS NOT NULL", name="decided"),
        Index("ix_contract_penalties_site_status", "site_id", "status"),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(index=True)
    scorecard_id: Mapped[uuid.UUID]
    period: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    reasons: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default=PenaltyStatus.PENDING.value)
    approval_request_id: Mapped[uuid.UUID | None]
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)


class ContractEscalation(TenantMixin, Base):
    """Artış tarihindeki karar (onay / ret). Onayda bedel geçmişine artış tarihinden geçerli yeni
    satır yazılır. Bir tarihte tek etkin karar; geri çekilen talepten sonra yeniden karar."""

    __tablename__ = "contract_escalations"
    __table_args__ = tenant_table_args(
        tenant_fk("contract_id", "contracts"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("status", EscalationStatus), name="status"),
        CheckConstraint("new_amount >= 0 AND old_amount >= 0", name="amounts"),
        Index(
            "uq_contract_escalations_active",
            "site_id",
            "contract_id",
            "due_date",
            unique=True,
            postgresql_where=text("status <> 'withdrawn'"),
        ),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(index=True)
    due_date: Mapped[dt.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    index_period: Mapped[str | None] = mapped_column(Text)
    rate: Mapped[Decimal] = mapped_column(RATE)
    capped_rate: Mapped[Decimal] = mapped_column(RATE)
    old_amount: Mapped[Decimal] = mapped_column(MONEY)
    new_amount: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    approval_request_id: Mapped[uuid.UUID | None]
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    requested_by: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class IndexValue(Base):
    """Endeks değeri (TÜİK yıllık % değişim) — GLOBAL, **değişmez**. Aynı dönemin düzeltmesi yeni
    satırdır; geçerli değer dönemin en son satırıdır (kim, ne zaman girdi saklanır)."""

    __tablename__ = "index_values"
    __table_args__ = (
        CheckConstraint("period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'", name="period_format"),
        CheckConstraint("tufe > -50 AND tufe < 500", name="tufe"),
        CheckConstraint("ufe > -50 AND ufe < 500", name="ufe"),
        Index("ix_index_values_period", "period", "created_at"),
    )

    period: Mapped[str] = mapped_column(Text)
    tufe: Mapped[Decimal] = mapped_column(RATE)
    ufe: Mapped[Decimal] = mapped_column(RATE)
    entered_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    entered_by: Mapped[str] = mapped_column(Text)
