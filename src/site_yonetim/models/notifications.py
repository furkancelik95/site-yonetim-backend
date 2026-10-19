"""Bildirim merkezi — Yazılım Dokümanı PLT-05 (docs/03).

- `notifications` [K]: uygulama içi bildirim; siteye aittir (RLS), alıcıya yazılır. Aynı olay
  aynı kişiye ikinci kez bildirim üretmez: `(site, kullanıcı, tekil anahtar)` benzersiz.
- `notification_preferences` [G]: kişi × kategori × dış kanal; yalnız varsayılandan farklı
  tercih saklanır (`domain/notifications.py`).
- `push_tokens` [G]: mobil/tarayıcı push jetonu — gönderim K5'te; jeton şimdiden kaydedilir.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_table_args
from site_yonetim.domain.notifications import Channel, NotificationCategory, PushPlatform


class Notification(TenantMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = tenant_table_args(
        CheckConstraint(enum_check("category", NotificationCategory), name="category"),
        UniqueConstraint("site_id", "user_id", "dedupe_key"),
        Index("ix_notifications_site_user_created", "site_id", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    category: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # ör. "approval.requested" — frontend ikonu
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    link_entity: Mapped[str | None] = mapped_column(Text)  # tıklayınca gidilecek kayıt türü
    link_id: Mapped[uuid.UUID | None]
    dedupe_key: Mapped[str] = mapped_column(Text)  # olay kimliği + şablon; alıcı satırda
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificationPreference(Base):
    """[G] Varsayılandan farklı kanal tercihi (kişi × kategori × kanal)."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        CheckConstraint(enum_check("category", NotificationCategory), name="category"),
        CheckConstraint(enum_check("channel", Channel), name="channel"),
        UniqueConstraint("user_id", "category", "channel"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool]


class PushToken(Base):
    """[G] Cihazın push jetonu; aynı jeton başka kullanıcıyla gelirse ona taşınır (cihaz el
    değiştirdi / başka hesapla giriş)."""

    __tablename__ = "push_tokens"
    __table_args__ = (CheckConstraint(enum_check("platform", PushPlatform), name="platform"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(Text, unique=True)
    platform: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
