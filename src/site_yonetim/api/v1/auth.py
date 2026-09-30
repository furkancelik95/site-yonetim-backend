"""Kimlik uçları — docs/06 §2.1, docs/05 §8.

Erişim jetonu yanıt gövdesinde döner (frontend bellekte tutar). Yenileme jetonu yalnız
httpOnly + SameSite=Lax (+ üretimde Secure) çerezde taşınır; JavaScript okuyamaz.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Request, Response, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import NowDep, SessionDep, SettingsDep
from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ForbiddenError, UnauthorizedError
from site_yonetim.core.security import InvalidTokenError, create_access_token, decode_access_token
from site_yonetim.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["kimlik"])

REFRESH_COOKIE = "sy_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # Üst sınır: çok uzun parola ile hash maliyeti üzerinden hizmet dışı bırakmaya karşı.
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — şema türü, sır değil
    expires_in: int = Field(description="Erişim jetonunun ömrü, saniye")


def _issue(
    settings: Settings, response: Response, issued: auth_service.IssuedSession
) -> TokenResponse:
    now = issued.auth_session.last_used_at
    token = create_access_token(
        settings, user_id=issued.user.id, session_id=issued.auth_session.id, now=now
    )
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=settings.session_hours * 3600,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=token, expires_in=settings.jwt_access_minutes * 60)


def _clear_cookie(settings: Settings, response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )


def _check_origin(request: Request, settings: Settings) -> None:
    """Çerezle kimlik doğrulayan uçlarda CSRF savunması: tarayıcıdan gelen istek
    yalnız izinli frontend kaynağından olabilir (SameSite=Lax'a ek katman)."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        raise ForbiddenError("Bu kaynaktan gelen isteğe izin verilmiyor.")


@router.post("/login", summary="Giriş")
async def login(
    body: LoginRequest,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
) -> TokenResponse:
    """E-posta + parola. 5 hatalı denemede hesap 15 dakika kilitlenir (429)."""
    issued = await auth_service.login(
        session,
        email=body.email,
        password=body.password,
        now=now,
        session_hours=settings.session_hours,
    )
    return _issue(settings, response, issued)


@router.post("/refresh", summary="Oturumu yenile")
async def refresh(
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> TokenResponse:
    """Çerezdeki yenileme jetonuyla yeni erişim jetonu. Yenileme jetonu da yenilenir."""
    _check_origin(request, settings)
    if not refresh_token:
        raise UnauthorizedError
    try:
        issued = await auth_service.refresh(
            session, refresh_token=refresh_token, now=now, session_hours=settings.session_hours
        )
    except UnauthorizedError:
        _clear_cookie(settings, response)
        raise
    return _issue(settings, response, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Çıkış")
async def logout(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> Response:
    """Oturumu anında iptal eder (erişim jetonu da geçersizleşir). Her zaman 204."""
    _check_origin(request, settings)
    session_id = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        try:
            session_id = decode_access_token(settings, header[7:]).session_id
        except InvalidTokenError:
            session_id = None
    await auth_service.logout(session, now=now, session_id=session_id, refresh_token=refresh_token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(settings, response)
    return response
