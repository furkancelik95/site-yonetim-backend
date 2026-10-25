"""Talep ve duyuru — docs/03 §9. Hepsi KİRACI tablosu.

`request_events` değişmezdir (göçteki tetikleyici UPDATE/DELETE'i reddeder): talebin geçmişi
sonradan yazılamaz.
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.operations import (
    Audience,
    Channel,
    Importance,
    RequestEventKind,
    RequestPriority,
    RequestStatus,
)

TITLE_MIN, TITLE_MAX = 3, 120


class Request(TenantMixin, Base):
    """Talep / arıza / şikâyet. Numara site içinde artan (1, 2, 3 …)."""

    __tablename__ = "requests"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("reported_by_person_id", "persons"),
        tenant_fk("department_id", "request_departments"),
        tenant_fk("outage_id", "outages"),
        # Site tanımlı kategori (docs/12 D-26): anahtar değişmez, talep anahtarla bağlanır.
        ForeignKeyConstraint(
            ["site_id", "category"], ["request_categories.site_id", "request_categories.key"]
        ),
        UniqueConstraint("site_id", "number"),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint(
            f"char_length(title) BETWEEN {TITLE_MIN} AND {TITLE_MAX}", name="title_length"
        ),
        CheckConstraint(enum_check("priority", RequestPriority), name="priority"),
        CheckConstraint(enum_check("status", RequestStatus), name="status"),
        CheckConstraint(
            "status NOT IN ('resolved', 'closed') OR resolution IS NOT NULL",
            name="resolution_required",
        ),
        Index("ix_requests_site_status", "site_id", "status"),
        Index("ix_requests_reporter", "site_id", "reported_by_person_id"),
        Index("ix_requests_outage", "site_id", "outage_id"),
    )

    number: Mapped[int]
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)  # request_categories.key
    priority: Mapped[str] = mapped_column(Text, default=RequestPriority.NORMAL.value)
    status: Mapped[str] = mapped_column(Text, default=RequestStatus.OPEN.value)
    reported_by_person_id: Mapped[uuid.UUID | None]
    unit_id: Mapped[uuid.UUID | None]  # ortak alan talebinde boş
    department_id: Mapped[uuid.UUID | None]  # servis isteği 11
    location: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_by_name: Mapped[str | None] = mapped_column(Text)  # talebi açan kullanıcı
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # kapandı/iptal
    # Çözüldü talebe sakin yanıt vermezse bu anda kendiliğinden kapanır (§12.3, K18).
    auto_close_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # SLA kural anlık görüntüsü (HIZ-03 adım 4): açık talep açıldığı kuralla devam eder.
    sla_hour_type: Mapped[str | None] = mapped_column(Text)
    sla_target_hours: Mapped[int | None]
    sla_calendar: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    sla_warn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # %80
    sla_warned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    sla_breached_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    outage_id: Mapped[uuid.UUID | None]  # ana olay (servis isteği 49)


class RequestEvent(TenantMixin, Base):
    """Talebin geçmişi — **değişmez**."""

    __tablename__ = "request_events"
    __table_args__ = tenant_table_args(
        tenant_fk("request_id", "requests"),
        CheckConstraint(enum_check("kind", RequestEventKind), name="kind"),
        CheckConstraint(
            f"new_status IS NULL OR {enum_check('new_status', RequestStatus)}", name="new_status"
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    actor_name: Mapped[str | None] = mapped_column(Text)
    new_status: Mapped[str | None] = mapped_column(Text)


class Announcement(TenantMixin, Base):
    __tablename__ = "announcements"
    __table_args__ = tenant_table_args(
        CheckConstraint(
            f"char_length(title) BETWEEN {TITLE_MIN} AND {TITLE_MAX}", name="title_length"
        ),
        CheckConstraint(enum_check("importance", Importance), name="importance"),
        CheckConstraint(enum_check("audience", Audience), name="audience"),
        Index("ix_announcements_site_published", "site_id", "published_at"),
    )

    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    importance: Mapped[str] = mapped_column(Text, default=Importance.NORMAL.value)
    audience: Mapped[str] = mapped_column(Text, default=Audience.ALL_RESIDENTS.value)
    audience_block_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(Uuid))
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))  # NULL=taslak
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    published_by: Mapped[str | None] = mapped_column(Text)
    is_pinned: Mapped[bool] = mapped_column(default=False)


class AnnouncementDelivery(TenantMixin, Base):
    """Kime, hangi kanaldan ulaştı. Gerçek gönderim yok (docs/12 K5): yalnız uygulama içi
    teslim anında `sent_at` alır; diğer kanallar bekler."""

    __tablename__ = "announcement_deliveries"
    __table_args__ = tenant_table_args(
        tenant_fk("announcement_id", "announcements"),
        tenant_fk("person_id", "persons"),
        UniqueConstraint("announcement_id", "person_id", "channel"),
        CheckConstraint(enum_check("channel", Channel), name="channel"),
        Index("ix_announcement_deliveries_person", "site_id", "person_id"),
    )

    announcement_id: Mapped[uuid.UUID] = mapped_column(index=True)
    person_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class RequestDepartment(TenantMixin, Base):
    """Talep departmanı (servis isteği 11). Ad site içinde benzersiz (Türkçe harf ve büyük/küçük
    duyarsız `name_key`). Silinmez; pasifleştirilir — pasife yeni talep atanmaz."""

    __tablename__ = "request_departments"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id", "name_key"))

    name: Mapped[str] = mapped_column(Text)
    name_key: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)
