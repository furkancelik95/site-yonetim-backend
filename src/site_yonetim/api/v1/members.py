"""Site kullanıcıları ve roller — frontend servis isteği 12 (backend issue #40).

Tümü `members.manage` (Yönetici). Roller sabittir (docs/05 §3); burada kişiye rol verilir.
Yeni kullanıcıya **72 saatlik davet bağlantısı** gider (P5, PLT-09); geçici parola yok. Test
sağlayıcısında (K5) bağlantı geliştirme/demo yanıtında `preview_url` ile görünür (üretimde asla).
Üyelik değişiklikleri denetim kaydında (`site_memberships`).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    ChannelsDep,
    CurrentUser,
    CurrentUserDep,
    NowDep,
    SettingsDep,
    SiteContext,
    client_ip,
    require_permission,
    user_agent,
)
from site_yonetim.api.schemas import Written
from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FORM_ERROR, STAFF_ROLES, FormFieldError
from site_yonetim.services import invitations
from site_yonetim.services import members as svc
from site_yonetim.services.auth_events import Client
from site_yonetim.services.channels import Channels

router = APIRouter(prefix="/sites/{slug}", tags=["kullanıcılar"])
MembersManage = Annotated[SiteContext, Depends(require_permission(Permission.MEMBERS_MANAGE))]


def form_error(exc: FormFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def rule_error(exc: svc.MemberRuleError) -> ApiError:
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


class RoleOut(BaseModel):
    key: str
    name: str
    description: str


class MemberOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str | None
    role_key: str
    role_name: str
    is_active: bool
    source: Literal["site", "organization"] = Field(
        description="organization: yönetim şirketinden gelir, bu ekrandan değiştirilemez"
    )
    last_login_at: dt.datetime | None
    invited_at: dt.datetime
    invitation_pending: bool = Field(
        default=False, description="parolasını henüz belirlemedi (davet bekliyor)"
    )
    mfa_enabled: bool = Field(default=False, description="iki adımlı doğrulama kurulu")

    @classmethod
    def of(cls, m: svc.Member) -> MemberOut:
        return cls(
            id=m.id,
            full_name=m.user.full_name,
            email=m.user.email,
            role_key=svc.role_key(m.role),
            role_name=m.role.value,
            is_active=m.is_active,
            source=m.source,  # type: ignore[arg-type]
            last_login_at=m.user.last_login_at,
            invited_at=m.invited_at,
            invitation_pending=m.user.password_hash is None and m.user.phone is None,
            mfa_enabled=m.user.mfa_enabled_at is not None,
        )


@router.get("/roles", summary="Sabit roller (ad + kısa açıklama)")
async def list_roles(ctx: MembersManage) -> list[RoleOut]:
    return [RoleOut(key=r.key, name=r.role.value, description=r.description) for r in STAFF_ROLES]


@router.get("/members", summary="Siteye erişen personel")
async def list_members(ctx: MembersManage) -> list[MemberOut]:
    return [MemberOut.of(m) for m in await svc.list_members(ctx.session, ctx.site)]


class MemberIn(BaseModel):
    full_name: str = Field(max_length=200)
    email: str = Field(max_length=320)
    role_key: str = Field(max_length=40)


class MemberInvitationOut(BaseModel):
    expires_at: dt.datetime
    sent_to: list[str] = Field(description="maskeli e-posta / telefon")
    preview_url: str | None = Field(
        default=None,
        description="yalnız geliştirme/demo (test sağlayıcısı): davet bağlantısı. Üretimde boş",
    )
    test_provider: bool = True


class AddedOut(BaseModel):
    member: MemberOut
    invitation: MemberInvitationOut | None = Field(
        description="yalnız yeni kullanıcıda: 72 saat geçerli davet gönderildi"
    )


def _invitation(settings: Settings, sent: invitations.SentInvitation) -> MemberInvitationOut:
    return MemberInvitationOut(
        expires_at=sent.invitation.expires_at,
        sent_to=sent.sent_to,
        preview_url=sent.url if settings.test_inbox_active else None,
    )


def _inviter(
    request: Request, current: CurrentUser, now: dt.datetime, settings: Settings, channels: Channels
) -> svc.Inviter:
    return svc.Inviter(
        name=current.user.full_name,
        now=now,
        web_url=settings.web_url,
        channels=channels,
        client=Client(ip=client_ip(request), user_agent=user_agent(request)),
    )


@router.post("/members", status_code=status.HTTP_201_CREATED, summary="Kullanıcı ekle")
async def add_member(
    body: MemberIn,
    ctx: MembersManage,
    response: Response,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[AddedOut]:
    try:
        added = await svc.add_member(
            ctx.session,
            ctx.site,
            full_name=body.full_name,
            email=body.email,
            role_key=body.role_key,
            inviter=_inviter(request, current, now, settings, channels),
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.MemberRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"  # önizleme bağlantısı önbelleğe düşmesin
    member = added.member
    who = f"{member.user.full_name} {member.role.value}"
    if added.invitation is None:
        message = (
            f"{member.user.full_name} kayıtlı bir kullanıcı; bu sitede {member.role.value} rolü "
            "verildi. Mevcut hesabıyla giriş yapar."
        )
    else:
        message = (
            f"{who} olarak eklendi. 72 saat geçerli davet bağlantısı "
            f"{', '.join(added.invitation.sent_to)} adresine gönderildi; kişi parolasını "
            "bağlantıdan belirler."
        )
    return Written(
        data=AddedOut(
            member=MemberOut.of(member),
            invitation=_invitation(settings, added.invitation) if added.invitation else None,
        ),
        message=message,
    )


@router.post(
    "/members/{member_id}/resend-invitation", summary="Daveti yeniden gönder (eskisi geçersiz)"
)
async def resend_invitation(
    member_id: uuid.UUID,
    ctx: MembersManage,
    response: Response,
    request: Request,
    current: CurrentUserDep,
    settings: SettingsDep,
    channels: ChannelsDep,
    now: NowDep,
) -> Written[MemberInvitationOut]:
    try:
        sent = await svc.resend_invitation(
            ctx.session,
            ctx.site,
            member_id,
            inviter=_inviter(request, current, now, settings, channels),
        )
    except LookupError:
        raise NotFoundError("Kullanıcı bulunamadı.") from None
    except svc.MemberRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=_invitation(settings, sent),
        message=f"Yeni davet bağlantısı {', '.join(sent.sent_to)} adresine gönderildi; "
        "önceki bağlantı geçersiz.",
    )


class MemberPatch(BaseModel):
    role_key: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None


@router.patch("/members/{member_id}", summary="Rolü değiştir, erişimi kapat/aç")
async def update_member(
    member_id: uuid.UUID,
    body: MemberPatch,
    ctx: MembersManage,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[MemberOut]:
    try:
        member, closed = await svc.update_member(
            ctx.session,
            ctx.site,
            member_id,
            role_key=body.role_key,
            is_active=body.is_active,
            actor_id=current.user.id,
            now=now,
        )
    except LookupError:
        raise NotFoundError("Kullanıcı bulunamadı.") from None
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.MemberRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    name = member.user.full_name
    if closed:
        message = f"{name} erişimi kapatıldı; oturumları sonlandırıldı."
    elif body.is_active and member.is_active and body.role_key is None:
        message = f"{name} erişimi yeniden açıldı."
    else:
        message = f"{name} rolü {member.role.value} olarak kaydedildi."
    return Written(data=MemberOut.of(member), message=message)
