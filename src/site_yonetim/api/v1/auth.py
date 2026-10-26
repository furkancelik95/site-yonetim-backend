"""Kimlik uçları — docs/06 §2.1, docs/05 §8, Yazılım Dokümanı PLT-01, §4.7 (P5).

Erişim jetonu yanıt gövdesinde döner (frontend bellekte tutar). Yenileme jetonu yalnız
httpOnly + SameSite=Lax (+ üretimde Secure) çerezde taşınır; JavaScript okuyamaz.

Giriş yolları:
- Yönetici rolleri: e-posta + parola (`/auth/login`) → 2FA kuruluysa `mfa_required: true` ve
  kod (`/auth/mfa/verify`); kurulu değilse `mfa_setup_deadline` (geçiş süresi) ya da kurulum.
- Sakin ve saha: telefon + SMS kodu (`/auth/otp/request` → `/auth/otp/verify`). Numara kayıtlı
  olsun olmasın yanıt aynıdır; doğrulama sonrası hesap yoksa 404 `no_account` (kayıt başvurusu).
- Parola sıfırlama: e-posta ya da telefona kod (`/auth/password/forgot` → `/reset`).
- Davet: 72 saatlik bağlantıyla parola belirleme (`/auth/invitations/{token}`).
"""

import datetime as dt
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from site_yonetim.api.deps import (
    BUSINESS_TZ,
    AuthenticatedUserDep,
    ChannelsDep,
    CurrentUserDep,
    FactoryDep,
    NowDep,
    PendingUserDep,
    SessionDep,
    SettingsDep,
    client_ip,
    user_agent,
)
from site_yonetim.api.schemas import Written
from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError, UnauthorizedError
from site_yonetim.core.security import InvalidTokenError, create_access_token, decode_access_token
from site_yonetim.domain.login import password_problem
from site_yonetim.domain.masking import mask_email, mask_phone
from site_yonetim.domain.mfa import STEP_UP_WINDOW
from site_yonetim.domain.otp import OtpChannel, OtpPurpose, RequestBlock, VerifyResult
from site_yonetim.domain.validation import normalize_email_address, normalize_phone
from site_yonetim.models import AuthEventKind, User
from site_yonetim.services import auth as auth_service
from site_yonetim.services import invitations as invitations_svc
from site_yonetim.services import login_throttle, rate_limit
from site_yonetim.services import mfa as mfa_svc
from site_yonetim.services import otp as otp_svc
from site_yonetim.services.access import load_user_access

router = APIRouter(prefix="/auth", tags=["kimlik"])

REFRESH_COOKIE = "sy_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
PUBLIC_WINDOW = timedelta(minutes=15)
# IP başına herkese açık kod/davet uçları (SMS pompalamaya karşı; hedef sınırının yanında).
CODE_REQUESTS_PER_IP = 20
CODE_CHECKS_PER_IP = 60


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # Üst sınır: çok uzun parola ile hash maliyeti üzerinden hizmet dışı bırakmaya karşı.
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — şema türü, sır değil
    expires_in: int = Field(description="Erişim jetonunun ömrü, saniye")
    must_change_password: bool = Field(
        description="true: geçici parola — parola değiştirme ekranına yönlendir; değiştirilene "
        "kadar diğer uçlar 403 `password_change_required`"
    )
    mfa_required: bool = Field(
        default=False,
        description="true: iki adımlı kod bekleniyor — `POST /auth/mfa/verify`; o zamana dek "
        "her uç 401 `mfa_required`",
    )
    mfa_setup_deadline: dt.datetime | None = Field(
        default=None,
        description="yönetici rolünde 2FA kurulmamış: bu tarihe kadar uyarıyla girer, sonra "
        "kurulmadan devam edilemez (403 `mfa_setup_required`)",
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


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
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_minutes * 60,
        must_change_password=issued.user.must_change_password,
        mfa_required=issued.auth_session.mfa_pending,
        mfa_setup_deadline=issued.mfa_setup_deadline,
    )


def _clear_cookie(settings: Settings, response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite="lax",
    )


def _client(request: Request) -> auth_service.Client:
    return auth_service.Client(ip=client_ip(request), user_agent=user_agent(request))


