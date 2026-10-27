"""Rezervasyon — Yazılım Dokümanı §14 (REZ-01..03), servis istekleri 59–61. Hepsi KİRACI tablosu.

Çakışma veritabanında engellenir:
- Her rezervasyon alanın kapasitesindeki bir "yer"i (`seat`, 1..kapasite) tutar. Aynı alanda aynı
  yer aynı başlangıçta iki canlı rezervasyonda olamaz (kısmi benzersiz indeks); aynı bölüm aynı
  alanda aynı başlangıca ikinci canlı rezervasyon açamaz.
- Yazan her işlem önce ortak alan satırını kilitler (`SELECT … FOR UPDATE`): aynı alana eşzamanlı
  istekler sıraya girer; ikincisi birincinin sonucunu görür (kapasite doluysa 409).
- Göç, sunucuda `btree_gist` uzantısı varsa ayrıca `EXCLUDE USING gist` kısıtı kurar (aynı alanda
  aynı yerde zaman aralıkları kesişemez — kural değişikliğiyle kayan saatler dahil).
"""

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.reservations import (
    LIVE_VALUES,
    ApprovalMode,
    BookingChannel,
    ChannelRule,
    FeeType,
    ReservationStatus,
    VerifyMethod,
    WaitlistStatus,
)
from site_yonetim.models.finance import MONEY

_LIVE = "status IN ({})".format(", ".join(f"'{v}'" for v in LIVE_VALUES))


class ReservationRules(TenantMixin, Base):
    """Rezerve edilebilir ortak alanın kuralları (REZ-01). Satırı olmayan alan varsayılanla
    çalışır (`domain/reservations.DEFAULT_RULES`)."""

    __tablename__ = "reservation_rules"
    __table_args__ = tenant_table_args(
        tenant_fk("area_id", "common_areas"),
        UniqueConstraint("site_id", "area_id"),
        CheckConstraint(enum_check("fee_type", FeeType), name="fee_type"),
        CheckConstraint(enum_check("channel", ChannelRule), name="channel"),
        CheckConstraint(enum_check("approval", ApprovalMode), name="approval"),
        CheckConstraint(enum_check("verify", VerifyMethod), name="verify"),
        CheckConstraint("open_to > open_from", name="hours"),
        CheckConstraint("slot_minutes IN (30, 60, 90, 120, 180, 240)", name="slot_minutes"),
        CheckConstraint("capacity BETWEEN 1 AND 200", name="capacity"),
        CheckConstraint("fee_amount >= 0", name="fee_not_negative"),
        CheckConstraint("NOT noshow_enabled OR verify <> 'none'", name="noshow_needs_verify"),
    )

    area_id: Mapped[uuid.UUID]
    enabled: Mapped[bool] = mapped_column(default=True)
    open_from: Mapped[dt.time] = mapped_column(Time)
    open_to: Mapped[dt.time] = mapped_column(Time)
    slot_minutes: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    max_people: Mapped[int] = mapped_column(Integer)
    max_guests: Mapped[int] = mapped_column(Integer)
    weekly_limit: Mapped[int] = mapped_column(Integer)
    cancel_hours: Mapped[int] = mapped_column(Integer)
    late_cancel_refund: Mapped[bool] = mapped_column(default=False)
    fee_type: Mapped[str] = mapped_column(Text)
    fee_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    channel: Mapped[str] = mapped_column(Text)
    approval: Mapped[str] = mapped_column(Text)
    verify: Mapped[str] = mapped_column(Text)
    waitlist: Mapped[bool] = mapped_column(default=True)
    waitlist_minutes: Mapped[int] = mapped_column(Integer)
    noshow_enabled: Mapped[bool] = mapped_column(default=False)
    noshow_months: Mapped[int] = mapped_column(Integer)
    noshow_count: Mapped[int] = mapped_column(Integer)
    noshow_block_days: Mapped[int] = mapped_column(Integer)


class ReservationSettings(TenantMixin, Base):
    """Site geneli rezervasyon ayarı — tek satır. Şimdilik yalnız K11 (borçlu bölüme kısıt).

    GEÇİCİ: K11 parametresi finans parametrelerine (`finance_parameter_versions`, servis isteği 33)
    aittir; o paket gelince oraya taşınır."""

    __tablename__ = "reservation_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        CheckConstraint("debt_min_amount IS NULL OR debt_min_amount > 0", name="debt_amount"),
        CheckConstraint("debt_min_days IS NULL OR debt_min_days >= 1", name="debt_days"),
        CheckConstraint(
            "NOT debt_block_enabled OR (debt_min_amount IS NOT NULL AND debt_min_days IS NOT NULL)",
            name="debt_thresholds",
        ),
    )

    debt_block_enabled: Mapped[bool] = mapped_column(default=False)
    debt_min_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    debt_min_days: Mapped[int | None] = mapped_column(Integer)


