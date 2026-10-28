"""Platform destek — Yazılım Dokümanı PYN-04, PYN-06, PLT-12 (servis istekleri 82–84; K12).

KİRACI tabloları (site verisi; platform tarafı `all_sites_scope()` ile bilinçli okur):
- `support_tickets`: destek bileti; numara platform genelinde tek (`support_ticket_number_seq`,
  "DST-0001"). SLA hedefleri açılışta bilete yazılır (paket değişse de açık bilet etkilenmez).
- `support_ticket_messages`: yazışma. **Değişmez** (forbid_history_change).
- `support_sessions`: destek oturumu talebi → tesis yöneticisi onayı → süreli, salt okunur
  erişim. Bir tesiste aynı anda tek bekleyen/aktif oturum (kısmi benzersiz indeks).
- `view_as_sessions`: sakin olarak görüntüleme (en çok 30 dk, gerekçeli).

GLOBAL tablolar (platform içeriği, tüm tesisler için ortak):
- `knowledge_articles`, `knowledge_feedback` (kullanıcı başına makale başına tek oy).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
from site_yonetim.domain.support import (
    ArticleAudience,
    MessageSide,
    SupportSessionStatus,
    SupportTier,
    TicketCategory,
    TicketPriority,
    TicketStatus,
    ViewAsStatus,
)


class SupportTicket(TenantMixin, Base):
    __tablename__ = "support_tickets"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("category", TicketCategory), name="category"),
        CheckConstraint(enum_check("priority", TicketPriority), name="priority"),
        CheckConstraint(enum_check("status", TicketStatus), name="status"),
        CheckConstraint(enum_check("support_tier", SupportTier), name="support_tier"),
        CheckConstraint("char_length(title) BETWEEN 5 AND 120", name="title_length"),
        UniqueConstraint("number"),
        Index("ix_support_tickets_site_created", "site_id", "created_at"),
        Index("ix_support_tickets_status", "status"),
    )

    number: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default=TicketStatus.OPEN.value)
    title: Mapped[str] = mapped_column(Text)
    # Ekran bağlamı (otomatik): ekran yolu, uygulama sürümü, tarayıcı, iz kimliği (istek kimliği)
    context_path: Mapped[str] = mapped_column(Text, default="")
    app_version: Mapped[str] = mapped_column(Text, default="")
    user_agent: Mapped[str] = mapped_column(Text, default="")
    correlation_id: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_by_name: Mapped[str] = mapped_column(Text)
    # Açılıştaki paket destek seviyesi ve SLA hedefleri (anlık görüntü)
    support_tier: Mapped[str] = mapped_column(Text)
    first_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolve_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    on_call: Mapped[bool] = mapped_column(Boolean, default=False)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SLA işi (15 dakikada bir) aşımı ilk gördüğü an — raporlama
    first_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolve_breached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupportTicketMessage(TenantMixin, Base):
    __tablename__ = "support_ticket_messages"
    __table_args__ = tenant_table_args(
        tenant_fk("ticket_id", "support_tickets"),
        CheckConstraint(enum_check("side", MessageSide), name="side"),
        Index("ix_support_ticket_messages_ticket", "ticket_id", "created_at"),
    )

    ticket_id: Mapped[uuid.UUID]
    side: Mapped[str] = mapped_column(Text)
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    author_name: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)


class SupportSession(TenantMixin, Base):
    __tablename__ = "support_sessions"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("status", SupportSessionStatus), name="status"),
        CheckConstraint("hours BETWEEN 1 AND 24", name="hours"),
        CheckConstraint("status <> 'active' OR ends_at IS NOT NULL", name="active_ends"),
        Index(
            "uq_support_sessions_open",
            "site_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'active')"),
        ),
        Index("ix_support_sessions_status_ends", "status", "ends_at"),
    )

    reason: Mapped[str] = mapped_column(Text)
    hours: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, default=SupportSessionStatus.PENDING.value)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    requested_by_name: Mapped[str] = mapped_column(Text)
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by_name: Mapped[str | None] = mapped_column(Text)  # süre dolmadan bitiren


class ViewAsSession(TenantMixin, Base):
    __tablename__ = "view_as_sessions"
    __table_args__ = tenant_table_args(
        tenant_fk("person_id", "persons"),
        tenant_fk("support_session_id", "support_sessions"),
        CheckConstraint(enum_check("status", ViewAsStatus), name="status"),
        Index("ix_view_as_sessions_viewer", "viewer_id", "status"),
        Index("ix_view_as_sessions_status_ends", "status", "ends_at"),
    )

    viewer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    viewer_name: Mapped[str] = mapped_column(Text)
    person_id: Mapped[uuid.UUID]
    reason: Mapped[str] = mapped_column(Text)
    support_session_id: Mapped[uuid.UUID | None]  # platform destek ekibi görüntülüyorsa
    status: Mapped[str] = mapped_column(Text, default=ViewAsStatus.ACTIVE.value)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resident_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"
    __table_args__ = (
        CheckConstraint(enum_check("audience", ArticleAudience), name="audience"),
        CheckConstraint("char_length(title) BETWEEN 5 AND 120", name="title_length"),
        CheckConstraint("char_length(body) >= 30", name="body_length"),
    )

    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text)
    screens: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    updated_by_name: Mapped[str] = mapped_column(Text)


class KnowledgeFeedback(Base):
    __tablename__ = "knowledge_feedback"
    __table_args__ = (UniqueConstraint("article_id", "user_id"),)

    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_articles.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    helpful: Mapped[bool] = mapped_column(Boolean)