def _check_origin(request: Request, settings: Settings) -> None:
    """Çerezle kimlik doğrulayan uçlarda CSRF savunması: tarayıcıdan gelen istek
    yalnız izinli frontend kaynağından olabilir (SameSite=Lax'a ek katman)."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        raise ForbiddenError("Bu kaynaktan gelen isteğe izin verilmiyor.")


async def _limit(
    factory: FactoryDep, request: Request, name: str, limit: int, now: dt.datetime
) -> None:
    key = f"{name}:{client_ip(request) or 'unknown'}"
    if not await rate_limit.hit(factory, key=key, now=now, limit=limit, window=PUBLIC_WINDOW):
        raise auth_service.TooManyAttemptsError(int(PUBLIC_WINDOW.total_seconds()))


async def _open(
    session: SessionDep,
    factory: FactoryDep,
    settings: Settings,
    user: User,
    *,
    now: dt.datetime,
    request: Request,
    response: Response,
) -> TokenResponse:
    """Doğrulanmış kullanıcıya oturum: yönetici rolü 2FA kuralıyla."""
    access = await load_user_access(factory, user, today=now.astimezone(BUSINESS_TZ).date())
    issued = await auth_service.open_session(
        session,
        user,
        now=now,
        session_hours=settings.session_hours,
        client=_client(request),
        mfa_role=mfa_svc.required_for(access),
        mfa_grace_days=settings.mfa_grace_days,
    )
    return _issue(settings, response, issued)


@router.post("/login", summary="Giriş (e-posta + parola)")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> TokenResponse:
    """E-posta + parola. 5 hatalı denemede hesap 15 dakika kilitlenir (429 `account_locked`);
    aynı adresten pencerede çok hatalı deneme 429 `too_many_attempts` (parola denenmez).
    2FA kuruluysa `mfa_required: true` döner; kod `POST /auth/mfa/verify` ile girilir."""
    ip = request.client.host if request.client else None
    async with login_throttle.guard(
        factory,
        ip=ip,
        now=now,
        limit=settings.login_ip_max_failures,
        window=timedelta(minutes=settings.login_ip_window_minutes),
    ):
        user = await auth_service.check_password(
            session, email=body.email, password=body.password, now=now, client=_client(request)
        )
    return await _open(
        session, factory, settings, user, now=now, request=request, response=response
    )


@router.post(
    "/change-password", status_code=status.HTTP_204_NO_CONTENT, summary="Parolayı değiştir"
)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    current: AuthenticatedUserDep,
    session: SessionDep,
    now: NowDep,
) -> Response:
    """Geçici parolada zorunlu; her zaman kullanılabilir. Diğer oturumlar iptal edilir, bu
    oturum sürer. Hatalı mevcut parola hesap kilidi sayacını artırır."""
    await auth_service.change_password(
        session,
        user=current.user,
        current_password=body.current_password,
        new_password=body.new_password,
        keep_session_id=current.session_id,
        now=now,
        client=_client(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
            session,
            refresh_token=refresh_token,
            now=now,
            session_hours=settings.session_hours,
            client=_client(request),
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
    await auth_service.logout(
        session,
        now=now,
        session_id=session_id,
        refresh_token=refresh_token,
        client=_client(request),
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_cookie(settings, response)
    return response


# --- Tek kullanımlık kod (SMS / e-posta) ------------------------------------------------


class CodeSentOut(BaseModel):
    sent_to: str = Field(description="maskeli hedef")
    expires_at: dt.datetime
    resend_after_seconds: int = Field(description="aynı hedefe yeni kod için bekleme")
    test_code: str | None = Field(
        default=None,
        description="yalnız geliştirme/demo (test sağlayıcısı): gönderilen kod. Üretimde her "
        "zaman boş",
    )


def _blocked(exc: otp_svc.CodeBlockedError) -> ApiError:
    headers = {"Retry-After": str(exc.retry_after_seconds)}
    if exc.block is RequestBlock.LOCKED:
        return ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "code_locked",
            "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin.",
            headers=headers,
        )
    if exc.block is RequestBlock.TOO_SOON:
        return ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "code_too_soon",
            f"Yeni kod için {exc.retry_after_seconds} saniye bekleyin.",
            headers=headers,
        )
    return ApiError(
        HTTPStatus.TOO_MANY_REQUESTS,
        "code_daily_limit",
        "Bu adrese bugün en fazla 10 kod gönderilebilir. Daha sonra tekrar deneyin.",
        headers=headers,
    )


def _check_failed(check: otp_svc.CodeCheck) -> ApiError:
    if check.result is VerifyResult.LOCKED:
        return ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "code_locked",
            "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin.",
            headers={"Retry-After": str(max(1, check.retry_after_seconds))},
        )
    if check.result is VerifyResult.EXPIRED:
        message = "Kodun süresi doldu. Yeni kod isteyin."
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, "code_expired", message, fields={"code": message}
        )
    message = f"Kod hatalı. Kalan deneme: {check.remaining}"
    return ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY, "invalid_code", message, fields={"code": message}
    )


def _field_error(field: str, message: str) -> ApiError:
    return ApiError(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "validation_error",
        "Formda düzeltilmesi gereken alanlar var.",
        fields={field: message},
    )


def _phone(value: str, field: str = "phone") -> str:
    try:
        return normalize_phone(value)
    except ValueError as exc:
        raise _field_error(field, str(exc)) from None


def _sent(
    settings: Settings, target: str, channel: OtpChannel, sent: otp_svc.SentCode
) -> CodeSentOut:
    return CodeSentOut(
        sent_to=mask_phone(target) if channel is OtpChannel.SMS else mask_email(target),
        expires_at=sent.expires_at,
        resend_after_seconds=sent.resend_after_seconds,
        test_code=sent.code if settings.test_inbox_active else None,
    )


class OtpRequestIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32, description="E.164 ya da 05XX… (TR)")


@router.post("/otp/request", summary="Telefonla giriş: kod iste")
async def otp_request(
    body: OtpRequestIn,
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[CodeSentOut]:
    """Numara kayıtlı olsun olmasın **aynı yanıt** ve 6 haneli kod (PLT-01 adım 4). Sınırlar:
    60 sn'de bir, günde 10 kod; 5 hatalı denemede 15 dk kilit (429)."""
    await _limit(factory, request, "otp-request", CODE_REQUESTS_PER_IP, now)
    phone = _phone(body.phone)

    user_id = await session.scalar(select(User.id).where(User.phone == phone))
    try:
        sent = await otp_svc.request_code(
            session,
            purpose=OtpPurpose.LOGIN,
            channel=OtpChannel.SMS,
            target=phone,
            user_id=user_id,
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
        message="Kod gönderildi. 3 dakika içinde girin.",
    )


