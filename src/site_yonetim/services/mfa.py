"""İki adımlı doğrulama ve step-up — Yazılım Dokümanı §4.7, PLT-01, PLT-10.

- Kurulum: sır üretilir (şifreli saklanır) ve `otpauth://` adresi döner; ilk doğru kodla
  etkinleşir ve 10 yedek kod **bir kez** gösterilir (yalnız özetleri saklanır).
- Girişte ikinci adım: TOTP ya da yedek kod. Hatalı kod hesap kilidi sayacını artırır
  (5 hatada 15 dk); aynı TOTP adımı iki kez kabul edilmez.
- Yönetici rolünde kapatılamaz (zorunlu); diğer rollerde kod ile kapatılır.
- Step-up: oturumun son doğrulama zamanı (`auth_sessions.verified_at`) yenilenir.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.errors import ApiError
from site_yonetim.core.secretbox import SecretBoxError, seal, unseal
from site_yonetim.core.security import verify_password
from site_yonetim.domain.access import UserAccess
from site_yonetim.domain.mfa import (
    looks_like_recovery_code,
    new_recovery_codes,
    new_secret,
    otpauth_uri,
    recovery_digest,
    requires_mfa,
    verify_totp,
)
from site_yonetim.models import AuthEventKind, AuthSession, MfaRecoveryCode, User
from site_yonetim.services.auth import AccountLockedError, register_failure
from site_yonetim.services.auth_events import Client, event


class MfaError(ApiError):
    def __init__(self, status: int, code: str, message: str, field: str | None = None) -> None:
        super().__init__(status, code, message, fields={field: message} if field else None)


def invalid_code() -> MfaError:
    return MfaError(
        HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_code", "Doğrulama kodu hatalı.", "code"
    )


def required_for(access: UserAccess) -> bool:
    """Kullanıcı yönetici rolünde mi (2FA zorunlu)?"""
    return requires_mfa(
        is_platform_admin=access.is_platform_admin,
        role_levels=[g.level for s in access.sites for g in s.roles],
        has_organization_role=any(s.organization_role is not None for s in access.sites),
    )


@dataclass(frozen=True, slots=True)
class Setup:
    secret: str
    otpauth_uri: str
    account: str
    issuer: str


async def start_setup(session: AsyncSession, user: User, *, key: bytes, issuer: str) -> Setup:
    """Yeni sır üretir (kurulum sürerken öncekini ezer). Kurulu doğrulayıcı varsa 409."""
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:  # pragma: no cover
        raise LookupError
    if locked.mfa_enabled_at is not None:
        await session.rollback()
        raise MfaError(
            HTTPStatus.CONFLICT,
            "mfa_already_enabled",
            "İki adımlı doğrulama zaten kurulu. Yeni cihaz için önce kapatıp yeniden kurun.",
        )
    secret = new_secret()
    locked.mfa_secret_sealed = seal(key, secret)
    locked.mfa_last_step = None
    account = locked.email or locked.phone or str(locked.id)
    await session.commit()
    return Setup(secret, otpauth_uri(secret, account=account, issuer=issuer), account, issuer)


def _secret(user: User, key: bytes) -> str | None:
    if not user.mfa_secret_sealed:
        return None
    try:
        return unseal(key, user.mfa_secret_sealed)
    except SecretBoxError:
        return None


async def _replace_recovery_codes(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))
    codes = new_recovery_codes()
    session.add_all(MfaRecoveryCode(user_id=user_id, code_digest=recovery_digest(c)) for c in codes)
    return codes


async def enable(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    key: bytes,
    now: datetime,
    client: Client,
    session_id: uuid.UUID,
) -> list[str]:
    """İlk doğru kodla etkinleştirir; yedek kodları döner (yalnız bu yanıtta)."""
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:  # pragma: no cover
        raise LookupError
    if locked.mfa_enabled_at is not None:
        await session.rollback()
        raise MfaError(
            HTTPStatus.CONFLICT, "mfa_already_enabled", "İki adımlı doğrulama zaten kurulu."
        )
    secret = _secret(locked, key)
    if secret is None:
        await session.rollback()
        raise MfaError(
            HTTPStatus.CONFLICT, "mfa_setup_missing", "Önce kurulumu başlatın (QR kodu alın)."
        )
    step = verify_totp(secret, code, now, last_step=None)
    if step is None:
        await session.rollback()
        raise invalid_code()
    locked.mfa_enabled_at = now
    locked.mfa_last_step = step
    codes = await _replace_recovery_codes(session, locked.id)
    await session.execute(
        update(AuthSession).where(AuthSession.id == session_id).values(verified_at=now)
    )
    event(session, AuthEventKind.MFA_ENABLED, client, user_id=locked.id, session_id=session_id)
    await session.commit()
    return codes


async def _check_second_factor(
    session: AsyncSession, user: User, code: str, *, key: bytes, now: datetime, client: Client
) -> bool:
    """TOTP ya da yedek kod; doğruysa kullanılmış işaretler (commit etmez)."""
    if looks_like_recovery_code(code):
        row = await session.scalar(
            select(MfaRecoveryCode)
            .where(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.code_digest == recovery_digest(code),
                MfaRecoveryCode.used_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            return False
        row.used_at = now
        event(session, AuthEventKind.RECOVERY_CODE_USED, client, user_id=user.id)
        return True
    secret = _secret(user, key)
    if secret is None:
        return False
    step = verify_totp(secret, code, now, last_step=user.mfa_last_step)
    if step is None:
        return False
    user.mfa_last_step = step
    return True


async def verify_login(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    code: str,
    key: bytes,
    now: datetime,
    session_hours: int,
    client: Client,
) -> AuthSession:
    """Beklemedeki oturumu ikinci adımla tamamlar. Hatalı kod kilit sayacını artırır; 5.
    hatada hesap 15 dk kilitlenir ve beklemedeki oturum kapanır."""
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.id == session_id).with_for_update()
    )
    if user is None or auth_session is None:  # pragma: no cover
        raise LookupError
    if not auth_session.mfa_pending:
        await session.rollback()
        raise MfaError(HTTPStatus.CONFLICT, "mfa_not_pending", "Bu oturum zaten doğrulandı.")
    if user.locked_until is not None and now < user.locked_until:
        await session.rollback()
        raise AccountLockedError
    if not await _check_second_factor(session, user, code, key=key, now=now, client=client):
        try:
            await register_failure(
                session, user, now=now, client=client, kind=AuthEventKind.MFA_FAILED
            )
        except AccountLockedError:
            await session.execute(
                update(AuthSession).where(AuthSession.id == session_id).values(revoked_at=now)
            )
            await session.commit()
            raise
        raise invalid_code() from None
    auth_session.mfa_pending = False
    auth_session.verified_at = now
    auth_session.expires_at = now + timedelta(hours=session_hours)
    user.failed_login_count = 0
    await session.commit()
    return auth_session


async def regenerate_recovery_codes(
    session: AsyncSession, user: User, *, code: str, key: bytes, now: datetime, client: Client
) -> list[str]:
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None or locked.mfa_enabled_at is None:
        await session.rollback()
        raise MfaError(HTTPStatus.CONFLICT, "mfa_not_enabled", "İki adımlı doğrulama kurulu değil.")
    secret = _secret(locked, key)
    step = verify_totp(secret, code, now, last_step=locked.mfa_last_step) if secret else None
    if step is None:
        await register_failure(
            session, locked, now=now, client=client, kind=AuthEventKind.MFA_FAILED
        )
        raise invalid_code()
    locked.mfa_last_step = step
    codes = await _replace_recovery_codes(session, locked.id)
    event(session, AuthEventKind.RECOVERY_CODES_REGENERATED, client, user_id=locked.id)
    await session.commit()
    return codes


async def disable(
    session: AsyncSession,
    user: User,
    *,
    code: str,
    key: bytes,
    now: datetime,
    client: Client,
    role_requires: bool,
) -> None:
    if role_requires:
        raise MfaError(
            HTTPStatus.CONFLICT,
            "mfa_required_for_role",
            "Yönetici rolünde iki adımlı doğrulama zorunludur; kapatılamaz.",
        )
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None or locked.mfa_enabled_at is None:
        await session.rollback()
        raise MfaError(HTTPStatus.CONFLICT, "mfa_not_enabled", "İki adımlı doğrulama kurulu değil.")
    if not await _check_second_factor(session, locked, code, key=key, now=now, client=client):
        await register_failure(
            session, locked, now=now, client=client, kind=AuthEventKind.MFA_FAILED
        )
        raise invalid_code()
    locked.mfa_enabled_at = None
    locked.mfa_secret_sealed = None
    locked.mfa_last_step = None
    await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == locked.id))
    event(session, AuthEventKind.MFA_DISABLED, client, user_id=locked.id)
    await session.commit()


async def recovery_codes_left(session: AsyncSession, user_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count()).where(
            MfaRecoveryCode.user_id == user_id, MfaRecoveryCode.used_at.is_(None)
        )
    )
    return int(count or 0)


# --- Step-up -------------------------------------------------------------------------


async def step_up(
    session: AsyncSession,
    user: User,
    *,
    session_id: uuid.UUID,
    password: str | None,
    code: str | None,
    key: bytes,
    now: datetime,
    client: Client,
) -> datetime:
    """Parola ya da 2FA koduyla yeniden doğrulama; oturumun `verified_at`'ı yenilenir.
    (SMS koduyla step-up API katmanında `services/otp` ile doğrulanıp `mark_verified` çağrılır.)"""
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:  # pragma: no cover
        raise LookupError
    if locked.locked_until is not None and now < locked.locked_until:
        await session.rollback()
        raise AccountLockedError
    ok = False
    if code and locked.mfa_enabled_at is not None:
        ok = await _check_second_factor(session, locked, code, key=key, now=now, client=client)
    elif password and locked.password_hash is not None:
        ok = verify_password(locked.password_hash, password)
    if not ok:
        await register_failure(
            session, locked, now=now, client=client, kind=AuthEventKind.MFA_FAILED
        )
        raise MfaError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "step_up_failed",
            "Doğrulama başarısız. Parolanızı ya da doğrulama kodunu kontrol edin.",
        )
    return await mark_verified(
        session, user_id=locked.id, session_id=session_id, now=now, client=client
    )


async def mark_verified(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime,
    client: Client,
) -> datetime:
    await session.execute(
        update(AuthSession).where(AuthSession.id == session_id).values(verified_at=now)
    )
    event(session, AuthEventKind.STEP_UP, client, user_id=user_id, session_id=session_id)
    await session.commit()
    return now
