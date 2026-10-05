"""Kimlik ve üyelik — docs/03-veri-modeli.md §2."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    false,
    text,
)
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


class RateLimit(Base):
    """[G] Herkese açık uçlar için istek sınırı (sabit pencere, IP + uç anahtarı). Paylaşılan
    depo PostgreSQL — birden çok API kopyası aynı sayacı görür (`services/rate_limit.py`)."""

    __tablename__ = "rate_limits"
    __table_args__ = (CheckConstraint("count >= 0", name="count"),)

    key: Mapped[str] = mapped_column(Text, unique=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    count: Mapped[int] = mapped_column(default=0)


class RegistrationLink(TenantMixin, Base):
    """[K] Sakinin kendini kaydettiği bağlantının kodu — site başına bir. Kod tahmin edilemez
    (`secrets.token_urlsafe`), genelde benzersiz; yenilenince eskisi hemen geçersiz."""

    __tablename__ = "registration_links"
    __table_args__ = tenant_table_args(UniqueConstraint("site_id"), UniqueConstraint("code"))

    code: Mapped[str] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(default=True)


class Registration(TenantMixin, Base):
    """[K] Sakin kayıt başvurusu (servis isteği 13). Kişisel veri — saklama süresi açık karar K8.
    Aynı telefonla sitede bir bekleyen başvuru olabilir (kısmi benzersiz indeks)."""

    __tablename__ = "registrations"
    __table_args__ = tenant_table_args(
        tenant_fk("unit_id", "units"),
        tenant_fk("person_id", "persons"),
        UniqueConstraint("site_id", "number"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status"),
        CheckConstraint("relation IN ('owner', 'tenant')", name="relation"),
        Index(
            "uq_registrations_pending_phone",
            "site_id",
            "phone",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    number: Mapped[int]
    first_name: Mapped[str] = mapped_column(Text)
    last_name: Mapped[str] = mapped_column(Text)
    phone: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    unit_text: Mapped[str] = mapped_column(Text)
    relation: Mapped[str] = mapped_column(Text)
    kvkk_ack_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    explicit_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_name: Mapped[str | None] = mapped_column(Text)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    unit_id: Mapped[uuid.UUID | None]
    person_id: Mapped[uuid.UUID | None]