class OtpVerifyIn(BaseModel):
    phone: str = Field(min_length=3, max_length=32)
    code: str = Field(min_length=1, max_length=12)


@router.post("/otp/verify", summary="Telefonla giriş: kodu doğrula")
async def otp_verify(
    body: OtpVerifyIn,
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> TokenResponse:
    """Kod doğruysa oturum açılır. Hesap yoksa 404 `no_account` (kayıt başvurusuna yönlendir);
    hesap kapalıysa 403 `account_disabled`. Hatalı kod 422 `invalid_code` (kalan deneme),
    süresi dolmuş 422 `code_expired`, 5. hata 429 `code_locked`."""
    await _limit(factory, request, "otp-verify", CODE_CHECKS_PER_IP, now)
    phone = _phone(body.phone)
    check = await otp_svc.verify_code(
        session,
        purpose=OtpPurpose.LOGIN,
        target=phone,
        code=body.code,
        now=now,
        key=settings.encryption_key,
        client=_client(request),
    )
    if check.result is not VerifyResult.OK:
        raise _check_failed(check)

    user = await session.scalar(select(User).where(User.phone == phone).with_for_update())
    if user is None:
        await session.rollback()
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "no_account",
            "Bu numarayla kayıtlı hesap yok. Sitenizin kayıt bağlantısından başvurabilirsiniz.",
        )
    if not user.is_active:
        await session.rollback()
        raise ApiError(
            HTTPStatus.FORBIDDEN,
            "account_disabled",
            "Hesabınız kapatılmış. Site yönetiminizle iletişime geçin.",
        )
    return await _open(
        session, factory, settings, user, now=now, request=request, response=response
    )


# --- İki adımlı doğrulama ------------------------------------------------------------


class MfaCodeIn(BaseModel):
    code: str = Field(
        min_length=1, max_length=20, description="doğrulayıcıdaki 6 hane ya da yedek kod"
    )


