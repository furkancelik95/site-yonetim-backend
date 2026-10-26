"""Kimlik ve üyelik — docs/03-veri-modeli.md §2."""

import uuid
from datetime import datetime
from enum import StrEnum

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
from site_yonetim.domain.otp import OtpChannel, OtpPurpose


class User(Base):
    """[G] Kullanıcı. Parola yalnız argon2id hash'i olarak saklanır.

    Hesap globaldir: bir telefon (E.164) ya da e-posta tek hesaptır (Ürün Dokümanı §6.5). Sakin
    ve saha personeli telefon + SMS koduyla girer — parolası olmayabilir; yönetici rolleri
    e-posta + parola + iki adımlı doğrulama. Davetle açılan hesabın parolası daveti kabul edince
    belirlenir (o zamana dek `password_hash` boş)."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(enum_check("kind", UserKind), name="kind"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("failed_login_count >= 0", name="failed_login_count"),
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="contact_required"),
        CheckConstraint(r"phone ~ '^\+[1-9][0-9]{7,14}$'", name="phone_e164"),
    )

    email: Mapped[str | None] = mapped_column(Text, unique=True)
    phone: Mapped[str | None] = mapped_column(Text, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text)
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
    # Telefon değişikliği: sonraki 24 saat finansal işlemlerde step-up (§4.7, PLT-10).
    phone_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # İki adımlı doğrulama (TOTP): sır şifreli (`core/secretbox`); etkinleşince zaman dolu.
    # Kurulum sürerken sır dolu, `mfa_enabled_at` boştur.
    mfa_secret_sealed: Mapped[str | None] = mapped_column(Text)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_last_step: Mapped[int | None]  # son kabul edilen TOTP adımı (tekrar oynatma engeli)
    # Yönetici rolünde 2FA geçiş süresi: ilk girişte başlar (MFA_GRACE_DAYS).
    mfa_grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    # Oturum listesi (Yazılım Dokümanı PLT-10): kullanıcı kendi cihazlarını görür, kapatır.
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    # İki adımlı doğrulama bekleyen oturum: kod girilene dek her uç 401 `mfa_required`.
    mfa_pending: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Son güçlü doğrulama (giriş, 2FA, step-up) — hassas işlemlerde son 5 dakika kuralı.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthEventKind(StrEnum):
    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"  # yanlış parola, bilinmeyen e-posta ya da pasif hesap
    ACCOUNT_LOCKED = "account_locked"
    LOGOUT = "logout"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105  # nosec B105 — olay adı, sır değil
    SESSION_REVOKED = "session_revoked"  # kullanıcı bir cihazı kapattı
    TOKEN_REUSED = "token_reused"  # noqa: S105  # nosec B105 — jeton tekrarı olayı
    # PLT-01 / PLT-10 / §4.7 (P5): kodla giriş, iki adımlı doğrulama, sıfırlama, davet
    CODE_SENT = "code_sent"  # SMS/e-posta kodu gönderildi (hedef kayıtlı olmasa da)
    CODE_FAILED = "code_failed"  # hatalı kod denemesi
    CODE_LOCKED = "code_locked"  # 5 hatalı denemede hedef 15 dk kilitlendi
    MFA_ENABLED = "mfa_enabled"
    MFA_DISABLED = "mfa_disabled"
    MFA_FAILED = "mfa_failed"
    RECOVERY_CODE_USED = "recovery_code_used"
    RECOVERY_CODES_REGENERATED = "recovery_codes_regenerated"
    PASSWORD_RESET = "password_reset"  # noqa: S105  # nosec B105 — olay adı, sır değil
    PHONE_CHANGED = "phone_changed"
    STEP_UP = "step_up"
    INVITATION_SENT = "invitation_sent"
    INVITATION_ACCEPTED = "invitation_accepted"


class AuthEvent(Base):
    """[G] Kimlik olayı: giriş, başarısız giriş, kilit, çıkış, parola, oturum kapatma
    (Yazılım Dokümanı M-24). **Değişmez** (tetikleyici). Siteye değil kullanıcıya aittir —
    giriş site bağlamından önce olur. Bilinmeyen e-postayla denemede `user_id` boştur ve
    e-posta yazılmaz (yalnız IP); saklama süresi açık karar K8."""

    __tablename__ = "auth_events"
    __table_args__ = (
        CheckConstraint(enum_check("kind", AuthEventKind), name="kind"),
        Index("ix_auth_events_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(Text)
    session_id: Mapped[uuid.UUID | None]
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)


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


class OtpChallenge(Base):
    """[G] Tek kullanımlık kod (SMS/e-posta) — §4.7. Kodun **yalnız HMAC özeti** saklanır
    (hedef ve amaca bağlı). Hedef kayıtlı olsun olmasın kod üretilir (numaralandırma yok);
    `user_id` yalnız eşleşen hesap varsa dolu. Kilit (`locked_until`) hedefin son kodunda
    tutulur: 5 hatalı denemede 15 dakika."""

    __tablename__ = "otp_challenges"
    __table_args__ = (
        CheckConstraint(enum_check("purpose", OtpPurpose), name="purpose"),
        CheckConstraint(enum_check("channel", OtpChannel), name="channel"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_otp_challenges_target_created", "purpose", "target", "created_at"),
    )

    purpose: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)  # E.164 telefon ya da küçük harf e-posta
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    code_digest: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Gönderim kaydı (platform hesabı; site kotasından düşmez)
    provider: Mapped[str | None] = mapped_column(Text)
    provider_ref: Mapped[str | None] = mapped_column(Text)
    cost_units: Mapped[int] = mapped_column(default=0)
    ip: Mapped[str | None] = mapped_column(Text)


class MfaRecoveryCode(Base):
    """[G] İki adımlı doğrulamanın yedek kodu: yalnız SHA-256 özeti; bir kez kullanılır."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (UniqueConstraint("user_id", "code_digest"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    code_digest: Mapped[str] = mapped_column(Text)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserInvitation(Base):
    """[G] Hesap daveti (PLT-09): 72 saat geçerli, tek kullanımlık bağlantı. Jetonun yalnız
    SHA-256 özeti saklanır. Kabul edilince kişi parolasını belirler; yeni davet eskisini
    geçersiz kılar. Site kimliği tutulmaz (hesap global); gösterim için site adı."""

    __tablename__ = "user_invitations"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_digest: Mapped[str] = mapped_column(Text, unique=True)
    site_name: Mapped[str | None] = mapped_column(Text)
    role_label: Mapped[str | None] = mapped_column(Text)
    invited_by_name: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
