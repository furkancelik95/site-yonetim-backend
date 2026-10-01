"""Kimlik ve üyelik — docs/03-veri-modeli.md §2."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from site_yonetim.db.base import Base, TenantMixin, enum_check, tenant_fk, tenant_table_args
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
    # Geçici parola (platformun verdiği) ile açılan hesap: değiştirene kadar yalnız `/me`,
    # `/auth/*` ve parola değişikliği çalışır (docs/05 §8.1).
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default=false())


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
        tenant_fk("person_id", "persons"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(Text)
    # Sakin üyeliğinde dolu: kullanıcının bu sitedeki kişi kaydı (bileşik FK).
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


class LoginThrottle(Base):
    """[G] IP bazlı giriş hız sınırı: sabit pencerede hatalı deneme sayacı (docs/09 §4).

    Paylaşılan depo PostgreSQL'dir — birden çok API kopyası aynı sayacı görür. Sayaç parola
    doğrulamadan **önce** atomik artırılır (paralel denemeler sınırı aşamaz); başarılı girişte
    o deneme geri alınır. Pencere bitince sayaç sıfırdan başlar; eski satırları gece işi siler.
    """

    __tablename__ = "login_throttle"
    __table_args__ = (CheckConstraint("failures >= 0", name="failures"),)

    ip: Mapped[str] = mapped_column(Text, unique=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    failures: Mapped[int] = mapped_column(default=0)
