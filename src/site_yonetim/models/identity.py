"""Kimlik ve üyelik — docs/03-veri-modeli.md §2."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_table_args
from site_yonetim.domain.access import OrganizationRole, SiteRole, UserKind


class User(Base):
    """[G] Kullanıcı. Parola yalnız argon2id hash'i olarak saklanır."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(enum_check("kind", UserKind), name="kind"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count"),
    )

    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default=UserKind.STAFF.value)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_platform_admin: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_login_count: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrganizationMembership(Base):
    """[G] Yönetim şirketi üyeliği → şirketin bütün sitelerine türetilmiş erişim."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        CheckConstraint(enum_check("role", OrganizationRole), name="role"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)


class SiteMembership(TenantMixin, Base):
    """[K] Açık site üyeliği. Türetilmiş erişimi ezer."""

    __tablename__ = "site_memberships"
    __table_args__ = tenant_table_args(
        UniqueConstraint("site_id", "user_id"),
        CheckConstraint(enum_check("role", SiteRole), name="role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(Text)
    # Sakin üyeliğinde dolu. persons tablosu (Dilim 3) gelince bileşik FK eklenir.
    person_id: Mapped[uuid.UUID | None]
    is_active: Mapped[bool] = mapped_column(default=True)


class AuthSession(Base):
    """[G] Oturum: yenileme jetonu yalnız SHA-256 hash'i olarak saklanır.

    Her yenilemede jeton döner (rotasyon); eski jeton tekrar kullanılırsa oturum iptal edilir.
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    previous_token_hash: Mapped[str | None] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
