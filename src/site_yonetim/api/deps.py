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

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ForbiddenError, NotFoundError, UnauthorizedError
from site_yonetim.core.logging import user_id_var
from site_yonetim.core.security import InvalidTokenError, decode_access_token
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import Permission, SiteAccess, UserAccess
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Site, User
from site_yonetim.services.access import load_user_access
from site_yonetim.services.auth import authenticate
from site_yonetim.services.sites import available_modules, find_site_by_slug

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


async def get_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
NowDep = Annotated[datetime, Depends(get_now)]

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


async def current_user(
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
    user = await authenticate(
        session, user_id=claims.user_id, session_id=claims.session_id, now=now
    )
    user_id_var.set(str(user.id))
    return CurrentUser(user=user, session_id=claims.session_id)


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


async def user_access(current: CurrentUserDep, factory: FactoryDep) -> UserAccess:
    return await load_user_access(factory, current.user)


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
