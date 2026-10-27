"""İstek bağımlılıkları — istek yaşam döngüsü (docs/02-mimari.md §4).

    1. Kimlik doğrulama   → yoksa 401       current_user
    2. Site çözümleme     → yoksa 404       site_context
    3. Erişim kontrolü    → yoksa 404       site_context (403 DEĞİL: site varlığı sızmasın)
    4. Kapsamı aç         → site_scope      site_context
    4a. Salt okunur erişim → yazma 403      site_context (destek oturumu, sakin olarak görüntüle)
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
from site_yonetim.domain.access import LEVEL_MAX as ROLE_LEVEL_MAX
from site_yonetim.domain.access import (
    TEMPLATE_PERMISSIONS,
    AccessMode,
    Permission,
    RoleGrant,
    SiteAccess,
    SiteRole,
    UserAccess,
)
from site_yonetim.domain.mfa import step_up_needed
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.domain.support import VIEW_AS_ROLE_KEY, VIEW_AS_ROLE_NAME
from site_yonetim.models import Site, User
from site_yonetim.models.audit import (
    Actor,
    ActorType,
    AuditAction,
    current_actor,
    record,
    set_actor,
)
from site_yonetim.services import privacy as privacy_svc
from site_yonetim.services import support as support_svc
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


# Parolası değişmiş ama aydınlatma onayı bekleyebilen oturum: yalnız onay uçları (P6).
PasswordChangedUserDep = Annotated[CurrentUser, Depends(password_changed)]


async def resolved_access(
    current: PasswordChangedUserDep, factory: FactoryDep, today: TodayDep, now: NowDep
) -> UserAccess:
    """İstek başına bir kez çözülür (FastAPI bağımlılık önbelleği): onay kapısı ve site
    bağlamı aynı sonucu kullanır."""
    return await load_user_access(factory, current.user, today=today, now=now)


class ConsentRequiredError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.FORBIDDEN,
            "consent_required",
            "Devam etmeden önce güncel aydınlatma metnini okuyup onaylayın.",
        )


async def consent_given(
    current: PasswordChangedUserDep,
    access: Annotated[UserAccess, Depends(resolved_access)],
    factory: FactoryDep,
) -> CurrentUser:
    """Aydınlatma onayı kapısı (PM kararı P6, PLT-01 adım 14): kullanıcının erişebildiği bir
    sitede onaylamadığı son sürüm varsa yalnız `/me`, `/auth/*`, `/me/consents` ve herkese açık
    metin çalışır; diğer uçlar 403 `consent_required`. Parola kapısından sonra gelir."""
    if access.sites and await privacy_svc.pending_notices(
        factory, current.user.id, [s.site_id for s in access.sites]
    ):
        raise ConsentRequiredError
    return current


# Varsayılan kapı: geçici parola değişmiş ve güncel aydınlatma metni onaylanmış oturum.
CurrentUserDep = Annotated[CurrentUser, Depends(consent_given)]


async def user_access(
    _: CurrentUserDep, access: Annotated[UserAccess, Depends(resolved_access)]
) -> UserAccess:
    return access


UserAccessDep = Annotated[UserAccess, Depends(user_access)]


# --- 2–4. Site, erişim, kapsam ------------------------------------------------


@dataclass(frozen=True, slots=True)
class SiteContext:
    """Çözümlenmiş site ve kullanıcının oradaki erişimi. Uç çalışırken site kapsamı açıktır."""

    site: Site
    access: SiteAccess
    modules: frozenset[ModuleKey]
    session: AsyncSession
    # Sakin olarak görüntülemede görüntülenen kişinin adı (bant için)
    view_as_person_name: str | None = None


VIEW_AS_HEADER = "X-View-As-Session"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Salt okunur erişimde de çalışan yazma uçları: sakin olarak görüntülemeyi açmak/kapatmak
# (destek ekibi oturum içinde kullanır — PYN-04 adım 5).
READ_ONLY_EXEMPT_ROUTES = (
    re.compile(r"^/api/v1/sites/[^/]+/people/[^/]+/view-as-sessions$"),  # POST: başlat
    re.compile(r"^/api/v1/sites/[^/]+/view-as-sessions/[^/]+$"),  # DELETE: çık
)


class ReadOnlyAccessError(ApiError):
    def __init__(self, mode: AccessMode) -> None:
        message = (
            "Destek oturumu salt okunurdur; bu işlem yapılamaz."
            if mode is AccessMode.SUPPORT_SESSION
            else "Sakin olarak görüntüleme salt okunurdur; bu işlem yapılamaz."
        )
        super().__init__(HTTPStatus.FORBIDDEN, "read_only", message)


class ViewAsEndedError(ApiError):
    def __init__(self) -> None:
        super().__init__(
            HTTPStatus.FORBIDDEN,
            "view_as_ended",
            "Sakin olarak görüntüleme oturumu sona erdi. Kişi kartından yeniden başlatın.",
        )


def _read_only_exempt(request: Request) -> bool:
    return any(pattern.match(request.url.path) for pattern in READ_ONLY_EXEMPT_ROUTES)


async def _view_as_access(
    session: AsyncSession, base: SiteAccess, view_as_id: str, user: User, now: datetime
) -> tuple[SiteAccess, str]:
    """İstek sakin olarak görüntüleme oturumuyla geliyorsa erişim **sakinin** erişimidir
    (PLT-12: görüntüleyenin kendi yetkileri eklenmez)."""
    try:
        session_id = uuid.UUID(view_as_id)
    except ValueError:
        raise ViewAsEndedError from None
    if not base.can(Permission.SUPPORT_VIEW_AS_RESIDENT):
        raise ViewAsEndedError
    found = await support_svc.usable_view_as(session, session_id, user.id, now)
    if found is None:
        raise ViewAsEndedError
    row, person = found
    permissions = TEMPLATE_PERMISSIONS[SiteRole.RESIDENT]
    role = RoleGrant(
        key=VIEW_AS_ROLE_KEY,
        name=VIEW_AS_ROLE_NAME,
        level=ROLE_LEVEL_MAX,
        permissions=permissions,
        ends_at=row.ends_at,
    )
    access = SiteAccess(
        site_id=base.site_id,
        name=base.name,
        slug=base.slug,
        roles=(role,),
        permissions=permissions,
        person_id=person.id,
        is_derived=True,
        organization_id=base.organization_id,
        organization_role=None,
        mode=AccessMode.VIEW_AS,
        session_id=row.id,
        ends_at=row.ends_at,
    )
    return access, person.full_name


async def _record_view(
    factory: async_sessionmaker[AsyncSession], site_id: uuid.UUID, access: SiteAccess, path: str
) -> None:
    """Salt okunur erişimde açılan her ekran denetim kaydına (PYN-04 "tüm okumalar", PLT-12
    "açılan ekranlar"). Ayrı transaction: uç veri yazmasa da kayıt kalır."""
    entity = "support_sessions" if access.mode is AccessMode.SUPPORT_SESSION else "view_as_sessions"
    with site_scope(site_id):
        async with factory() as audit_session, audit_session.begin():
            detail: dict[str, object] = {"path": path}
            if access.mode is AccessMode.VIEW_AS:
                detail["person_id"] = str(access.person_id)
            record(audit_session, AuditAction.VIEW, entity, access.session_id, detail)


async def site_context(
    slug: str,
    request: Request,
    session: SessionDep,
    access: UserAccessDep,
    current: CurrentUserDep,
    factory: FactoryDep,
    now: NowDep,
) -> AsyncIterator[SiteContext]:
    # Biçimsiz slug, olmayan site ve erişimi olmayan site aynı yanıtı alır: 404.
    site = await find_site_by_slug(session, slug) if _SLUG.match(slug) else None
    site_access = access.site(site.id) if site is not None else None
    if site is None or site_access is None:
        raise NotFoundError
    with site_scope(site.id):
        person_name: str | None = None
        if site_access.mode is AccessMode.SUPPORT_SESSION:
            # Denetimde aktör tipi "destek oturumu" (Yazılım Dokümanı M-24, PYN-04 adım 6)
            actor = current_actor()
            if actor is not None:
                set_actor(
                    Actor(
                        actor.user_id,
                        f"{actor.name} (destek)",
                        actor.ip,
                        actor.user_agent,
                        ActorType.SUPPORT_SESSION,
                    )
                )
        view_as_id = request.headers.get(VIEW_AS_HEADER)
        if view_as_id:
            site_access, person_name = await _view_as_access(
                session, site_access, view_as_id, current.user, now
            )
        if site_access.read_only:
            if request.method not in _SAFE_METHODS and not _read_only_exempt(request):
                raise ReadOnlyAccessError(site_access.mode)
            await _record_view(factory, site.id, site_access, request.url.path)
        modules = await available_modules(session, site)
        yield SiteContext(
            site=site,
            access=site_access,
            modules=modules,
            session=session,
            view_as_person_name=person_name,
        )


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
