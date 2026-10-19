"""Oturumlar, cihazlar ve giriş geçmişi — Yazılım Dokümanı PLT-10 (docs/06 §2.1).

Kullanıcı yalnız **kendi** oturumlarını görür ve kapatır; başkasının oturum kimliği "yok"tur
(404). Kapatılan oturumun erişim jetonu bir sonraki istekte geçersizdir (her istek oturumu
kontrol eder). Tümünü kapatmak bu oturumu da kapatabilir (`keep_current=false`): frontend o
zaman giriş ekranına döner.
"""

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import CurrentUserDep, NowDep, SessionDep, client_ip, user_agent
from site_yonetim.api.schemas import Written
from site_yonetim.core.errors import NotFoundError
from site_yonetim.domain.devices import describe_device
from site_yonetim.models import AuthEventKind
from site_yonetim.services import auth as auth_service

router = APIRouter(prefix="/me", tags=["kimlik"])

EVENT_LABELS = {
    AuthEventKind.LOGIN_SUCCEEDED: "Giriş yapıldı",
    AuthEventKind.LOGIN_FAILED: "Hatalı giriş denemesi",
    AuthEventKind.ACCOUNT_LOCKED: "Hesap geçici olarak kilitlendi",
    AuthEventKind.LOGOUT: "Çıkış yapıldı",
    AuthEventKind.PASSWORD_CHANGED: "Parola değiştirildi",
    AuthEventKind.SESSION_REVOKED: "Bir cihazdaki oturum kapatıldı",
    AuthEventKind.TOKEN_REUSED: "Şüpheli oturum yenileme; oturum güvenlik için kapatıldı",
}


def _client(request: Request) -> auth_service.Client:
    return auth_service.Client(ip=client_ip(request), user_agent=user_agent(request))


class SessionOut(BaseModel):
    id: uuid.UUID
    device: str = Field(description='okunur cihaz adı, ör. "Chrome · Windows"')
    user_agent: str | None
    ip: str | None
    created_at: dt.datetime = Field(description="giriş zamanı")
    last_used_at: dt.datetime
    expires_at: dt.datetime
    is_current: bool = Field(description="bu istek bu oturumdan geliyor")


class RevokedOut(BaseModel):
    revoked: int
    current_revoked: bool = Field(description="true → bu oturum da kapandı, girişe dön")


class AuthEventOut(BaseModel):
    at: dt.datetime
    kind: AuthEventKind
    label: str
    device: str
    ip: str | None


@router.get("/sessions", summary="Açık oturumlarım (cihazlar)")
async def my_sessions(
    current: CurrentUserDep, session: SessionDep, now: NowDep
) -> list[SessionOut]:
    rows = await auth_service.active_sessions(session, user_id=current.user.id, now=now)
    return [
        SessionOut(
            id=r.id,
            device=describe_device(r.user_agent),
            user_agent=r.user_agent,
            ip=r.ip,
            created_at=r.created_at,
            last_used_at=r.last_used_at,
            expires_at=r.expires_at,
            is_current=r.id == current.session_id,
        )
        for r in rows
    ]


@router.delete(
    "/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Bir oturumu kapat"
)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    current: CurrentUserDep,
    session: SessionDep,
    now: NowDep,
) -> Response:
    revoked = await auth_service.revoke_session(
        session,
        user_id=current.user.id,
        session_id=session_id,
        now=now,
        client=_client(request),
    )
    if not revoked:
        raise NotFoundError("Oturum bulunamadı ya da zaten kapalı.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/sessions", summary="Tüm cihazlardan çık")
async def revoke_all_sessions(
    request: Request,
    current: CurrentUserDep,
    session: SessionDep,
    now: NowDep,
    keep_current: Annotated[bool, Query(description="true → bu oturum açık kalır")] = True,
) -> Written[RevokedOut]:
    count = await auth_service.revoke_all_sessions(
        session,
        user_id=current.user.id,
        keep_session_id=current.session_id if keep_current else None,
        now=now,
        client=_client(request),
    )
    if keep_current:
        message = (
            f"{count} cihazdaki oturum kapatıldı; bu cihazda oturumunuz açık."
            if count
            else "Başka cihazda açık oturumunuz yok."
        )
    else:
        message = "Tüm cihazlardan çıkış yapıldı."
    return Written(
        data=RevokedOut(revoked=count, current_revoked=not keep_current), message=message
    )


@router.get("/security-events", summary="Giriş geçmişim (son 50 olay)")
async def my_security_events(current: CurrentUserDep, session: SessionDep) -> list[AuthEventOut]:
    rows = await auth_service.recent_events(session, user_id=current.user.id)
    return [
        AuthEventOut(
            at=r.created_at,
            kind=AuthEventKind(r.kind),
            label=EVENT_LABELS[AuthEventKind(r.kind)],
            device=describe_device(r.user_agent),
            ip=r.ip,
        )
        for r in rows
    ]
