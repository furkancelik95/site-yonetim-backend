"""Site kullanıcıları, roller ve rol atamaları — servis isteği 12 + Yazılım Dokümanı PLT-09
(PM kararları P18 çoklu rol ve siteye özel rol, K20 rol sırası).

Yazma uçlarının hepsi `members.manage`. Rol listesi onay kuralı ekranı için `approvals.rules.read`
ile de okunur. Atayan kendi sahip olmadığı izni içeren rolü atayamaz, kaldıramaz, tanımlayamaz
(403 `exceeds_own_permissions`). Yeni kullanıcıya **72 saatlik davet bağlantısı**
gider (P5, PLT-09); geçici parola yok. Test sağlayıcısında (K5) bağlantı geliştirme/demo yanıtında
`preview_url` ile görünür (üretimde asla). Üyelik, rol ve atama değişiklikleri
denetim kaydında (`site_memberships`, `site_roles`, `site_role_assignments`).

Geriye uyum: üyede `role_key` / `role_name` ana rolü (en üst düzeydeki etkin rol) taşır; bütün
roller `roles` dizisinde.
"""

import datetime as dt
import uuid
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    ChannelsDep,
    CurrentUserDep,
    NowDep,
    SettingsDep,
    SiteContext,
    SiteContextDep,
    TodayDep,
    client_ip,
    require_permission,
    user_agent,
)
from site_yonetim.api.schemas import ApprovalPendingOut, Page, PageParams, Written
from site_yonetim.core.config import Settings
from site_yonetim.core.errors import ApiError, ForbiddenError, NotFoundError
from site_yonetim.domain.access import Permission, RoleScope
from site_yonetim.domain.members import FORM_ERROR, FormFieldError
from site_yonetim.domain.roles import (
    RoleDef,
    RoleKind,
    RoleRuleError,
    Sensitivity,
    is_critical,
    permission_groups,
)
from site_yonetim.models import Block, SiteRoleAssignment, User
from site_yonetim.services import members as svc
from site_yonetim.services import role_approvals, user_invitations
from site_yonetim.services import roles as roles_svc
from site_yonetim.services.approvals import ApprovalError, Person
from site_yonetim.services.auth_events import Client
from site_yonetim.services.channels import Channels

router = APIRouter(prefix="/sites/{slug}", tags=["kullanıcılar"])
catalog_router = APIRouter(tags=["kullanıcılar"])
MembersManage = Annotated[SiteContext, Depends(require_permission(Permission.MEMBERS_MANAGE))]


async def _roles_reader(ctx: SiteContextDep) -> SiteContext:
    if not (
        ctx.access.can(Permission.MEMBERS_MANAGE) or ctx.access.can(Permission.APPROVAL_RULES_READ)
    ):
        raise ForbiddenError
    return ctx


RolesRead = Annotated[SiteContext, Depends(_roles_reader)]


def form_error(exc: FormFieldError) -> ApiError:
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, exc.fields)


def rule_error(exc: svc.MemberRuleError | RoleRuleError) -> ApiError:
    if isinstance(exc, RoleRuleError):
        return ApiError(HTTPStatus(exc.status), exc.code, exc.message)
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


def _actor(ctx: SiteContext, current: CurrentUserDep) -> svc.Actor:
    return svc.Actor(current.user.id, current.user.full_name, ctx.access.permissions)


# --- İzin kataloğu ------------------------------------------------------------------------


class PermissionItemOut(BaseModel):
    key: str
    description: str
    sensitivity: Sensitivity = Field(description="normal · sensitive (hassas) · critical (kritik)")


class PermissionGroupOut(BaseModel):
    group: str
    permissions: list[PermissionItemOut]


@catalog_router.get("/permissions", summary="İzin ağacı (grup → izin, hassasiyet)")
async def list_permissions(current: CurrentUserDep) -> list[PermissionGroupOut]:
    return [
        PermissionGroupOut(
            group=name,
            permissions=[
                PermissionItemOut(key=i.key, description=i.description, sensitivity=i.sensitivity)
                for i in items
            ],
        )
        for name, items in permission_groups()
    ]


