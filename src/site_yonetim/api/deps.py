"""İstek bağımlılıkları — istek yaşam döngüsü (docs/02-mimari.md §4).

    1. Kimlik doğrulama   → yoksa 401       current_user
    2. Site çözümleme     → yoksa 404       site_context
    3. Erişim kontrolü    → yoksa 404       site_context (403 DEĞİL: site varlığı sızmasın)
    4. Kapsamı aç         → site_scope      site_context
    5. Modül kontrolü     → kapalıysa 404   require_module
    6. İzin kontrolü      → yoksa 403       require_permission

Siteye bağlı her uç `site_context` üzerinden geçer; `tests/architecture/test_routes.py` bunu
ve `site_context`'in kimlik istediğini denetler.
"""

import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from http import HTTPStatus
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError, UnauthorizedError
from site_yonetim.core.logging import user_id_var
from site_yonetim.core.security import InvalidTokenError, decode_access_token
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import Permission, SiteAccess, UserAccess
from site_yonetim.domain.mfa import step_up_needed
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Site, User
from site_yonetim.models.audit import Actor, set_actor
from site_yonetim.services.access import load_user_access
from site_yonetim.services.auth import authenticate
from site_yonetim.services.channels import Channels
from site_yonetim.services.delivery import DISPATCH_KEY, dispatch_due
from site_yonetim.services.mfa import required_for as mfa_required_for
from site_yonetim.services.sites import available_modules, find_site_by_slug

logger = logging.getLogger(__name__)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_bearer = HTTPBearer(auto_error=False, description="Erişim jetonu (JWT)")


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_now() -> datetime:
    """İsteğin saati (UTC). Testlerde `dependency_overrides` ile sabitlenir."""
    return datetime.now(UTC)


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = request.app.state.session_factory
    if factory is None:
        raise RuntimeError("Veritabanı yapılandırılmamış (DATABASE_URL).")
    return factory


def get_channels(request: Request) -> Channels:
    """Dış kanal adaptörleri (SMS, e-posta, push) — `services/channels.py`."""
    channels: Channels = request.app.state.channels
    return channels


async def get_session(
    request: Request,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session
        sites: set[uuid.UUID] = session.info.pop(DISPATCH_KEY, set())
    if sites:
        # İstek başarıyla bittiyse bu istekte planlanan bildirimler hemen gönderilir
        # (kritik bildirim < 30 sn, §4.8). Hata isteği bozmaz; dakikalık iş yeniden dener.
        settings: Settings = request.app.state.settings
        try:
            await dispatch_due(
                factory,
                get_channels(request),
                now=datetime.now(UTC),
                default_quota=settings.sms_default_monthly_quota,
                site_ids=sites,
            )
        except Exception:
            logger.exception("İstek sonrası bildirim gönderimi başarısız")


USER_AGENT_MAX = 300

SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
NowDep = Annotated[datetime, Depends(get_now)]
ChannelsDep = Annotated[Channels, Depends(get_channels)]

BUSINESS_TZ = ZoneInfo("Europe/Istanbul")


def get_today(now: NowDep) -> date:
    """İş günü (vade, başlangıç/bitiş tarihleri) Türkiye saatine göredir."""
    return now.astimezone(BUSINESS_TZ).date()


TodayDep = Annotated[date, Depends(get_today)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
FactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


# --- 1. Kimlik ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    session_id: uuid.UUID
    mfa_pending: bool = False
    verified_at: datetime | None = None


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError
    try:
        claims = decode_access_token(settings, credentials.credentials)
    except InvalidTokenError:
        raise UnauthorizedError from None
    user, auth_session = await authenticate(
        session, user_id=claims.user_id, session_id=claims.session_id, now=now
    )
    user_id_var.set(str(user.id))
    # Denetim kaydında "kim": istek görevi boyunca geçerli (docs/09 §6).
    set_actor(Actor(user.id, user.full_name, client_ip(request), user_agent(request)))
    return CurrentUser(
        user=user,
        session_id=claims.session_id,
        mfa_pending=auth_session.mfa_pending,
        verified_at=auth_session.verified_at,
    )


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    """Tarayıcı/cihaz bilgisi — denetim ve oturum listesi için; aşırı uzunsa kesilir."""
    value = request.headers.get("user-agent")
    return value[:USER_AGENT_MAX] if value else None


# İki adımlı kod bekleyen oturum: yalnız `/auth/mfa/verify` (ve çıkış).
PendingUserDep = Annotated[CurrentUser, Depends(current_user)]


class MfaRequiredError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.UNAUTHORIZED,
            "mfa_required",
            "İki adımlı doğrulama kodunu girin.",
        )


async def mfa_passed(current: PendingUserDep) -> CurrentUser:
    """PLT-01 S2: kod girilmeden hiçbir uç 401 `mfa_required` dışında yanıt vermez."""
    if current.mfa_pending:
        raise MfaRequiredError
    return current


# Geçici parolayla ya da 2FA kurulumu beklerken açık oturum: yalnız `/me`, parola değişikliği
# ve 2FA kurulumu (docs/05 §8.1).
AuthenticatedUserDep = Annotated[CurrentUser, Depends(mfa_passed)]


class PasswordChangeRequiredError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.FORBIDDEN,
            "password_change_required",
            "Devam etmeden önce geçici parolanızı değiştirin.",
        )


