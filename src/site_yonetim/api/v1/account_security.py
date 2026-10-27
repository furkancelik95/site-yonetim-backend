"""Profil › Güvenlik — Yazılım Dokümanı PLT-10 (telefon, iki adımlı doğrulama, yedek kodlar).

- `GET /me/security`: telefon, e-posta, parola var mı, 2FA durumu ve geçiş süresi, kalan yedek
  kod, son telefon değişikliği.
- Telefon değişikliği: yeni numaraya kod → doğrulayınca numara güncellenir, eski numaraya
  bilgilendirme SMS'i gider, 24 saat finansal işlemlerde step-up istenir (§4.7). Numara başka
  hesaptaysa bu ancak kod doğrulandıktan sonra söylenir (numaralandırma yok).
"""

import datetime as dt
from http import HTTPStatus

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import (
    BUSINESS_TZ,
    AuthenticatedUserDep,
    ChannelsDep,
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SessionDep,
    SettingsDep,
    client_ip,
    user_agent,
)
from site_yonetim.api.schemas import Written
from site_yonetim.api.v1.auth import CodeSentOut, _blocked, _check_failed, _phone, _sent
from site_yonetim.core.errors import ApiError
from site_yonetim.domain.mfa import PHONE_CHANGE_STEP_UP, MfaStatus
from site_yonetim.domain.notifications import NotificationChannel
from site_yonetim.domain.otp import OtpChannel, OtpPurpose, VerifyResult
from site_yonetim.models import AuthEventKind, User
from site_yonetim.services import mfa as mfa_svc
from site_yonetim.services import otp as otp_svc
from site_yonetim.services.access import load_user_access
from site_yonetim.services.auth_events import Client, event
from site_yonetim.services.channels import OutboundMessage

router = APIRouter(prefix="/me", tags=["kimlik"])


def _client(request: Request) -> Client:
    return Client(ip=client_ip(request), user_agent=user_agent(request))


class MfaStatusOut(BaseModel):
    enabled: bool
    enabled_at: dt.datetime | None
    required: bool = Field(description="yönetici rolü: 2FA zorunlu")
    setup_deadline: dt.datetime | None = Field(
        description="zorunlu ve kurulmamışsa geçiş süresinin sonu"
    )
    setup_required: bool = Field(description="true: süre doldu, kurulmadan devam edilemez")
    recovery_codes_left: int


class AccountSecurityOut(BaseModel):
    email: str | None
    phone: str | None
    phone_changed_at: dt.datetime | None
    has_password: bool
    mfa: MfaStatusOut


async def mfa_status(
    current_user: User, factory: FactoryDep, session: SessionDep, now: dt.datetime
) -> MfaStatusOut:
    access = await load_user_access(factory, current_user, today=now.astimezone(BUSINESS_TZ).date())
    required = mfa_svc.required_for(access)
    state = MfaStatus(
        required, current_user.mfa_enabled_at is not None, current_user.mfa_grace_until
    )
    return MfaStatusOut(
        enabled=state.enabled,
        enabled_at=current_user.mfa_enabled_at,
        required=required,
        setup_deadline=current_user.mfa_grace_until if state.setup_pending() else None,
        setup_required=state.setup_blocking(now),
        recovery_codes_left=await mfa_svc.recovery_codes_left(session, current_user.id),
    )


@router.get("/security", summary="Güvenlik ayarlarım")
async def my_security(
    current: AuthenticatedUserDep, factory: FactoryDep, session: SessionDep, now: NowDep
) -> AccountSecurityOut:
    """2FA kurulumu beklerken de çalışır (kurulum ekranı buradan beslenir)."""
    user = current.user
    return AccountSecurityOut(
        email=user.email,
        phone=user.phone,
        phone_changed_at=user.phone_changed_at,
        has_password=user.password_hash is not None,
        mfa=await mfa_status(user, factory, session, now),
    )


class PhoneChangeIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32)


@router.post("/phone-change", summary="Telefon değişikliği: yeni numaraya kod")
async def phone_change_request(
    body: PhoneChangeIn,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    channels: ChannelsDep,
    now: NowDep,
    response: Response,
) -> Written[CodeSentOut]:
    phone = _phone(body.phone)
    if phone == current.user.phone:
        raise ApiError(
            HTTPStatus.CONFLICT, "phone_unchanged", "Bu numara zaten hesabınızda kayıtlı."
        )
    try:
        sent = await otp_svc.request_code(
            session,
            purpose=OtpPurpose.PHONE_CHANGE,
            channel=OtpChannel.SMS,
            target=phone,
            user_id=current.user.id,
            now=now,
            key=settings.encryption_key,
            channels=channels,
            client=_client(request),
        )
    except otp_svc.CodeBlockedError as exc:
        raise _blocked(exc) from None
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=_sent(settings, phone, OtpChannel.SMS, sent),
        message="Yeni numaranıza doğrulama kodu gönderildi.",
    )


class PhoneChangeConfirmIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    code: str = Field(min_length=1, max_length=12)


class PhoneChangedOut(BaseModel):
    phone: str
    step_up_until: dt.datetime = Field(description="bu ana dek finansal işlemlerde ek doğrulama")


@router.post("/phone-change/confirm", summary="Telefon değişikliği: kodu doğrula")
async def phone_change_confirm(
    body: PhoneChangeConfirmIn,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[PhoneChangedOut]:
    phone = _phone(body.phone)
    client = _client(request)
    check = await otp_svc.verify_code(
        session,
        purpose=OtpPurpose.PHONE_CHANGE,
        target=phone,
        code=body.code,
        now=now,
        key=settings.encryption_key,
        client=client,
    )
    if check.result is not VerifyResult.OK or check.user_id != current.user.id:
        raise _check_failed(
            check
            if check.result is not VerifyResult.OK
            else otp_svc.CodeCheck(VerifyResult.EXPIRED)
        )
    taken = await session.scalar(
        select(User.id).where(User.phone == phone, User.id != current.user.id)
    )
    if taken is not None:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "phone_taken",
            "Bu numara başka bir hesapta kayıtlı. Site yönetiminizle iletişime geçin.",
        )
    user = await session.scalar(select(User).where(User.id == current.user.id).with_for_update())
    if user is None:  # pragma: no cover
        raise LookupError
    old = user.phone
    user.phone = phone
    user.phone_changed_at = now
    event(session, AuthEventKind.PHONE_CHANGED, client, user_id=user.id)
    await session.commit()
    if old:
        await channels.sms.send(
            OutboundMessage(
                channel=NotificationChannel.SMS,
                to=old,
                body="Hesabınızın telefon numarası değiştirildi. Bu işlemi siz yapmadıysanız "
                "site yönetiminize hemen bildirin.",
            ),
            now=now,
        )
    return Written(
        data=PhoneChangedOut(phone=phone, step_up_until=now + PHONE_CHANGE_STEP_UP),
        message="Telefon numaranız güncellendi. 24 saat boyunca finansal işlemlerde ek "
        "doğrulama istenir.",
    )
