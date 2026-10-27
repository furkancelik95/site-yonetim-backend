"""Giriş, oturum yenileme ve çıkış (docs/05 §8).

Güvenlik notları:
- Bilinmeyen e-posta ile yanlış parola **aynı** yanıtı ve yaklaşık aynı süreyi alır.
- Kilit sayacı paralel denemelerle atlatılamasın diye kullanıcı satırı kilitlenir (FOR UPDATE).
- Yenileme jetonu her kullanımda döner; eski jetonun tekrar gelmesi çalınma belirtisidir →
  oturum iptal edilir.
- Çıkış anında etkilidir: her istekte oturumun iptal edilmediği kontrol edilir.
- Her giriş, başarısız giriş, kilit, çıkış, parola değişikliği ve jeton tekrarı `auth_events`'e
  yazılır (değişmez; bilinmeyen e-postada e-posta yazılmaz).
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
from site_yonetim.models import AuthEvent, AuthEventKind, AuthSession, User
from site_yonetim.services.auth_events import NO_CLIENT, Client, event

__all__ = ["NO_CLIENT", "Client"]

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


_event = event  # kısa ad (bu modülde çok kullanılır)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    auth_session: AuthSession
    refresh_token: str  # yalnız çereze yazılır; saklanmaz, loglanmaz
    mfa_setup_deadline: datetime | None = None  # yönetici rolünde 2FA kurulmamış: son gün


PENDING_MFA_MINUTES = 10  # parola/kod doğru, iki adımlı kod bekleniyor


def _new_auth_session(
    user: User, token: str, now: datetime, hours: int, client: Client
) -> AuthSession:
    return AuthSession(
        id=uuid.uuid7(),
        user_id=user.id,
        token_hash=hash_refresh_token(token),
        expires_at=now + timedelta(hours=hours),
        last_used_at=now,
        ip=client.ip,
        user_agent=client.user_agent,
    )


async def check_password(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    now: datetime,
    client: Client = NO_CLIENT,
) -> User:
    """E-posta + parola doğrulaması (kilit kuralıyla). Başarılıysa kullanıcı satırı kilitli
    döner (transaction açık); `open_session` kapatır. Hatada olay yazılır ve commit edilir."""
    user = await session.scalar(
        select(User).where(User.email == normalize_email(email)).with_for_update()
    )
    password_ok = verify_password(user.password_hash if user else None, password)
    if user is None:
        _event(session, AuthEventKind.LOGIN_FAILED, client)  # e-posta yazılmaz
        await session.commit()
        raise InvalidCredentialsError

    user_id = user.id  # rollback sonrası nesne süresi dolar; kimlik önceden alınır
    state = LoginState(user.failed_login_count, user.locked_until)
    if state.is_locked(now):
        await session.rollback()
        _event(session, AuthEventKind.LOGIN_FAILED, client, user_id=user_id)
        await session.commit()
        raise AccountLockedError

    if not password_ok:
        await register_failure(session, user, now=now, client=client)
        raise InvalidCredentialsError

    if not user.is_active:
        # Pasif hesap: parola doğru olsa da aynı genel yanıt (hesap durumu sızmasın).
        await session.rollback()
        _event(session, AuthEventKind.LOGIN_FAILED, client, user_id=user_id)
        await session.commit()
        raise InvalidCredentialsError

    if user.password_hash is not None and password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


async def register_failure(
    session: AsyncSession,
    user: User,
    *,
    now: datetime,
    client: Client,
    kind: AuthEventKind = AuthEventKind.LOGIN_FAILED,
) -> None:
    """Hatalı parola / iki adımlı kod: sayaç artar, 5. hatada 15 dk kilit (commit eder)."""
    state = LoginState(user.failed_login_count, user.locked_until)
    new_state = state.after_failure(now)
    user.failed_login_count = new_state.failed_count
    user.locked_until = new_state.locked_until
    _event(session, kind, client, user_id=user.id)
    locked = new_state.is_locked(now)
    if locked:
        _event(session, AuthEventKind.ACCOUNT_LOCKED, client, user_id=user.id)
    await session.commit()
    if locked:
        logger.warning("Hesap kilitlendi: user_id=%s", user.id)
        raise AccountLockedError


async def open_session(
    session: AsyncSession,
    user: User,
    *,
    now: datetime,
    session_hours: int,
    client: Client = NO_CLIENT,
    mfa_role: bool = False,
    mfa_grace_days: int = 0,
) -> IssuedSession:
    """Kimliği doğrulanmış kullanıcıya oturum açar (parola, SMS kodu ya da davet).

    - İki adımlı doğrulama kuruluysa oturum **beklemede** açılır: 10 dakika içinde kod
      girilmezse düşer; o zamana dek her uç 401 `mfa_required` (PLT-01 S2).
    - Yönetici rolünde kurulu değilse geçiş süresi ilk girişte başlar (`mfa_grace_days`)."""
    success = LoginState.after_success()
    user.failed_login_count = success.failed_count
    user.locked_until = success.locked_until
    user.last_login_at = now
    pending = user.mfa_enabled_at is not None
    deadline = None
    if mfa_role and not pending:
        if user.mfa_grace_until is None:
            user.mfa_grace_until = now + timedelta(days=mfa_grace_days)
        deadline = user.mfa_grace_until

    token = new_refresh_token()
    hours = session_hours
    auth_session = _new_auth_session(user, token, now, hours, client)
    if pending:
        auth_session.mfa_pending = True
        auth_session.expires_at = now + timedelta(minutes=PENDING_MFA_MINUTES)
    else:
        auth_session.verified_at = now
    session.add(auth_session)
    _event(
        session,
        AuthEventKind.LOGIN_SUCCEEDED,
        client,
        user_id=user.id,
        session_id=auth_session.id,
    )
    await session.commit()
    return IssuedSession(user, auth_session, token, deadline)


async def refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    now: datetime,
    session_hours: int,
    client: Client = NO_CLIENT,
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
            _event(
                session,
                AuthEventKind.TOKEN_REUSED,
                client,
                user_id=reused.user_id,
                session_id=reused.id,
            )
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
    if not auth_session.mfa_pending:  # beklemedeki oturum uzamaz
        auth_session.expires_at = now + timedelta(hours=session_hours)  # kayan pencere
    if client.ip is not None:
        auth_session.ip = client.ip  # cihaz ağ değiştirmiş olabilir: listede son adres
    if client.user_agent is not None:
        auth_session.user_agent = client.user_agent
    await session.commit()
    return IssuedSession(user, auth_session, token)


async def logout(
    session: AsyncSession,
    *,
    now: datetime,
    session_id: uuid.UUID | None = None,
    refresh_token: str | None = None,
    client: Client = NO_CLIENT,
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
        _event(
            session,
            AuthEventKind.LOGOUT,
            client,
            user_id=auth_session.user_id,
            session_id=auth_session.id,
        )
        await session.commit()


async def authenticate(
    session: AsyncSession, *, user_id: uuid.UUID, session_id: uuid.UUID, now: datetime
) -> tuple[User, AuthSession]:
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
    return user, auth_session


async def change_password(
    session: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    keep_session_id: uuid.UUID,
    now: datetime,
    client: Client = NO_CLIENT,
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
    _event(
        session,
        AuthEventKind.PASSWORD_CHANGED,
        client,
        user_id=locked.id,
        session_id=keep_session_id,
    )
    await session.commit()
    logger.info("Parola değiştirildi: user_id=%s", locked.id)


# --- Oturumlar ve cihazlar (Yazılım Dokümanı PLT-10) -----------------------------------

EVENTS_LIMIT = 50


async def active_sessions(
    session: AsyncSession, *, user_id: uuid.UUID, now: datetime
) -> list[AuthSession]:
    """Kullanıcının açık oturumları, son kullanılan üstte."""
    rows = await session.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        .order_by(AuthSession.last_used_at.desc(), AuthSession.id.desc())
    )
    return list(rows)


async def revoke_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    now: datetime,
    client: Client = NO_CLIENT,
) -> bool:
    """Kullanıcının kendi oturumunu kapatır; başkasının ya da kapalı oturum → False."""
    target = await session.scalar(
        select(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if target is None:
        return False
    target.revoked_at = now
    _event(session, AuthEventKind.SESSION_REVOKED, client, user_id=user_id, session_id=target.id)
    await session.commit()
    return True


async def revoke_all_sessions(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    keep_session_id: uuid.UUID | None,
    now: datetime,
    client: Client = NO_CLIENT,
) -> int:
    """ "Tüm cihazlardan çık": açık oturumların hepsi (ya da bu oturum hariç hepsi) kapanır."""
    conditions = [AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)]
    if keep_session_id is not None:
        conditions.append(AuthSession.id != keep_session_id)
    revoked = list(
        await session.scalars(
            update(AuthSession).where(*conditions).values(revoked_at=now).returning(AuthSession.id)
        )
    )
    for revoked_id in revoked:
        _event(
            session, AuthEventKind.SESSION_REVOKED, client, user_id=user_id, session_id=revoked_id
        )
    await session.commit()
    return len(revoked)


async def recent_events(session: AsyncSession, *, user_id: uuid.UUID) -> list[AuthEvent]:
    """Kullanıcının son kimlik olayları (giriş geçmişi), yeni üstte."""
    rows = await session.scalars(
        select(AuthEvent)
        .where(AuthEvent.user_id == user_id)
        .order_by(AuthEvent.created_at.desc(), AuthEvent.id.desc())
        .limit(EVENTS_LIMIT)
    )
    return list(rows)


async def set_new_password(
    session: AsyncSession,
    *,
    user: User,
    new_password: str,
    now: datetime,
    client: Client,
    kind: AuthEventKind,
) -> None:
    """Kodla sıfırlama ya da daveti kabul: kural kontrolü, kilit ve geçici parola işareti
    kalkar, **bütün açık oturumlar** kapanır. Commit etmez."""
    problem = password_problem(new_password)
    if problem is not None:
        raise PasswordChangeError("weak_password", "new_password", problem)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    await session.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    _event(session, kind, client, user_id=user.id)