class Reservation(TenantMixin, Base):
    """Rezervasyon (REZ-02/03). Silinmez; iptal/ret/gelinmedi durumuyla kalır.

    Ücret onayda cari deftere (`reservation` kaynağı) borç yazılır (`charge_entry_id`); son iptal
    süresinden önce iptalde ters kayıt (`refund_entry_id`)."""

    __tablename__ = "reservations"
    __table_args__ = tenant_table_args(
        tenant_fk("area_id", "common_areas"),
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        tenant_fk("account_id", "ledger_accounts"),
        tenant_fk("charge_entry_id", "ledger_entries"),
        tenant_fk("refund_entry_id", "ledger_entries"),
        tenant_fk("approval_request_id", "approval_requests"),
        CheckConstraint(enum_check("status", ReservationStatus), name="status"),
        CheckConstraint(enum_check("channel", BookingChannel), name="channel"),
        CheckConstraint("ends_at > starts_at", name="period"),
        CheckConstraint("seat >= 1", name="seat"),
        CheckConstraint("people >= 1 AND guests >= 0", name="headcount"),
        CheckConstraint("fee >= 0", name="fee_not_negative"),
        CheckConstraint("check_in_code ~ '^[0-9]{6}$'", name="code_format"),
        CheckConstraint(
            "status <> 'rejected' OR reject_reason IS NOT NULL", name="reject_reason_required"
        ),
        Index("ix_reservations_day", "site_id", "date", "area_id"),
        Index("ix_reservations_unit", "site_id", "unit_id", "area_id", "date"),
        Index("ix_reservations_status", "site_id", "status", "ends_at"),
        Index(
            "uq_reservations_seat",
            "site_id",
            "area_id",
            "seat",
            "starts_at",
            unique=True,
            postgresql_where=text(_LIVE),
        ),
        Index(
            "uq_reservations_unit_slot",
            "site_id",
            "area_id",
            "unit_id",
            "starts_at",
            unique=True,
            postgresql_where=text(_LIVE),
        ),
        Index(
            "uq_reservations_code",
            "site_id",
            "date",
            "check_in_code",
            unique=True,
            postgresql_where=text(_LIVE),
        ),
    )

    area_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID | None]  # sakin açtıysa o; personel açtıysa boş
    account_id: Mapped[uuid.UUID | None]  # ücretin yazılacağı hesap (açılışta belirlenir)
    person_name: Mapped[str] = mapped_column(Text)
    date: Mapped[dt.date] = mapped_column(Date)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    seat: Mapped[int] = mapped_column(SmallInteger)
    people: Mapped[int] = mapped_column(Integer)
    guests: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    check_in_code: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    created_by_name: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    charge_entry_id: Mapped[uuid.UUID | None]
    refund_entry_id: Mapped[uuid.UUID | None]
    approval_request_id: Mapped[uuid.UUID | None]
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancelled_late: Mapped[bool] = mapped_column(default=False)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_by_name: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ReservationWaitlist(TenantMixin, Base):
    """Bekleme listesi kaydı (REZ-02 iptal ve bekleme akışı). Teklif gidince yer `offered_until`'e
    kadar ona ayrılır; kabul edilmezse zamanlanmış iş sıradakine geçirir."""

    __tablename__ = "reservation_waitlist"
    __table_args__ = tenant_table_args(
        tenant_fk("area_id", "common_areas"),
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        tenant_fk("reservation_id", "reservations"),
        CheckConstraint(enum_check("status", WaitlistStatus), name="status"),
        CheckConstraint("ends_at > starts_at", name="period"),
        CheckConstraint("status <> 'offered' OR offered_until IS NOT NULL", name="offer_until"),
        Index("ix_reservation_waitlist_slot", "site_id", "area_id", "starts_at", "status"),
        Index(
            "uq_reservation_waitlist_open",
            "site_id",
            "area_id",
            "unit_id",
            "starts_at",
            unique=True,
            postgresql_where=text("status IN ('offered', 'waiting')"),
        ),
    )

    area_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]
    person_id: Mapped[uuid.UUID | None]
    person_name: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    date: Mapped[dt.date] = mapped_column(Date)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    people: Mapped[int] = mapped_column(Integer)
    guests: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    offered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    offered_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reservation_id: Mapped[uuid.UUID | None]
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
