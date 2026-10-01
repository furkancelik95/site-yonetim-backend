"""Talep ve duyuru — docs/03 §9. Hepsi KİRACI tablosu.

`request_events` değişmezdir (göçteki tetikleyici UPDATE/DELETE'i reddeder): talebin geçmişi
sonradan yazılamaz.
"""

import datetime as dt
import uuid

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
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.operations import (
    Audience,
    Channel,
    Importance,
    RequestCategory,
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
        UniqueConstraint("site_id", "number"),
        CheckConstraint("number > 0", name="number_positive"),
        CheckConstraint(
            f"char_length(title) BETWEEN {TITLE_MIN} AND {TITLE_MAX}", name="title_length"
        ),
        CheckConstraint(enum_check("category", RequestCategory), name="category"),
        CheckConstraint(enum_check("priority", RequestPriority), name="priority"),
        CheckConstraint(enum_check("status", RequestStatus), name="status"),
        CheckConstraint(
            "status NOT IN ('resolved', 'closed') OR resolution IS NOT NULL",
            name="resolution_required",
        ),
        Index("ix_requests_site_status", "site_id", "status"),
        Index("ix_requests_reporter", "site_id", "reported_by_person_id"),
    )

    number: Mapped[int]
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, default=RequestCategory.OTHER.value)
    priority: Mapped[str] = mapped_column(Text, default=RequestPriority.NORMAL.value)
    status: Mapped[str] = mapped_column(Text, default=RequestStatus.OPEN.value)
    reported_by_person_id: Mapped[uuid.UUID | None]
    unit_id: Mapped[uuid.UUID | None]  # ortak alan talebinde boş
    location: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    created_by_name: Mapped[str | None] = mapped_column(Text)  # talebi açan kullanıcı


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
