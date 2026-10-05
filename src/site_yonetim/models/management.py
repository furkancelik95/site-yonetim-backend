"""Yönetim paketi — frontend servis istekleri 14–18. KİRACI tabloları.

Hiçbiri silinmez: toplantı iptal edilir, sözleşme arşivlenir, demirbaş kullanım dışı yapılır,
stok hareketi ters hareketle düzeltilir (hareket tablosu değişmez), personele ayrılış
tarihi girilir.
Anket oyları **gizlidir**: oy tablosu denetim kaydına yazılmaz.
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.management import (
    AgendaResult,
    AssetStatus,
    ContractCategory,
    ContractPeriod,
    Employer,
    MeetingKind,
    MeetingStatus,
    PollAudience,
    StockDirection,
)
from site_yonetim.models.finance import MONEY

QUANTITY = Numeric(14, 3)

# --- Toplantı (14) ------------------------------------------------------------------


class Meeting(TenantMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "number"),
        CheckConstraint(enum_check("kind", MeetingKind), name="kind"),
        CheckConstraint(enum_check("status", MeetingStatus), name="status"),
        Index("ix_meetings_site_scheduled", "site_id", "scheduled_at"),
    )

    number: Mapped[int]
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    location: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=MeetingStatus.PLANNED.value)
    attendance_note: Mapped[str | None] = mapped_column(Text)
    held_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by_name: Mapped[str | None] = mapped_column(Text)


class MeetingAgendaItem(TenantMixin, Base):
    """Gündem maddesi; sonuç ve karar tek seferde girilir, sonra değişmez (yasal kayıt)."""

    __tablename__ = "meeting_agenda_items"
    __table_args__ = tenant_table_args(
        tenant_fk("meeting_id", "meetings"),
        UniqueConstraint("meeting_id", "order"),
        CheckConstraint(f"result IS NULL OR {enum_check('result', AgendaResult)}", name="result"),
        CheckConstraint(
            "COALESCE(votes_for, 0) >= 0 AND COALESCE(votes_against, 0) >= 0 "
            "AND COALESCE(votes_abstain, 0) >= 0",
            name="votes_non_negative",
        ),
    )

    meeting_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order: Mapped[int]
    title: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    votes_for: Mapped[int | None]
    votes_against: Mapped[int | None]
    votes_abstain: Mapped[int | None]


# --- Anket (15) ---------------------------------------------------------------------


class Poll(TenantMixin, Base):
    __tablename__ = "polls"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("audience", PollAudience), name="audience"),
        Index("ix_polls_site_ends", "site_id", "ends_on"),
    )

    question: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text, default=PollAudience.ALL.value)
    ends_on: Mapped[dt.date] = mapped_column(Date)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # erken kapanış
    created_by_name: Mapped[str | None] = mapped_column(Text)


class PollOption(TenantMixin, Base):
    __tablename__ = "poll_options"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        UniqueConstraint("poll_id", "order"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order: Mapped[int]
    label: Mapped[str] = mapped_column(Text)


class PollVote(TenantMixin, Base):
    """Bir bağımsız bölüm = bir oy (benzersiz kısıt; eşzamanlı iki istekte de). Oy kişiye değil
    bölüme yazılır, değiştirilemez. Kimin oy verdiği tutulmaz — gizli oy."""

    __tablename__ = "poll_votes"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        tenant_fk("option_id", "poll_options"),
        tenant_fk("unit_id", "units"),
        UniqueConstraint("poll_id", "unit_id"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    option_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]


# --- Sözleşme (16) ------------------------------------------------------------------


class Contract(TenantMixin, Base):
    """Hizmet sözleşmesi. Gider yazmaz (ödeme Giderler'den). Silinmez, arşivlenir."""

    __tablename__ = "contracts"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("category", ContractCategory), name="category"),
        CheckConstraint(f"period IS NULL OR {enum_check('period', ContractPeriod)}", name="period"),
        CheckConstraint("end_date >= start_date", name="dates"),
        CheckConstraint("notice_days BETWEEN 0 AND 365", name="notice_days"),
        CheckConstraint("amount IS NULL OR amount >= 0", name="amount"),
    )

    vendor: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    period: Mapped[str | None] = mapped_column(Text)
    notice_days: Mapped[int] = mapped_column(default=30)
    auto_renew: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str | None] = mapped_column(Text)
    is_archived: Mapped[bool] = mapped_column(default=False)


# --- Demirbaş ve stok (17) ----------------------------------------------------------


class Asset(TenantMixin, Base):
    """Demirbaş. Kod sitede sıralı ve benzersiz (`DB-0001`), değişmez. Silinmez: `retired`."""

    __tablename__ = "assets"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "sequence"),
        CheckConstraint(enum_check("status", AssetStatus), name="status"),
        CheckConstraint("value IS NULL OR value >= 0", name="value"),
    )

    sequence: Mapped[int]
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    acquired_on: Mapped[dt.date | None] = mapped_column(Date)
    value: Mapped[Decimal | None] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(Text, default=AssetStatus.IN_USE.value)
    assignee: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)


class StockItem(TenantMixin, Base):
    """Stok malzemesi. `quantity` hareketle aynı işlemde, satır kilidi altında güncellenir;
    eksiye düşemez (kısıt). Ad site içinde benzersiz (Türkçe harf duyarsız)."""

    __tablename__ = "stock_items"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "name_key"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("min_quantity >= 0", name="min_quantity"),
    )

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    unit_label: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, default=Decimal(0))
    min_quantity: Mapped[Decimal] = mapped_column(QUANTITY, default=Decimal(0))
    location: Mapped[str | None] = mapped_column(Text)


class StockMove(TenantMixin, Base):
    """Stok hareketi — **değişmez** (tetikleyici); yanlışsa ters hareket."""

    __tablename__ = "stock_moves"
    __table_args__ = tenant_table_args(
        tenant_fk("stock_item_id", "stock_items"),
        CheckConstraint(enum_check("direction", StockDirection), name="direction"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_stock_moves_item_created", "site_id", "stock_item_id", "created_at"),
    )

    stock_item_id: Mapped[uuid.UUID]
    direction: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    note: Mapped[str | None] = mapped_column(Text)
    moved_by: Mapped[str | None] = mapped_column(Text)


# --- Personel (18) ------------------------------------------------------------------


class StaffMember(TenantMixin, Base):
    """Site personeli. KVKK veri minimizasyonu: T.C. kimlik no, maaş, bordro, adres, sağlık
    bilgisi **tutulmaz**. Silinmez; ayrılış tarihi girilir (saklama süresi K8)."""

    __tablename__ = "staff_members"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("employer", Employer), name="employer"),
        CheckConstraint(
            "employer <> 'contractor' OR contractor_name IS NOT NULL", name="contractor_name"
        ),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="dates"),
    )

    full_name: Mapped[str] = mapped_column(Text)
    position: Mapped[str] = mapped_column(Text)
    employer: Mapped[str] = mapped_column(Text)
    contractor_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    shift: Mapped[str | None] = mapped_column(Text)
