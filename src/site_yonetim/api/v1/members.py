"""Site kullanıcıları ve roller — frontend servis isteği 12 (backend issue #40).

Tümü `members.manage` (Yönetici). Roller sabittir (docs/05 §3); burada kişiye rol verilir.
Geçici parola yalnız ekleme yanıtında döner (`Cache-Control: no-store`); loga ve denetim kaydına
yazılmaz. Üyelik değişiklikleri denetim kaydında (`site_memberships`).
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field

from site_yonetim.api.deps import (
    CurrentUserDep,
    NowDep,
    SiteContext,
    require_permission,
)
from site_yonetim.api.schemas import Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.domain.access import Permission
from site_yonetim.domain.members import FORM_ERROR, STAFF_ROLES, FormFieldError
from site_yonetim.services import members as svc

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
    email: str
    role_key: str
    role_name: str
    is_active: bool
    source: Literal["site", "organization"] = Field(
        description="organization: yönetim şirketinden gelir, bu ekrandan değiştirilemez"
    )
    last_login_at: dt.datetime | None
    invited_at: dt.datetime

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


class AddedOut(BaseModel):
    member: MemberOut
    temporary_password: str | None = Field(
        description="yalnız yeni kullanıcıda, yalnız bu yanıtta; kişiye elden iletilir (K5)"
    )


@router.post("/members", status_code=status.HTTP_201_CREATED, summary="Kullanıcı ekle")
async def add_member(body: MemberIn, ctx: MembersManage, response: Response) -> Written[AddedOut]:
    try:
        added = await svc.add_member(
            ctx.session,
            ctx.site,
            full_name=body.full_name,
            email=body.email,
            role_key=body.role_key,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except svc.MemberRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"  # geçici parola önbelleğe düşmesin
    member = added.member
    who = f"{member.user.full_name} {member.role.value}"
    if added.temporary_password is None:
        message = (
            f"{member.user.full_name} kayıtlı bir kullanıcı; bu sitede {member.role.value} rolü "
            "verildi. Mevcut parolasıyla giriş yapar."
        )
    else:
        message = (
            f"{who} olarak eklendi. Geçici parolayı kişiye elden iletin; ilk girişte değiştirecek."
        )
    return Written(
        data=AddedOut(member=MemberOut.of(member), temporary_password=added.temporary_password),
        message=message,
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