@router.post("/mfa/verify", summary="Girişte iki adımlı kod")
async def mfa_verify(
    body: MfaCodeIn,
    request: Request,
    current: PendingUserDep,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
) -> TokenResponse:
    """Beklemedeki oturumu tamamlar (TOTP ya da yedek kod). Hatalı kod 422 `invalid_code`;
    5 hatada hesap 15 dk kilitlenir (429 `account_locked`) ve oturum kapanır."""
    auth_session = await mfa_svc.verify_login(
        session,
        user_id=current.user.id,
        session_id=current.session_id,
        code=body.code,
        key=settings.encryption_key,
        now=now,
        session_hours=settings.session_hours,
        client=_client(request),
    )
    token = create_access_token(
        settings, user_id=current.user.id, session_id=auth_session.id, now=now
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_minutes * 60,
        must_change_password=current.user.must_change_password,
    )


class MfaSetupOut(BaseModel):
    secret: str = Field(description="doğrulayıcıya elle girilecek anahtar (Base32)")
    otpauth_uri: str = Field(description="QR kodun içeriği")
    account: str
    issuer: str


@router.post("/mfa/setup", summary="İki adımlı doğrulama kurulumunu başlat")
async def mfa_setup(
    current: AuthenticatedUserDep, settings: SettingsDep, session: SessionDep, response: Response
) -> MfaSetupOut:
    """Yeni sır ve QR içeriği (frontend QR'ı çizer). Etkinleşmesi için `/auth/mfa/enable`."""
    setup = await mfa_svc.start_setup(
        session, current.user, key=settings.encryption_key, issuer=settings.mfa_issuer
    )
    response.headers["Cache-Control"] = "no-store"
    return MfaSetupOut(
        secret=setup.secret,
        otpauth_uri=setup.otpauth_uri,
        account=setup.account,
        issuer=setup.issuer,
    )


class RecoveryCodesOut(BaseModel):
    codes: list[str] = Field(description="yalnız bu yanıtta gösterilir; her biri bir kez")


@router.post("/mfa/enable", summary="İlk kodla iki adımlı doğrulamayı etkinleştir")
async def mfa_enable(
    body: MfaCodeIn,
    request: Request,
    current: AuthenticatedUserDep,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
    response: Response,
) -> Written[RecoveryCodesOut]:
    codes = await mfa_svc.enable(
        session,
        current.user,
        code=body.code,
        key=settings.encryption_key,
        now=now,
        client=_client(request),
        session_id=current.session_id,
    )
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=RecoveryCodesOut(codes=codes),
        message="İki adımlı doğrulama açıldı. Yedek kodları güvenli bir yere kaydedin; "
        "bir daha gösterilmez.",
    )