class MfaSetupRequiredError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.FORBIDDEN,
            "mfa_setup_required",
            "Yönetici rolünde iki adımlı doğrulama zorunlu. Devam etmek için doğrulayıcı "
            "uygulamayı kurun.",
        )


async def password_changed(
    current: AuthenticatedUserDep, factory: FactoryDep, now: NowDep
) -> CurrentUser:
    """Varsayılan kapı: geçici parola değiştirilmeden ve (geçiş süresi bittiyse) yönetici
    rolünde iki adımlı doğrulama kurulmadan hiçbir uç çalışmaz."""
    user = current.user
    if user.must_change_password:
        raise PasswordChangeRequiredError
    if (
        user.mfa_enabled_at is None
        and user.mfa_grace_until is not None
        and now >= user.mfa_grace_until
    ):
        # Nadir yol (geçiş süresi bitmiş, kurulmamış): rol hâlâ yönetici mi?
        access = await load_user_access(factory, user, today=now.astimezone(BUSINESS_TZ).date())
        if mfa_required_for(access):
            raise MfaSetupRequiredError
    return current


CurrentUserDep = Annotated[CurrentUser, Depends(password_changed)]


async def user_access(
    current: CurrentUserDep, factory: FactoryDep, today: TodayDep, now: NowDep
) -> UserAccess:
    return await load_user_access(factory, current.user, today=today, now=now)


UserAccessDep = Annotated[UserAccess, Depends(user_access)]


# --- 2–4. Site, erişim, kapsam ------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteContext:
    """Çözümlenmiş site ve kullanıcının oradaki erişimi. Uç çalışırken site kapsamı açıktır."""

    site: Site
    access: SiteAccess
    modules: frozenset[ModuleKey]
    session: AsyncSession


async def site_context(
    slug: str, session: SessionDep, access: UserAccessDep
) -> AsyncIterator[SiteContext]:
    # Biçimsiz slug, olmayan site ve erişimi olmayan site aynı yanıtı alır: 404.
    site = await find_site_by_slug(session, slug) if _SLUG.match(slug) else None
    site_access = access.site(site.id) if site is not None else None
    if site is None or site_access is None:
        raise NotFoundError
    with site_scope(site.id):
        modules = await available_modules(session, site)
        yield SiteContext(site=site, access=site_access, modules=modules, session=session)


SiteContextDep = Annotated[SiteContext, Depends(site_context)]


# --- 5–6. Modül ve izin -------------------------------------------------------


def require_module(key: ModuleKey) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """Uç noktanın modülü sitede kapalıysa ya da planda yoksa 404 (docs/05 §7).

    Kullanım: `ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))]`
    """

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if key not in ctx.modules:
            raise NotFoundError
        return ctx

    return dependency


def require_permission(
    permission: Permission,
) -> Callable[[SiteContext], Awaitable[SiteContext]]:
    """Siteye erişimi var ama bu işlem için izni yok → 403 (docs/05 §7). Rol adına bakılmaz."""

    async def dependency(ctx: SiteContextDep) -> SiteContext:
        if not ctx.access.can(permission):
            raise ForbiddenError
        return ctx

    return dependency


# --- Platform ----------------------------------------------------------------


async def platform_admin(current: CurrentUserDep) -> CurrentUser:
    """Platform uçları yalnız platform yöneticisine; başkası **404** alır (panelin varlığı
    sızmasın — docs/05 §5, docs/07 §8.6)."""
    if not current.user.is_platform_admin:
        raise NotFoundError
    return current


PlatformAdminDep = Annotated[CurrentUser, Depends(platform_admin)]


# --- Step-up (Yazılım Dokümanı §4.7) ----------------------------------------------------


class StepUpRequiredError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.FORBIDDEN,
            "step_up_required",
            "Bu işlem için kimliğinizi yeniden doğrulayın (son 5 dakika).",
        )


def require_step_up(
    *, sensitive: bool = True, financial: bool = False
) -> Callable[..., Awaitable[CurrentUser]]:
    """Hassas işlem kapısı: oturum son 5 dakikada doğrulanmadıysa 403 `step_up_required`
    (frontend `POST /auth/step-up` sonrası isteği yineler). `financial=True, sensitive=False`:
    yalnız telefon son 24 saatte değiştiyse istenir.

    Kullanım: `current: Annotated[CurrentUser, Depends(require_step_up())]` — iade, borç silme,
    rol yükseltme, dönem kilidi açma vb. uçlar sonra bağlanır."""

    async def dependency(current: CurrentUserDep, now: NowDep) -> CurrentUser:
        if step_up_needed(
            verified_at=current.verified_at,
            now=now,
            sensitive=sensitive,
            financial=financial,
            phone_changed_at=current.user.phone_changed_at,
        ):
            raise StepUpRequiredError
        return current

    return dependency