# --- Roller -------------------------------------------------------------------------------


class RoleOut(BaseModel):
    key: str
    name: str
    description: str
    kind: RoleKind = Field(description="system: hazır şablon (salt okunur) · custom: siteye özel")
    base_key: str | None = Field(description="özel rolün kopyalandığı rol")
    level: int = Field(description="rol sırası: 1 en üst (K20); devir ve eskalasyon buna bakar")
    permissions: list[str]
    is_active: bool = Field(description="false: arşivlenmiş özel rol (atanamaz)")
    is_critical: bool = Field(description="kritik izin içerir (atanması onaydan geçecek: P16b)")
    holder_count: int = Field(description="bu roldeki etkin (tesis kapsamlı) kullanıcı sayısı")

    @classmethod
    def of(cls, role: RoleDef, holders: dict[str, int]) -> RoleOut:
        return cls(
            key=role.key,
            name=role.name,
            description=role.description,
            kind=role.kind,
            base_key=role.base_key,
            level=role.level,
            permissions=sorted(p.value for p in role.permissions),
            is_active=role.is_active,
            is_critical=is_critical(role.permissions),
            holder_count=holders.get(role.key, 0),
        )


async def _holder_counts(ctx: SiteContext, now: dt.datetime) -> dict[str, int]:
    counts: dict[str, int] = {}
    for holder in await roles_svc.holders(ctx.session, ctx.site, now):
        for role in holder.roles:
            counts[role.key] = counts.get(role.key, 0) + 1
    return counts


@router.get("/roles", summary="Roller: hazır şablonlar ve siteye özel roller (sakin hariç)")
async def list_roles(
    ctx: RolesRead,
    now: NowDep,
    include_archived: bool = False,
) -> list[RoleOut]:
    catalog = await roles_svc.load_catalog(ctx.session)
    counts = await _holder_counts(ctx, now)
    return [
        RoleOut.of(r, counts)
        for r in catalog.roles
        if r.is_staff and (r.is_active or include_archived)
    ]


class RoleIn(BaseModel):
    base_key: str = Field(max_length=40, description="kopyalanacak rol (şablon ya da özel)")
    name: str = Field(max_length=100, description="değiştirilemez; onay kurallarında rolü tanıtır")
    description: str | None = Field(default=None, max_length=400)
    permissions: list[str] | None = Field(
        default=None, max_length=100, description="boşsa kopyalanan rolün izinleri"
    )
    level: int | None = Field(default=None, description="boşsa kopyalanan rolün düzeyi")


