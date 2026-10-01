"""Giriş, oturum yenileme ve çıkış (docs/05 §8).

Güvenlik notları:
- Bilinmeyen e-posta ile yanlış parola **aynı** yanıtı ve yaklaşık aynı süreyi alır.
- Kilit sayacı paralel denemelerle atlatılamasın diye kullanıcı satırı kilitlenir (FOR UPDATE).
- Yenileme jetonu her kullanımda döner; eski jetonun tekrar gelmesi çalınma belirtisidir →
  oturum iptal edilir.
- Çıkış anında etkilidir: her istekte oturumun iptal edilmediği kontrol edilir.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.core.errors import ApiError, UnauthorizedError
from site_yonetim.core.security import (
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from site_yonetim.domain.login import LoginState, normalize_email, password_problem
from site_yonetim.models import AuthSession, User

logger = logging.getLogger(__name__)


class InvalidCredentialsError(UnauthorizedError):
    def __init__(self) -> None:
        super().__init__("E-posta veya parola hatalı.", code="invalid_credentials")


class AccountLockedError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.TOO_MANY_REQUESTS,
            "account_locked",
            "Çok fazla hatalı giriş denemesi yapıldı. Hesap 15 dakika kilitlendi; "
            "daha sonra tekrar deneyin.",
        )


class TooManyAttemptsError(ApiError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_attempts",
            "Bu ağdan çok fazla hatalı giriş denemesi yapıldı. "
            f"{max(1, retry_after_seconds // 60)} dakika sonra tekrar deneyin.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class PasswordChangeError(ApiError):
    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(HTTPStatus.UNPROCESSABLE_ENTITY, code, message, fields={field: message})


class SessionExpiredError(UnauthorizedError):
    def __init__(self) -> None:
        super().__init__(
            "Oturumunuzun süresi doldu. Lütfen yeniden giriş yapın.", code="session_expired"
        )


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    auth_session: AuthSession
    refresh_token: str  # yalnız çereze yazılır; saklanmaz, loglanmaz


def _new_auth_session(user: User, token: str, now: datetime, hours: int) -> AuthSession:
    return AuthSession(
        user_id=user.id,
        token_hash=hash_refresh_token(token),
        expires_at=now + timedelta(hours=hours),
        last_used_at=now,
    )


async def login(
    session: AsyncSession, *, email: str, password: str, now: datetime, session_hours: int
) -> IssuedSession:
    user = await session.scalar(
        select(User).where(User.email == normalize_email(email)).with_for_update()
    )
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None:
        raise InvalidCredentialsError

    state = LoginState(user.failed_login_count, user.locked_until)
    if state.is_locked(now):
        await session.rollback()
        raise AccountLockedError

    if not password_ok:
        new_state = state.after_failure(now)
        user.failed_login_count = new_state.failed_count
        user.locked_until = new_state.locked_until
        await session.commit()
        if new_state.is_locked(now):
            logger.warning("Hesap kilitlendi: user_id=%s", user.id)
            raise AccountLockedError
        raise InvalidCredentialsError

    if not user.is_active:
        # Pasif hesap: parola doğru olsa da aynı genel yanıt (hesap durumu sızmasın).
        await session.rollback()
        raise InvalidCredentialsError

    success = LoginState.after_success()
    user.failed_login_count = success.failed_count
    user.locked_until = success.locked_until
    user.last_login_at = now
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    token = new_refresh_token()
    auth_session = _new_auth_session(user, token, now, session_hours)
    session.add(auth_session)
    await session.commit()
    return IssuedSession(user, auth_session, token)


async def refresh(
    session: AsyncSession, *, refresh_token: str, now: datetime, session_hours: int
) -> IssuedSession:
    token_hash = hash_refresh_token(refresh_token)
    auth_session = await session.scalar(
        select(AuthSession).where(AuthSession.token_hash == token_hash).with_for_update()
    )
    if auth_session is None:
        reused = await session.scalar(
            select(AuthSession)
            .where(AuthSession.previous_token_hash == token_hash)
            .with_for_update()
        )
        if reused is not None and reused.revoked_at is None:
            reused.revoked_at = now
            await session.commit()
            logger.warning("Eski yenileme jetonu tekrar kullanıldı; oturum iptal: %s", reused.id)
        raise SessionExpiredError

    if auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise SessionExpiredError
    user = await session.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise SessionExpiredError

    token = new_refresh_token()
    auth_session.previous_token_hash = auth_session.token_hash
    auth_session.token_hash = hash_refresh_token(token)
    auth_session.last_used_at = now
    auth_session.expires_at = now + timedelta(hours=session_hours)  # kayan pencere
    await session.commit()
    return IssuedSession(user, auth_session, token)


async def logout(
    session: AsyncSession,
    *,
    now: datetime,
    session_id: uuid.UUID | None = None,
    refresh_token: str | None = None,
) -> None:
    """Oturumu iptal eder. Oturum bulunamazsa sessizce geçer (çıkış her zaman başarılıdır)."""
    query = select(AuthSession).with_for_update()
    if session_id is not None:
        query = query.where(AuthSession.id == session_id)
    elif refresh_token is not None:
        query = query.where(AuthSession.token_hash == hash_refresh_token(refresh_token))
    else:
        return
    auth_session = await session.scalar(query)
    if auth_session is not None and auth_session.revoked_at is None:
        auth_session.revoked_at = now
        await session.commit()


async def authenticate(
    session: AsyncSession, *, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime
) -> User:
    """Erişim jetonunu taşıyan isteğin oturumu ve kullanıcısı hâlâ geçerli mi?"""
    auth_session = await session.get(AuthSession, session_id)
    if (
        auth_session is None
        or auth_session.user_id != user_id
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= now
    ):
        raise SessionExpiredError
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise SessionExpiredError
    return user


async def change_password(
    session: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    keep_session_id: uuid.UUID,
    now: datetime,
) -> None:
    """Parola değişikliği: mevcut parola doğrulanır (hesap kilidi kuralı burada da işler).

    Başarılıysa geçici parola işareti kalkar ve kullanıcının **diğer bütün oturumları**
    iptal edilir (parolası ele geçmiş olabilecek başka cihazlar düşer); bu oturum sürer.
    """
    locked = await session.scalar(select(User).where(User.id == user.id).with_for_update())
    if locked is None:  # bu arada silinmiş olabilir
        raise SessionExpiredError
    state = LoginState(locked.failed_login_count, locked.locked_until)
    if state.is_locked(now):
        await session.rollback()
        raise AccountLockedError
    if not verify_password(locked.password_hash, current_password):
        new_state = state.after_failure(now)
        locked.failed_login_count = new_state.failed_count
        locked.locked_until = new_state.locked_until
        await session.commit()
        if new_state.is_locked(now):
            logger.warning("Hesap kilitlendi (parola değişikliği): user_id=%s", locked.id)
            raise AccountLockedError
        raise PasswordChangeError(
            "invalid_current_password", "current_password", "Mevcut parola hatalı."
        )
    problem = password_problem(new_password)
    if problem is not None:
        await session.rollback()
        raise PasswordChangeError("weak_password", "new_password", problem)
    if verify_password(locked.password_hash, new_password):
        await session.rollback()
        raise PasswordChangeError(
            "password_unchanged", "new_password", "Yeni parola mevcut paroladan farklı olmalı."
        )

    locked.password_hash = hash_password(new_password)
    locked.must_change_password = False
    locked.failed_login_count = 0
    locked.locked_until = None
    await session.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == locked.id,
            AuthSession.id != keep_session_id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await session.commit()
    logger.info("Parola değiştirildi: user_id=%s", locked.id)
