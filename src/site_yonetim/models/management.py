"""Yönetim paketi — frontend servis istekleri 14–18. KİRACI tabloları.

Hiçbiri silinmez: toplantı iptal edilir, sözleşme arşivlenir, demirbaş kullanım dışı yapılır,
stok hareketi ters hareketle düzeltilir (hareket tablosu değişmez), personele ayrılış
tarihi girilir.
Anket yanıtları denetim kaydına yazılmaz (gizlilik anket bazında, K31).
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
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.contract_performance import EscalationKind, EscalationPeriod
from site_yonetim.domain.management import (
    AgendaResult,
    AssetStatus,
    ContractCategory,
    ContractPeriod,
    Employer,
    MeetingKind,
    MeetingStatus,
    PollAudience,
    PollQuestionKind,
    StockDirection,
)
from site_yonetim.models.finance import MONEY

QUANTITY = Numeric(14, 3)
RATE = Numeric(6, 2)  # yüzde oran (%0,01–200)

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
    """Anket (ILT-03). `question` anketin başlığıdır; sorular `poll_questions`'ta (eski tek
    soruluk anketler göçte tek "tek seçim" sorusuna çevrildi). Gizlilik anket bazında (K31):
    `is_anonymous` → kim yanıtladı tutulmaz; `one_per_unit` → bölüm başına tek yanıt."""

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
    is_anonymous: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    one_per_unit: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    block_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))  # boş = tüm site


class PollQuestion(TenantMixin, Base):
    __tablename__ = "poll_questions"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        UniqueConstraint("poll_id", "order"),
        CheckConstraint(enum_check("kind", PollQuestionKind), name="kind"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    required: Mapped[bool] = mapped_column(default=True)


class PollOption(TenantMixin, Base):
    __tablename__ = "poll_options"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        tenant_fk("question_id", "poll_questions"),
        UniqueConstraint("question_id", "order"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    question_id: Mapped[uuid.UUID] = mapped_column(index=True)
    order: Mapped[int]
    label: Mapped[str] = mapped_column(Text)


class PollBallot(TenantMixin, Base):
    """Bir yanıt (oy pusulası); değiştirilemez. `one_per_unit` ankette bölüme yazılır
    (`(anket, bölüm)` benzersiz — eşzamanlı iki istekte de). İsimli ankette `person_id` tutulur
    (kişisel veri; isimli dökümü yalnız yetkili görür). Anonim + kişi başına ankette pusula
    kimseye bağlanmaz; tekrar oy `poll_participants` ile engellenir. Denetim kaydına yazılmaz."""

    __tablename__ = "poll_ballots"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        Index(
            "uq_poll_ballots_unit",
            "poll_id",
            "unit_id",
            unique=True,
            postgresql_where=text("unit_id IS NOT NULL"),
        ),
        Index(
            "uq_poll_ballots_person",
            "poll_id",
            "person_id",
            unique=True,
            postgresql_where=text("unit_id IS NULL AND person_id IS NOT NULL"),
        ),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID | None]
    person_id: Mapped[uuid.UUID | None]


class PollAnswer(TenantMixin, Base):
    """Pusuladaki yanıt: seçim (çoklu seçimde seçenek başına bir satır), puan ya da metin."""

    __tablename__ = "poll_answers"
    __table_args__ = tenant_table_args(
        tenant_fk("ballot_id", "poll_ballots"),
        tenant_fk("poll_id", "polls"),
        tenant_fk("question_id", "poll_questions"),
        tenant_fk("option_id", "poll_options"),
        UniqueConstraint("ballot_id", "question_id", "option_id"),
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating"),
        Index("ix_poll_answers_poll_question", "site_id", "poll_id", "question_id"),
    )

    ballot_id: Mapped[uuid.UUID] = mapped_column(index=True)
    poll_id: Mapped[uuid.UUID]
    question_id: Mapped[uuid.UUID]
    option_id: Mapped[uuid.UUID | None]
    rating: Mapped[int | None]
    text: Mapped[str | None] = mapped_column(Text)


class PollParticipant(TenantMixin, Base):
    """Anonim + kişi başına ankette kimin katıldığı (neye oy verdiği değil) — tekrar oyu önler."""

    __tablename__ = "poll_participants"
    __table_args__ = tenant_table_args(
        tenant_fk("poll_id", "polls"),
        tenant_fk("person_id", "persons"),
        UniqueConstraint("poll_id", "person_id"),
    )

    poll_id: Mapped[uuid.UUID] = mapped_column(index=True)
    person_id: Mapped[uuid.UUID]


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
        CheckConstraint(enum_check("escalation_kind", EscalationKind), name="escalation_kind"),
        CheckConstraint(
            enum_check("escalation_period", EscalationPeriod), name="escalation_period"
        ),
        CheckConstraint(
            "escalation_fixed_rate IS NULL OR escalation_fixed_rate BETWEEN 0.01 AND 200",
            name="escalation_fixed_rate",
        ),
        CheckConstraint(
            "escalation_cap IS NULL OR escalation_cap BETWEEN 0.01 AND 200", name="escalation_cap"
        ),
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
    # Sözleşme performansı (servis istekleri 68–70). `amount` en son girilen bedeldir; hangi
    # tarihte hangi bedelin geçerli olduğu `contract_amounts` geçmişindedir (effective_from).
    # Tedarikçi kartı (P10 `vendors`) henüz main'de değil: FK satın alma paketiyle eklenecek.
    vendor_id: Mapped[uuid.UUID | None]
    escalation_kind: Mapped[str] = mapped_column(
        Text, default=EscalationKind.NONE.value, server_default=EscalationKind.NONE.value
    )
    escalation_fixed_rate: Mapped[Decimal | None] = mapped_column(RATE)
    escalation_period: Mapped[str] = mapped_column(
        Text, default=EscalationPeriod.YEARLY.value, server_default=EscalationPeriod.YEARLY.value
    )
    escalation_cap: Mapped[Decimal | None] = mapped_column(RATE)


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
        # IK-01: bordro sicil no site içinde tekil; bir giriş hesabı tek personel kartına (K29)
        Index(
            "uq_staff_members_site_payroll_no",
            "site_id",
            "payroll_no",
            unique=True,
            postgresql_where=text("payroll_no IS NOT NULL"),
        ),
        Index(
            "uq_staff_members_site_user",
            "site_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    full_name: Mapped[str] = mapped_column(Text)
    position: Mapped[str] = mapped_column(Text)
    employer: Mapped[str] = mapped_column(Text)
    contractor_name: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    shift: Mapped[str | None] = mapped_column(Text)
    # IK-01: bordro sistemindeki sicil (T.C. kimlik no tutulmaz). K29: bağlı giriş hesabı —
    # ayrılınca site üyeliği pasifleşir (`access_closed_at`).
    payroll_no: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    access_closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