@router.post("/mfa/recovery-codes", summary="Yedek kodları yenile")
async def mfa_recovery_codes(
    body: MfaCodeIn,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
    response: Response,
) -> Written[RecoveryCodesOut]:
    """Doğrulayıcı koduyla yeni 10 yedek kod; eskilerin hepsi geçersiz olur."""
    codes = await mfa_svc.regenerate_recovery_codes(
        session,
        current.user,
        code=body.code,
        key=settings.encryption_key,
        now=now,
        client=_client(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=RecoveryCodesOut(codes=codes),
        message="Yeni yedek kodlar üretildi; eski kodlar artık geçersiz.",
    )


@router.post(
    "/mfa/disable", status_code=status.HTTP_204_NO_CONTENT, summary="İki adımlı doğrulamayı kapat"
)
async def mfa_disable(
    body: MfaCodeIn,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> Response:
    """Yönetici rolünde kapatılamaz (409 `mfa_required_for_role`)."""
    access = await load_user_access(factory, current.user, today=now.astimezone(BUSINESS_TZ).date())
    await mfa_svc.disable(
        session,
        current.user,
        code=body.code,
        key=settings.encryption_key,
        now=now,
        client=_client(request),
        role_requires=mfa_svc.required_for(access),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Step-up -------------------------------------------------------------------------


class StepUpIn(BaseModel):
    password: str | None = Field(default=None, max_length=256)
    code: str | None = Field(
        default=None, max_length=20, description="2FA kodu, yedek kod ya da SMS kodu"
    )


class StepUpOut(BaseModel):
    verified_at: dt.datetime
    valid_until: dt.datetime = Field(description="hassas işlemler bu ana kadar açık (5 dk)")


@router.post("/step-up/code", summary="Step-up için SMS kodu iste")
async def step_up_code(
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[CodeSentOut]:
    """2FA'sı ve parolası olmayan (telefonla giren) kullanıcı için kayıtlı numaraya kod."""
    phone = current.user.phone
    if phone is None:
        raise ApiError(
            HTTPStatus.CONFLICT, "no_phone", "Hesabınızda telefon yok; parolanızla doğrulayın."
        )
    try:
        sent = await otp_svc.request_code(
            session,
            purpose=OtpPurpose.STEP_UP,
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
    return Written(data=_sent(settings, phone, OtpChannel.SMS, sent), message="Kod gönderildi.")


@router.post("/step-up", summary="Hassas işlem öncesi yeniden doğrula")
async def step_up(
    body: StepUpIn,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    now: NowDep,
) -> Written[StepUpOut]:
    """Parola, 2FA kodu ya da (2FA yoksa) `/auth/step-up/code` ile gelen SMS kodu. Başarılıysa
    oturum 5 dakika boyunca hassas işlemlere açıktır (403 `step_up_required` gelmez)."""
    client = _client(request)
    user = current.user
    if body.code and user.mfa_enabled_at is None and user.phone is not None:
        check = await otp_svc.verify_code(
            session,
            purpose=OtpPurpose.STEP_UP,
            target=user.phone,
            code=body.code,
            now=now,
            key=settings.encryption_key,
            client=client,
        )
        if check.result is not VerifyResult.OK:
            raise _check_failed(check)
        at = await mfa_svc.mark_verified(
            session, user_id=user.id, session_id=current.session_id, now=now, client=client
        )
    else:
        at = await mfa_svc.step_up(
            session,
            user,
            session_id=current.session_id,
            password=body.password,
            code=body.code,
            key=settings.encryption_key,
            now=now,
            client=client,
        )
    return Written(
        data=StepUpOut(verified_at=at, valid_until=at + STEP_UP_WINDOW),
        message="Kimliğiniz doğrulandı.",
    )


# --- Parola sıfırlama ----------------------------------------------------------------


class ForgotIn(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)


def _reset_target(email: str | None, phone: str | None) -> tuple[OtpChannel, str]:
    if phone:
        return OtpChannel.SMS, _phone(phone)
    if email:
        try:
            return OtpChannel.EMAIL, normalize_email_address(email)
        except ValueError as exc:
            raise _field_error("email", str(exc)) from None
    raise _field_error("email", "E-posta adresinizi ya da telefonunuzu girin.")


@router.post("/password/forgot", summary="Parolamı unuttum: kod iste")
async def password_forgot(
    body: ForgotIn,
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    channels: ChannelsDep,
    now: NowDep,
    response: Response,
) -> Written[CodeSentOut]:
    """Hesap olsun olmasın **aynı yanıt** (numaralandırma yok); kod yalnız kayıtlı hesaba
    gider. Sınırlar SMS koduyla aynı."""
    await _limit(factory, request, "password-forgot", CODE_REQUESTS_PER_IP, now)
    channel, target = _reset_target(body.email, body.phone)

    column = User.phone if channel is OtpChannel.SMS else User.email
    user = await session.scalar(select(User).where(column == target))
    exists = user is not None and user.is_active
    try:
        sent = await otp_svc.request_code(
            session,
            purpose=OtpPurpose.PASSWORD_RESET,
            channel=channel,
            target=target,
            user_id=user.id if exists and user else None,
            now=now,
            key=settings.encryption_key,
            channels=channels,
            client=_client(request),
            send=exists,
        )
    except otp_svc.CodeBlockedError as exc:
        raise _blocked(exc) from None
    # Hesap yokken de aynı biçim (test kodu dahil): yanıt hesabın varlığını sızdırmaz.
    out = _sent(settings, target, channel, sent)
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=out,
        message="Bu adrese kayıtlı bir hesap varsa sıfırlama kodu gönderildi.",
    )


class ResetIn(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    code: str = Field(min_length=1, max_length=12)
    new_password: str = Field(min_length=1, max_length=256)


class ResetOut(BaseModel):
    reset: bool = True


@router.post("/password/reset", summary="Kodla yeni parola belirle")
async def password_reset(
    body: ResetIn,
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> Written[ResetOut]:
    """Kod doğruysa parola değişir, hesap kilidi ve geçici parola işareti kalkar, **bütün
    oturumlar** kapanır. Kural ihlali 422 `weak_password`."""
    await _limit(factory, request, "password-reset", CODE_CHECKS_PER_IP, now)
    channel, target = _reset_target(body.email, body.phone)
    problem = password_problem(body.new_password)
    if problem is not None:  # kod harcanmadan önce
        raise auth_service.PasswordChangeError("weak_password", "new_password", problem)
    client = _client(request)
    check = await otp_svc.verify_code(
        session,
        purpose=OtpPurpose.PASSWORD_RESET,
        target=target,
        code=body.code,
        now=now,
        key=settings.encryption_key,
        client=client,
    )
    if check.result is not VerifyResult.OK:
        raise _check_failed(check)

    column = User.phone if channel is OtpChannel.SMS else User.email
    user = await session.scalar(select(User).where(column == target).with_for_update())
    if user is None or not user.is_active or check.user_id != user.id:
        await session.rollback()
        raise _check_failed(otp_svc.CodeCheck(VerifyResult.EXPIRED))
    await auth_service.set_new_password(
        session,
        user=user,
        new_password=body.new_password,
        now=now,
        client=client,
        kind=AuthEventKind.PASSWORD_RESET,
    )
    await session.commit()
    return Written(
        data=ResetOut(), message="Parolanız yenilendi. Yeni parolanızla giriş yapabilirsiniz."
    )


# --- Davet ----------------------------------------------------------------------------


class InvitationInfoOut(BaseModel):
    full_name: str
    login: str = Field(description="maskeli giriş e-postası / telefonu")
    site_name: str | None
    role_label: str | None
    invited_by_name: str | None
    expires_at: dt.datetime
    status: Literal["pending", "accepted", "expired"]


@router.get("/invitations/{token}", summary="Hesap davetini göster")
async def invitation_info(
    token: str, request: Request, session: SessionDep, factory: FactoryDep, now: NowDep
) -> InvitationInfoOut:
    """Herkese açık; IP başına sınırlı. Bilinmeyen/iptal jeton 404."""
    await _limit(factory, request, "invitation", CODE_CHECKS_PER_IP, now)
    found = await invitations_svc.find(session, token[:200], now=now)
    if found is None:
        raise NotFoundError("Davet bulunamadı ya da geçersiz.")
    user = found.user
    login_hint = mask_email(user.email) if user.email else mask_phone(user.phone or "")
    return InvitationInfoOut(
        full_name=user.full_name,
        login=login_hint,
        site_name=found.invitation.site_name,
        role_label=found.invitation.role_label,
        invited_by_name=found.invitation.invited_by_name,
        expires_at=found.invitation.expires_at,
        status=found.status,  # type: ignore[arg-type]
    )


class InvitationAcceptIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@router.post("/invitations/{token}/accept", summary="Daveti kabul et: parola belirle")
async def invitation_accept(
    token: str,
    body: InvitationAcceptIn,
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: SessionDep,
    factory: FactoryDep,
    now: NowDep,
) -> TokenResponse:
    """Parola belirlenir ve oturum açılır. Süresi dolmuş/kullanılmış davet 410 `invitation_gone`."""
    await _limit(factory, request, "invitation", CODE_CHECKS_PER_IP, now)
    found = await invitations_svc.lock_pending(session, token[:200], now=now)
    if found is None:
        await session.rollback()
        raise ApiError(
            HTTPStatus.GONE,
            "invitation_gone",
            "Davetin süresi dolmuş ya da kullanılmış. Site yönetiminden yeni davet isteyin.",
        )
    client = _client(request)
    await auth_service.set_new_password(
        session,
        user=found.user,
        new_password=body.password,
        now=now,
        client=client,
        kind=AuthEventKind.PASSWORD_CHANGED,
    )
    invitations_svc.mark_accepted(session, found, now=now, client=client)
    await session.flush()
    return await _open(
        session, factory, settings, found.user, now=now, request=request, response=response
    )
