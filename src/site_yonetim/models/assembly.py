"""Genel kurul (Yazılım Dokümanı §17, TOP-01..04; servis istekleri 75–77). KİRACI tabloları.

- `assembly_settings`: karar türleri, nisap, vekâlet sınırı ve hukuki parametreler (tek satır).
- `assemblies`: toplantı başına genel kurul dosyası (format, uyarı onayı, çağrı, tutanak taslağı).
- `assembly_items`: maddenin karar türü, oylama durumu ve kapanıştaki sonuç anlık görüntüsü.
- `assembly_roster`: hazirunda ilk kayıtla sabitlenen malik listesi (değişmez).
- `assembly_attendance`: hazirun (kendisi / fiziki vekil); açık oylama yokken değişir, denetimde.
- `assembly_ballots`: oy olayları, **değişmez** (oy, zaman, giren, kanal); son olay geçerli oy.
- `assembly_minutes`: kesinleşen tutanak — **değişmez** anlık görüntü.
- `decision_book_entries`: karar defteri — site bazında artan sıra numarası, **değişmez**.
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.assembly import (
    PROXY_LIMIT_MAX,
    PROXY_LIMIT_MIN,
    AssemblyFormat,
    AttendanceMode,
    Ballot,
    BallotChannel,
    PersonRule,
    VoteStatus,
)


class AssemblySettings(TenantMixin, Base):
    __tablename__ = "assembly_settings"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id"),
        tenant_fk("legal_basis_document_id", "documents"),
        CheckConstraint(
            f"proxy_limit BETWEEN {PROXY_LIMIT_MIN} AND {PROXY_LIMIT_MAX}", name="proxy_limit"
        ),
        CheckConstraint(enum_check("person_rule", PersonRule), name="person_rule"),
        CheckConstraint("notice_days BETWEEN 0 AND 90", name="notice_days"),
        CheckConstraint("second_call_days BETWEEN 0 AND 90", name="second_call_days"),
    )

    decision_types: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    quorum: Mapped[dict[str, Any]] = mapped_column(JSONB)
    second_call_quorum: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # NULL: aranmaz
    proxy_limit: Mapped[int]
    person_rule: Mapped[str] = mapped_column(Text)
    notice_days: Mapped[int]
    second_call_days: Mapped[int]
    read_ack_proof: Mapped[bool] = mapped_column(default=True)
    remote_voting: Mapped[bool] = mapped_column(default=False)  # B18 — varsayılan kapalı
    digital_proxy: Mapped[bool] = mapped_column(default=False)  # B18 — varsayılan kapalı
    legal_basis_document_id: Mapped[uuid.UUID | None]
    updated_by_name: Mapped[str | None] = mapped_column(Text)


class Assembly(TenantMixin, Base):
    __tablename__ = "assemblies"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "meeting_id"),
        tenant_fk("meeting_id", "meetings"),
        tenant_fk("invitation_announcement_id", "announcements"),
        tenant_fk("signed_document_id", "documents"),
        CheckConstraint(enum_check("format", AssemblyFormat), name="format"),
        CheckConstraint(
            "invitation_ack_hours IS NULL OR invitation_ack_hours BETWEEN 1 AND 720",
            name="ack_hours",
        ),
    )

    meeting_id: Mapped[uuid.UUID]
    format: Mapped[str] = mapped_column(Text)
    second_call: Mapped[bool] = mapped_column(default=False)
    legal_ack_by_name: Mapped[str | None] = mapped_column(Text)
    legal_ack_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    invitation_announcement_id: Mapped[uuid.UUID | None]
    invitation_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    invitation_by_name: Mapped[str | None] = mapped_column(Text)
    invitation_ack_hours: Mapped[int | None]
    roster_frozen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    minutes_opening: Mapped[str | None] = mapped_column(Text)
    minutes_finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    minutes_finalized_by: Mapped[str | None] = mapped_column(Text)
    signed_document_id: Mapped[uuid.UUID | None]


class AssemblyItem(TenantMixin, Base):
    __tablename__ = "assembly_items"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "agenda_item_id"),
        tenant_fk("assembly_id", "assemblies"),
        tenant_fk("agenda_item_id", "meeting_agenda_items"),
        CheckConstraint(enum_check("status", VoteStatus), name="status"),
        CheckConstraint("(status = 'closed') = (result IS NOT NULL)", name="result_when_closed"),
    )

    assembly_id: Mapped[uuid.UUID] = mapped_column(index=True)
    agenda_item_id: Mapped[uuid.UUID]
    decision_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=VoteStatus.NONE.value)
    opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    opened_by_name: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_name: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # kapanışta anlık görüntü
    minutes_note: Mapped[str | None] = mapped_column(Text)
    decision_text: Mapped[str | None] = mapped_column(Text)


class AssemblyRosterEntry(TenantMixin, Base):
    __tablename__ = "assembly_roster"
    __table_args__ = tenant_table_args(
        UniqueConstraint("assembly_id", "unit_id"),
        tenant_fk("assembly_id", "assemblies"),
        tenant_fk("unit_id", "units"),
        CheckConstraint(
            "share_numerator >= 0 AND share_denominator > 0", name="share_non_negative"
        ),
    )

    assembly_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID]
    unit_name: Mapped[str] = mapped_column(Text)
    block_name: Mapped[str] = mapped_column(Text)
    owner_person_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid))
    owner_names: Mapped[list[str]] = mapped_column(ARRAY(Text))
    person_key: Mapped[str] = mapped_column(Text)
    share_numerator: Mapped[int]
    share_denominator: Mapped[int]
    sort_order: Mapped[int]


class AssemblyAttendance(TenantMixin, Base):
    __tablename__ = "assembly_attendance"
    __table_args__ = tenant_table_args(
        UniqueConstraint("assembly_id", "unit_id"),
        tenant_fk("assembly_id", "assemblies"),
        tenant_fk("unit_id", "units"),
        CheckConstraint(enum_check("mode", AttendanceMode), name="mode"),
        CheckConstraint(
            "(mode = 'proxy') = (proxy_name IS NOT NULL AND proxy_key IS NOT NULL)",
            name="proxy_name_when_proxy",
        ),
        Index("ix_assembly_attendance_proxy", "assembly_id", "proxy_key"),
    )

    assembly_id: Mapped[uuid.UUID] = mapped_column(index=True)
    unit_id: Mapped[uuid.UUID]
    mode: Mapped[str] = mapped_column(Text)
    proxy_name: Mapped[str | None] = mapped_column(Text)
    proxy_key: Mapped[str | None] = mapped_column(Text)
    recorded_by_name: Mapped[str | None] = mapped_column(Text)


class AssemblyBallot(TenantMixin, Base):
    """Oy olayı (değişmez). `choice` NULL: o bölümün oyu silindi (salonda düzeltme)."""

    __tablename__ = "assembly_ballots"
    __table_args__ = tenant_table_args(
        tenant_fk("assembly_item_id", "assembly_items"),
        tenant_fk("unit_id", "units"),
        CheckConstraint(f"choice IS NULL OR {enum_check('choice', Ballot)}", name="choice"),
        CheckConstraint(enum_check("channel", BallotChannel), name="channel"),
        Index("ix_assembly_ballots_item_unit", "assembly_item_id", "unit_id"),
    )

    assembly_item_id: Mapped[uuid.UUID]
    unit_id: Mapped[uuid.UUID]
    choice: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text, default=BallotChannel.HALL.value)
    entered_by_user_id: Mapped[uuid.UUID | None]
    entered_by_name: Mapped[str | None] = mapped_column(Text)
    device: Mapped[str | None] = mapped_column(Text)  # uygulamadan oyda cihaz bilgisi (B18)
    sms_verified: Mapped[bool] = mapped_column(default=False)  # uygulamadan oyda SMS kodu (B18)


class AssemblyMinutes(TenantMixin, Base):
    """Kesinleşen tutanak — değişmez anlık görüntü (hazirun, maddeler, sayılar, metinler)."""

    __tablename__ = "assembly_minutes"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "assembly_id"),
        tenant_fk("assembly_id", "assemblies"),
    )

    assembly_id: Mapped[uuid.UUID]
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    finalized_by_name: Mapped[str] = mapped_column(Text)


class DecisionBookEntry(TenantMixin, Base):
    """Karar defteri satırı — site bazında artan sıra numarası; değişmez."""

    __tablename__ = "decision_book_entries"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "number"),
        UniqueConstraint("site_id", "agenda_item_id"),
        tenant_fk("meeting_id", "meetings"),
        tenant_fk("agenda_item_id", "meeting_agenda_items"),
        CheckConstraint("number > 0", name="number_positive"),
    )

    number: Mapped[int]
    meeting_id: Mapped[uuid.UUID] = mapped_column(index=True)
    agenda_item_id: Mapped[uuid.UUID]
    decided_on: Mapped[dt.date] = mapped_column(Date)
    meeting_title: Mapped[str] = mapped_column(Text)
    item_title: Mapped[str] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    type_name: Mapped[str] = mapped_column(Text)