@router.post("/roles", status_code=status.HTTP_201_CREATED, summary="Kopyala ve özelleştir")
async def create_role(
    body: RoleIn, ctx: MembersManage, current: CurrentUserDep, now: NowDep
) -> Written[RoleOut]:
    try:
        role = await roles_svc.create_role(
            ctx.session, ctx.site,
            roles_svc.RoleInput(
                base_key=body.base_key, name=body.name, description=body.description,
                permissions=body.permissions, level=body.level,
            ),
            actor_permissions=ctx.access.permissions, actor_name=current.user.full_name,
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except RoleRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    return Written(data=RoleOut.of(role, {}), message=f"{role.name} rolü oluşturuldu.")


class RolePatch(BaseModel):
    description: str | None = Field(default=None, max_length=400)
    permissions: list[str] | None = Field(default=None, max_length=100)
    level: int | None = None
    is_active: bool | None = Field(default=None, description="özel rolü arşivle / geri al")


@router.patch("/roles/{role_key}", summary="Özel rolü düzenle; hazır şablonda yalnız düzey (K20)")
async def update_role(
    role_key: str, body: RolePatch, ctx: MembersManage, current: CurrentUserDep, now: NowDep
) -> Written[RoleOut]:
    try:
        role = await roles_svc.update_role(
            ctx.session, ctx.site, role_key,
            roles_svc.RoleInput(
                description=body.description, permissions=body.permissions, level=body.level
            ),
            is_active=body.is_active, actor_permissions=ctx.access.permissions,
            actor_name=current.user.full_name, now=now,
        )  # fmt: skip
    except LookupError:
        raise NotFoundError("Rol bulunamadı.") from None
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except RoleRuleError as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    counts = await _holder_counts(ctx, now)
    message = (
        f"{role.name} rolü arşivlendi."
        if body.is_active is False
        else f"{role.name} rolü kaydedildi."
    )
    return Written(data=RoleOut.of(role, counts), message=message)


# --- Kullanıcılar --------------------------------------------------------------------------


class MemberRoleOut(BaseModel):
    assignment_id: uuid.UUID | None = Field(description="şirketten gelen rolde boş")
    role_key: str
    role_name: str
    level: int
    scope_kind: RoleScope
    block_id: uuid.UUID | None
    block_name: str | None
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    is_current: bool = Field(description="false: ileri tarihte başlayacak")

    @classmethod
    def of(cls, r: svc.MemberRole) -> MemberRoleOut:
        return cls(
            assignment_id=r.assignment_id,
            role_key=r.key,
            role_name=r.name,
            level=r.level,
            scope_kind=r.scope_kind,
            block_id=r.block_id,
            block_name=r.block_name,
            starts_at=r.starts_at,
            ends_at=r.ends_at,
            is_current=r.is_current,
        )


class MemberOut(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str | None
    role_key: str | None = Field(description="ana rol (geriye uyum); rolü yoksa boş")
    role_name: str | None
    roles: list[MemberRoleOut] = Field(description="bitmemiş bütün roller, ana rol önce")
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
        primary = m.primary
        return cls(
            id=m.id,
            full_name=m.user.full_name,
            email=m.user.email,
            role_key=primary.key if primary else None,
            role_name=primary.name if primary else None,
            roles=[MemberRoleOut.of(r) for r in m.roles],
            is_active=m.is_active,
            source=m.source,  # type: ignore[arg-type]
            last_login_at=m.user.last_login_at,
            invited_at=m.invited_at,
            invitation_pending=m.user.password_hash is None and m.user.phone is None,
            mfa_enabled=m.user.mfa_enabled_at is not None,
        )


@router.get("/members", summary="Siteye erişen personel ve rolleri")
async def list_members(ctx: MembersManage, now: NowDep) -> list[MemberOut]:
    return [MemberOut.of(m) for m in await svc.list_members(ctx.session, ctx.site, now)]


class AssignmentFields(BaseModel):
    scope_kind: RoleScope = Field(
        default=RoleScope.SITE, description="site (tesis geneli) · block (kapı: GUV-08 ile)"
    )
    block_id: uuid.UUID | None = Field(default=None, description="yalnız scope_kind=block")
    starts_at: dt.datetime | None = Field(default=None, description="boşsa hemen")
    ends_at: dt.datetime | None = Field(default=None, description="boşsa süresiz")


class MemberIn(AssignmentFields):
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


def _invitation(settings: Settings, sent: user_invitations.SentInvitation) -> MemberInvitationOut:
    return MemberInvitationOut(
        expires_at=sent.invitation.expires_at,
        sent_to=sent.sent_to,
        preview_url=sent.url if settings.test_inbox_active else None,
    )


def _inviter(request: Request, settings: Settings, channels: Channels) -> svc.Inviter:
    return svc.Inviter(
        web_url=settings.web_url,
        channels=channels,
        client=Client(ip=client_ip(request), user_agent=user_agent(request)),
    )


def _assignment(role_key: str | None, body: AssignmentFields) -> roles_svc.AssignmentInput:
    return roles_svc.AssignmentInput(
        role_key=role_key,
        scope_kind=body.scope_kind.value,
        block_id=body.block_id,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )


@router.post("/members", status_code=status.HTTP_201_CREATED, summary="Kullanıcı ekle")
async def add_member(
    body: MemberIn,
    ctx: MembersManage,
    current: CurrentUserDep,
    now: NowDep,
    response: Response,
    request: Request,
    settings: SettingsDep,
    channels: ChannelsDep,
) -> Written[AddedOut]:
    try:
        added = await svc.add_member(
            ctx.session, ctx.site, full_name=body.full_name, email=body.email,
            assignment=_assignment(body.role_key, body), actor=_actor(ctx, current), now=now,
            inviter=_inviter(request, settings, channels),
        )  # fmt: skip
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except (svc.MemberRuleError, RoleRuleError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"  # önizleme bağlantısı önbelleğe düşmesin
    member = added.member
    role_name = member.primary.name if member.primary else ""
    if added.invitation is None:
        message = (
            f"{member.user.full_name} kayıtlı bir kullanıcı; bu sitede {role_name} rolü "
            "verildi. Mevcut hesabıyla giriş yapar."
        )
    else:
        message = (
            f"{member.user.full_name} {role_name} olarak eklendi. 72 saat geçerli davet "
            f"bağlantısı {', '.join(added.invitation.sent_to)} adresine gönderildi; kişi "
            "parolasını bağlantıdan belirler."
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
    current: CurrentUserDep,
    now: NowDep,
    response: Response,
    request: Request,
    settings: SettingsDep,
    channels: ChannelsDep,
) -> Written[MemberInvitationOut]:
    try:
        sent = await svc.resend_invitation(
            ctx.session,
            ctx.site,
            member_id,
            actor=_actor(ctx, current),
            now=now,
            inviter=_inviter(request, settings, channels),
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
    role_key: str | None = Field(
        default=None, max_length=40, description="ana rolü değiştir (diğer tesis rolleri kapanır)"
    )
    is_active: bool | None = None


@router.patch("/members/{member_id}", summary="Ana rolü değiştir, erişimi kapat/aç")
async def update_member(
    member_id: uuid.UUID,
    body: MemberPatch,
    ctx: MembersManage,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[MemberOut]:
    try:
        member, closed = await svc.update_member(
            ctx.session, ctx.site, member_id, role_key=body.role_key, is_active=body.is_active,
            actor=_actor(ctx, current), now=now,
        )  # fmt: skip
    except LookupError:
        raise NotFoundError("Kullanıcı bulunamadı.") from None
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except (svc.MemberRuleError, RoleRuleError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    name = member.user.full_name
    if closed:
        message = f"{name} erişimi kapatıldı; oturumları sonlandırıldı."
    elif body.is_active and member.is_active and body.role_key is None:
        message = f"{name} erişimi yeniden açıldı."
    else:
        role_name = member.primary.name if member.primary else ""
        message = f"{name} rolü {role_name} olarak kaydedildi."
    return Written(data=MemberOut.of(member), message=message)


# --- Rol atamaları ------------------------------------------------------------------------


class RoleAssignmentIn(AssignmentFields):
    role_key: str = Field(max_length=40)


@router.post(
    "/members/{member_id}/role-assignments",
    status_code=status.HTTP_201_CREATED,
    summary="Kişiye rol ata (kapsam ve süre isteğe bağlı)",
    response_model=Written[MemberOut] | Written[ApprovalPendingOut],
    responses={202: {"description": "`status: approval_pending` — kritik rol ataması onayda"}},
)
async def add_role_assignment(
    member_id: uuid.UUID,
    body: RoleAssignmentIn,
    ctx: MembersManage,
    current: CurrentUserDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[MemberOut] | Written[ApprovalPendingOut]:
    """Kritik izin içeren rol için `role_change` onay kuralı tanımlıysa 202 `approval_pending`:
    rol onaylanınca atanır."""
    try:
        member = await svc.get_member(ctx.session, ctx.site, member_id, now)
        request = await role_approvals.request_if_critical(
            ctx.session, ctx.site, user_id=member.user.id, user_name=member.user.full_name,
            data=_assignment(body.role_key, body),
            requester=Person(current.user.id, current.user.full_name),
            actor_permissions=ctx.access.permissions, today=today, now=now,
        )  # fmt: skip
        if request is not None:
            await ctx.session.commit()
            response.status_code = status.HTTP_202_ACCEPTED
            return Written(
                data=ApprovalPendingOut(approval_request_id=request.id),
                message=f"{member.user.full_name} için rol ataması onaya gönderildi (talep "
                f"#{request.number}); onaylanınca atanır.",
            )
        assignment = await roles_svc.assign_role(
            ctx.session, ctx.site, user_id=member.user.id,
            data=_assignment(body.role_key, body), actor_id=current.user.id,
            actor_permissions=ctx.access.permissions, actor_name=current.user.full_name, now=now,
        )  # fmt: skip
    except ApprovalError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    except LookupError:
        raise NotFoundError("Kullanıcı bulunamadı.") from None
    except FormFieldError as exc:
        raise form_error(exc) from exc
    except (svc.MemberRuleError, RoleRuleError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    member = await svc.get_member(ctx.session, ctx.site, member_id, now)
    role = next((r for r in member.roles if r.assignment_id == assignment.id), None)
    when = "" if role is None or role.is_current else " (başlangıç tarihinde etkinleşir)"
    return Written(
        data=MemberOut.of(member),
        message=f"{member.user.full_name} kişisine {role.name if role else ''} rolü atandı{when}.",
    )


@router.delete(
    "/members/{member_id}/role-assignments/{assignment_id}",
    summary="Rolü kaldır (atama bitiş zamanıyla kapanır; geçmiş kalır)",
)
async def end_role_assignment(
    member_id: uuid.UUID,
    assignment_id: uuid.UUID,
    ctx: MembersManage,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[MemberOut]:
    try:
        member = await svc.get_member(ctx.session, ctx.site, member_id, now)
        assignment = await ctx.session.get(SiteRoleAssignment, assignment_id)
        if assignment is None or assignment.user_id != member.user.id:
            raise LookupError
        role_name = next((r.name for r in member.roles if r.assignment_id == assignment.id), "")
        await roles_svc.end_assignment(
            ctx.session, ctx.site, assignment, actor_id=current.user.id,
            actor_permissions=ctx.access.permissions, actor_name=current.user.full_name, now=now,
        )  # fmt: skip
    except LookupError:
        raise NotFoundError("Rol ataması bulunamadı.") from None
    except (svc.MemberRuleError, RoleRuleError) as exc:
        raise rule_error(exc) from exc
    await ctx.session.commit()
    member = await svc.get_member(ctx.session, ctx.site, member_id, now)
    return Written(
        data=MemberOut.of(member),
        message=f"{member.user.full_name} kişisinin {role_name} rolü kaldırıldı.",
    )


class RoleAssignmentHistoryOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str
    role_key: str
    role_name: str
    scope_kind: RoleScope
    block_name: str | None
    starts_at: dt.datetime | None
    ends_at: dt.datetime | None
    assigned_by: str | None
    assigned_at: dt.datetime
    removed_by: str | None
    removed_at: dt.datetime | None


@router.get("/role-assignments", summary="Rol atama geçmişi (kim, kime, hangi rol, ne zaman)")
async def role_assignment_history(
    ctx: MembersManage,
    paging: Annotated[PageParams, Depends()],
    user_id: uuid.UUID | None = None,
) -> Page[RoleAssignmentHistoryOut]:
    query = (
        select(SiteRoleAssignment, User.full_name, Block.name)
        .join(User, User.id == SiteRoleAssignment.user_id)
        .outerjoin(Block, Block.id == SiteRoleAssignment.block_id)
        .order_by(SiteRoleAssignment.created_at.desc(), SiteRoleAssignment.id.desc())
    )
    if user_id is not None:
        query = query.where(SiteRoleAssignment.user_id == user_id)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    catalog = await roles_svc.load_catalog(ctx.session)
    items = []
    for a, user_name, block_name in await ctx.session.execute(
        query.offset(paging.offset).limit(paging.page_size)
    ):
        role = catalog.by_key(a.role_key)
        items.append(
            RoleAssignmentHistoryOut(
                id=a.id,
                user_id=a.user_id,
                user_name=user_name,
                role_key=a.role_key,
                role_name=role.name if role else a.role_key,
                scope_kind=RoleScope(a.scope_kind),
                block_name=block_name,
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                assigned_by=a.assigned_by_name,
                assigned_at=a.created_at,
                removed_by=a.removed_by_name,
                removed_at=a.removed_at,
            )
        )
    return Page(items=items, page=paging.page, page_size=paging.page_size, total=total)


__all__ = ["RoleKind", "catalog_router", "router"]
