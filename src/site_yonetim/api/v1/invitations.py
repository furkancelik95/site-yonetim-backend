"""Kayıt yöntemleri, davetler ve hane üyeleri — servis istekleri 20 ve 23 (YAP-04, YAP-07).

Herkese açık iki uç (`/public/invitations/{token}`, `…/accept`, oturum yok —
`tests/architecture/test_routes.py` beyaz listesinde): IP başına istek sınırı, belirtecin yalnız
özeti saklanır, yanıt yalnız site/bölüm adı ve telefonun son 4 hanesini taşır.
SMS yok (K5): davet bağlantısı yalnız oluşturma/yeniden gönderme yanıtında bir kez döner;
yönetim kopyalayıp iletir.
"""

import datetime as dt
import uuid
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    FactoryDep,
    NowDep,
    SiteContext,
    TodayDep,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.vehicles import Resident, my_units
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.invitations import (
    INVITE_DAYS_MAX,
    INVITE_DAYS_MIN,
    HouseholdStatus,
    InvitationRelation,
    InvitationStatus,
    effective_status,
    split_full_name,
)
from site_yonetim.domain.members import FORM_ERROR, check_full_name
from site_yonetim.domain.structure import PartyRole
from site_yonetim.domain.validation import normalize_email_address, normalize_tr_mobile
from site_yonetim.models import HouseholdMember, Invitation
from site_yonetim.models.audit import AuditAction, current_actor, record
from site_yonetim.services import invitations as svc
from site_yonetim.services import rate_limit
from site_yonetim.services.vehicles import read_settings, settings

router = APIRouter(prefix="/sites/{slug}", tags=["kayıt yöntemleri ve davetler"])
public_router = APIRouter(prefix="/public/invitations", tags=["herkese açık"])
PeopleManage = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_MANAGE))]
Paging = Annotated[PageParams, Depends()]
PUBLIC_WINDOW = timedelta(minutes=1)
PUBLIC_GET_LIMIT, PUBLIC_POST_LIMIT = 30, 5


def _error(exc: svc.InvitationRuleError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, exc.fields)


def _actor() -> tuple[str | None, uuid.UUID | None]:
    actor = current_actor()
    return (actor.name, actor.user_id) if actor else (None, None)


def _contact_fields(full_name: str | None, phone: str | None) -> tuple[str, str]:
    errors: dict[str, str] = {}
    name = ""
    clean_phone = ""
    try:
        name = check_full_name(full_name)
        split_full_name(name)
    except ValueError:
        errors["full_name"] = "Ad soyad yazın (yalnız harf, 3–80 karakter)."
    try:
        clean_phone = normalize_tr_mobile(phone or "")
    except ValueError:
        errors["phone"] = "Cep telefonu 5 ile başlayan 10 hane olmalı."
    if errors:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, errors)
    return name, clean_phone


# --- Kayıt yöntemleri ----------------------------------------------------------------------


class RegistrationSettingsOut(BaseModel):
    invite_enabled: bool
    qr_enabled: bool
    code_enabled: bool
    owner_can_approve_tenant: bool = Field(description="ortak kararı bekliyor; yalnız saklanır")
    owner_document_required: bool = Field(description="ortak kararı bekliyor; yalnız saklanır")
    invite_valid_days: int
    kvkk_published: bool = Field(description="bilgi bayrağı; canlıya geçişte zorunlu madde")


class RegistrationSettingsIn(BaseModel):
    invite_enabled: bool | None = None
    qr_enabled: bool | None = None
    code_enabled: bool | None = None
    owner_can_approve_tenant: bool | None = None
    owner_document_required: bool | None = None
    invite_valid_days: int | None = None
    kvkk_published: bool | None = None


def _settings_out(config: object) -> RegistrationSettingsOut:
    return RegistrationSettingsOut.model_validate(config, from_attributes=True)


@router.get("/settings/registration", summary="Kayıt yöntemleri")
async def get_registration_settings(ctx: PeopleManage) -> RegistrationSettingsOut:
    return _settings_out(await read_settings(ctx.session))


@router.put("/settings/registration", summary="Kayıt yöntemlerini kaydet")
async def put_registration_settings(
    ctx: PeopleManage, body: RegistrationSettingsIn
) -> Written[RegistrationSettingsOut]:
    days = body.invite_valid_days
    if days is not None and not INVITE_DAYS_MIN <= days <= INVITE_DAYS_MAX:
        raise ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "validation_error",
            FORM_ERROR,
            {"invite_valid_days": f"{INVITE_DAYS_MIN}–{INVITE_DAYS_MAX} gün."},
        )
    config = await settings(ctx.session)
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(config, key, value)
    out = _settings_out(config)
    await ctx.session.commit()
    return Written(data=out, message="Kayıt yöntemleri kaydedildi.")


# --- Davetler (personel) -------------------------------------------------------------------


class InvitationOut(BaseModel):
    id: uuid.UUID
    full_name: str
    phone: str
    email: str | None
    unit_id: uuid.UUID
    unit_name: str
    relation: InvitationRelation
    status: InvitationStatus
    link: str | None = Field(
        description="`/davet/{token}` — yalnız oluşturma/yeniden gönderme yanıtında; "
        "listede null (belirtecin yalnız özeti saklanır)"
    )
    sent_by: str | None
    sent_at: dt.datetime
    expires_at: dt.datetime
    completed_at: dt.datetime | None


def _invitation_out(
    inv: Invitation, unit_name: str, now: dt.datetime, token: str | None = None
) -> InvitationOut:
    return InvitationOut(
        id=inv.id,
        full_name=inv.full_name,
        phone=inv.phone,
        email=inv.email,
        unit_id=inv.unit_id,
        unit_name=unit_name,
        relation=InvitationRelation(inv.relation),
        status=effective_status(inv.status, inv.expires_at, now),
        link=svc.link_of(token) if token else None,
        sent_by=inv.sent_by_name,
        sent_at=inv.sent_at,
        expires_at=inv.expires_at,
        completed_at=inv.completed_at,
    )


async def _unit_names(ctx: SiteContext, unit_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from site_yonetim.services.registrations import unit_names

    return await unit_names(ctx.session, unit_ids)


@router.get("/invitations", summary="Davetler (yeni üstte; hane üyesi davetleri hariç)")
async def list_invitations(
    ctx: PeopleManage,
    paging: Paging,
    now: NowDep,
    status_filter: Annotated[InvitationStatus | None, Query(alias="status")] = None,
) -> Page[InvitationOut]:
    query = select(Invitation).where(Invitation.relation != InvitationRelation.HOUSEHOLD.value)
    if status_filter is InvitationStatus.EXPIRED:
        query = query.where(
            (Invitation.status == InvitationStatus.EXPIRED.value)
            | (Invitation.status.in_(["sent", "opened"]) & (Invitation.expires_at <= now))
        )
    elif status_filter in (InvitationStatus.SENT, InvitationStatus.OPENED):
        query = query.where(Invitation.status == status_filter.value, Invitation.expires_at > now)
    elif status_filter is not None:
        query = query.where(Invitation.status == status_filter.value)
    query = query.order_by(Invitation.sent_at.desc(), Invitation.id.desc())
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    names = await _unit_names(ctx, {r.unit_id for r in rows})
    return Page(
        items=[_invitation_out(r, names.get(r.unit_id, "—"), now) for r in rows],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


class InvitationIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=254)
    unit_id: uuid.UUID | None = None
    relation: Literal["owner", "tenant"] | None = None


@router.post("/invitations", status_code=status.HTTP_201_CREATED, summary="Davet oluştur")
async def create_invitation(
    ctx: PeopleManage, body: InvitationIn, now: NowDep, response: Response
) -> Written[InvitationOut]:
    """Bağlantı üretilir, **gönderilmez** (K5); yanıttaki `link` kopyalanıp iletilir."""
    errors: dict[str, str] = {}
    try:
        name, phone = _contact_fields(body.full_name, body.phone)
    except ApiError as exc:
        errors.update(exc.fields or {})
        name, phone = "", ""
    email = None
    if body.email and body.email.strip():
        try:
            email = normalize_email_address(body.email)
        except ValueError as exc:
            errors["email"] = str(exc)
    if body.relation is None:
        errors["relation"] = "İlişkiyi seçin."
    if body.unit_id is None:
        errors["unit_id"] = "Bölüm seçin."
    if errors:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, errors)
    if body.unit_id is None or body.relation is None:  # pragma: no cover — yukarıda elendi
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR)
    sent_by, _ = _actor()
    try:
        invitation, token, unit_name = await svc.create_invitation(
            ctx.session,
            ctx.site,
            full_name=name,
            phone=phone,
            email=email,
            unit_id=body.unit_id,
            relation=InvitationRelation(body.relation),
            sent_by=sent_by,
            now=now,
        )
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    out = _invitation_out(invitation, unit_name, now, token)
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=out,
        message=(
            f"{name} için davet oluşturuldu. SMS gönderimi henüz yok (K5); bağlantıyı "
            "kopyalayıp kendisine iletin."
        ),
    )


async def _invitation(ctx: SiteContext, invitation_id: uuid.UUID) -> Invitation:
    found = await ctx.session.scalar(
        select(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.relation != InvitationRelation.HOUSEHOLD.value,
        )
        .with_for_update()
    )
    if found is None:
        raise NotFoundError("Davet bulunamadı.")
    return found


@router.post("/invitations/{invitation_id}/resend", summary="Yeni bağlantı (eskisi geçersiz)")
async def resend_invitation(
    ctx: PeopleManage, invitation_id: uuid.UUID, now: NowDep, response: Response
) -> Written[InvitationOut]:
    invitation = await _invitation(ctx, invitation_id)
    sent_by, _ = _actor()
    try:
        token = await svc.resend(ctx.session, invitation, sent_by=sent_by, now=now)
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    names = await _unit_names(ctx, {invitation.unit_id})
    out = _invitation_out(invitation, names.get(invitation.unit_id, "—"), now, token)
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"
    return Written(data=out, message="Yeni davet bağlantısı üretildi; eskisi geçersiz.")


@router.post("/invitations/{invitation_id}/cancel", summary="Daveti iptal et")
async def cancel_invitation(
    ctx: PeopleManage, invitation_id: uuid.UUID, now: NowDep
) -> Written[InvitationOut]:
    invitation = await _invitation(ctx, invitation_id)
    try:
        await svc.cancel(ctx.session, invitation)
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    names = await _unit_names(ctx, {invitation.unit_id})
    out = _invitation_out(invitation, names.get(invitation.unit_id, "—"), now)
    await ctx.session.commit()
    return Written(data=out, message="Davet iptal edildi.")


# --- Herkese açık: davet ------------------------------------------------------------------


async def _limit(
    factory: FactoryDep, request: Request, now: dt.datetime, kind: str, limit: int
) -> None:
    ip = request.client.host if request.client else "unknown"
    allowed = await rate_limit.hit(
        factory, key=f"invitation-{kind}:{ip}", now=now, limit=limit, window=PUBLIC_WINDOW
    )
    if not allowed:
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_requests",
            "Çok fazla deneme yaptınız, biraz sonra tekrar deneyin.",
            headers={"Retry-After": str(int(PUBLIC_WINDOW.total_seconds()))},
        )


class PublicInvitationOut(BaseModel):
    site_name: str
    site_slug: str = Field(
        description="aydınlatma metni bağlantısı için (`/public/privacy-notice/{slug}`)"
    )
    full_name: str
    unit_name: str
    relation: InvitationRelation
    phone_hint: str = Field(description="telefonun son 4 hanesi")
    status: InvitationStatus
    expires_at: dt.datetime
    email_required: bool = Field(
        description="giriş hesabı için e-posta istenir (davette e-posta yoksa)"
    )


def _not_found() -> ApiError:
    return ApiError(
        HTTPStatus.NOT_FOUND,
        "not_found",
        "Davet bağlantısı geçersiz ya da süresi dolmuş. Site yönetiminden yeni davet isteyin.",
    )


@public_router.get("/{token}", summary="Davet bilgisi (oturumsuz)")
async def public_invitation(
    token: str, request: Request, factory: FactoryDep, now: NowDep
) -> PublicInvitationOut:
    await _limit(factory, request, now, "get", PUBLIC_GET_LIMIT)
    site = await svc.site_of_token(factory, token)
    if site is None:
        raise _not_found()
    with site_scope(site.id):
        async with factory() as session, session.begin():
            invitation = await svc.invitation_by_token(session, token, lock=True)
            try:
                invitation = svc.check_usable(invitation, now)
            except svc.InvitationRuleError:
                raise _not_found() from None
            if invitation.status == InvitationStatus.SENT.value:
                invitation.status = InvitationStatus.OPENED.value
                invitation.opened_at = now
            found = await svc.unit_with_block(session, invitation.unit_id)
            unit_name = svc.unit_label(*found) if found else "—"
            from site_yonetim.models import Person

            linked = await session.scalar(
                select(Person.user_id).where(
                    Person.phone == invitation.phone,
                    Person.merged_into_id.is_(None),
                    Person.user_id.is_not(None),
                )
            )
            out = PublicInvitationOut(
                site_name=site.name,
                site_slug=site.slug,
                full_name=invitation.full_name,
                unit_name=unit_name,
                relation=InvitationRelation(invitation.relation),
                phone_hint=invitation.phone[-4:],
                status=InvitationStatus(invitation.status),
                expires_at=invitation.expires_at,
                email_required=invitation.email is None and linked is None,
            )
    return out


class AcceptIn(BaseModel):
    phone: str | None = Field(default=None, max_length=30)
    kvkk_ack: bool = False
    email: str | None = Field(default=None, max_length=254)


class AcceptedOut(BaseModel):
    unit_name: str
    login_email: str = Field(description="giriş için kullanılacak e-posta")
    temporary_password: str | None = Field(
        description="yeni hesabın geçici parolası; yalnız bu yanıtta. Mevcut hesaba "
        "bağlandıysa null"
    )


@public_router.post(
    "/{token}/accept", status_code=status.HTTP_201_CREATED, summary="Daveti kabul et"
)
async def accept_invitation(
    token: str,
    body: AcceptIn,
    request: Request,
    factory: FactoryDep,
    now: NowDep,
    today: TodayDep,
    response: Response,
) -> Written[AcceptedOut]:
    await _limit(factory, request, now, "post", PUBLIC_POST_LIMIT)
    errors: dict[str, str] = {}
    phone = ""
    try:
        phone = normalize_tr_mobile(body.phone or "")
    except ValueError:
        errors["phone"] = "Cep telefonu 5 ile başlayan 10 hane olmalı."
    email = None
    if body.email and body.email.strip():
        try:
            email = normalize_email_address(body.email)
        except ValueError as exc:
            errors["email"] = str(exc)
    if not body.kvkk_ack:
        errors["kvkk_ack"] = "Devam etmek için bilgilendirme yazısını onaylayın."
    if errors:
        raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "validation_error", FORM_ERROR, errors)
    site = await svc.site_of_token(factory, token)
    if site is None:
        raise _not_found()
    with site_scope(site.id):
        async with factory() as session, session.begin():
            invitation = await svc.invitation_by_token(session, token, lock=True)
            try:
                invitation = svc.check_usable(invitation, now)
                accepted = await svc.accept(
                    session, invitation, phone=phone, email=email, today=today, now=now
                )
            except svc.InvitationRuleError as exc:
                raise _error(exc) from None
            record(
                session, AuditAction.UPDATE, "invitations", invitation.id,
                {"status": "completed", "relation": invitation.relation},
            )  # fmt: skip
    response.headers["Cache-Control"] = "no-store"
    message = (
        "Kaydınız tamamlandı. Geçici parolanızla giriş yapın; ilk girişte parolanızı "
        "belirleyeceksiniz."
        if accepted.temporary_password
        else "Kaydınız tamamlandı; mevcut hesabınızla giriş yapabilirsiniz."
    )
    return Written(
        data=AcceptedOut(
            unit_name=accepted.unit_name,
            login_email=accepted.login_email,
            temporary_password=accepted.temporary_password,
        ),
        message=message,
    )


# --- Hane üyeleri ----------------------------------------------------------------------------


class HouseholdPermissions(BaseModel):
    visitors: bool = True
    packages: bool = True
    reservations: bool = False
    requests: bool = False


class HouseholdPermissionsPatch(BaseModel):
    visitors: bool | None = None
    packages: bool | None = None
    reservations: bool | None = None
    requests: bool | None = None


class HouseholdOut(BaseModel):
    id: uuid.UUID
    unit_id: uuid.UUID
    unit_name: str
    full_name: str
    phone: str
    permissions: HouseholdPermissions
    invited_by: str | None
    status: HouseholdStatus
    invite_link: str | None = Field(
        description="`/davet/{token}` — yalnız davet yanıtında; listede null"
    )
    created_at: dt.datetime
    ended_at: dt.datetime | None


def _household_out(m: HouseholdMember, unit_name: str, token: str | None = None) -> HouseholdOut:
    return HouseholdOut(
        id=m.id,
        unit_id=m.unit_id,
        unit_name=unit_name,
        full_name=m.full_name,
        phone=m.phone,
        permissions=HouseholdPermissions(
            visitors=m.can_visitors,
            packages=m.can_packages,
            reservations=m.can_reservations,
            requests=m.can_requests,
        ),
        invited_by=m.invited_by_name,
        status=HouseholdStatus(m.status),
        invite_link=svc.link_of(token) if token else None,
        created_at=m.created_at,
        ended_at=m.ended_at,
    )


class HouseholdIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    permissions: HouseholdPermissions = Field(default_factory=HouseholdPermissions)
    unit_id: uuid.UUID | None = Field(default=None, description="sakin ucunda isteğe bağlı")


async def _household_list(ctx: SiteContext, unit_ids: list[uuid.UUID]) -> list[HouseholdOut]:
    if not unit_ids:
        return []
    rows = list(
        await ctx.session.scalars(
            select(HouseholdMember)
            .where(HouseholdMember.unit_id.in_(unit_ids))
            .order_by(HouseholdMember.created_at.desc())
        )
    )
    names = await _unit_names(ctx, {r.unit_id for r in rows})
    return [_household_out(r, names.get(r.unit_id, "—")) for r in rows]


async def _add_household(
    ctx: SiteContext, unit_id: uuid.UUID, body: HouseholdIn, now: dt.datetime
) -> tuple[HouseholdOut, str]:
    name, phone = _contact_fields(body.full_name, body.phone)
    by, user_id = _actor()
    try:
        member, token, unit_name = await svc.add_household(
            ctx.session,
            ctx.site,
            unit_id,
            full_name=name,
            phone=phone,
            permissions=svc.Permissions(**body.permissions.model_dump()),
            invited_by=by,
            invited_by_user_id=user_id,
            now=now,
        )
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    out = _household_out(member, unit_name, token)
    await ctx.session.commit()
    return out, name


@router.get("/units/{unit_id}/household", summary="Bölümün hane üyeleri (bitenler dahil)")
async def unit_household(ctx: PeopleManage, unit_id: uuid.UUID) -> list[HouseholdOut]:
    return await _household_list(ctx, [unit_id])


@router.post(
    "/units/{unit_id}/household", status_code=status.HTTP_201_CREATED, summary="Hane üyesi davet et"
)
async def add_unit_household(
    ctx: PeopleManage, unit_id: uuid.UUID, body: HouseholdIn, now: NowDep, response: Response
) -> Written[HouseholdOut]:
    if await svc.unit_with_block(ctx.session, unit_id) is None:
        raise NotFoundError("Bölüm bulunamadı.")
    out, name = await _add_household(ctx, unit_id, body, now)
    response.headers["Cache-Control"] = "no-store"
    return Written(
        data=out,
        message=(
            f"{name} hane üyesi olarak davet edildi. SMS gönderimi henüz yok; davet bağlantısını "
            "kendisine iletin."
        ),
    )


async def _member(ctx: SiteContext, member_id: uuid.UUID) -> HouseholdMember:
    found = await ctx.session.scalar(
        select(HouseholdMember).where(HouseholdMember.id == member_id).with_for_update()
    )
    if found is None:
        raise NotFoundError("Hane üyesi bulunamadı.")
    return found


class HouseholdPatch(BaseModel):
    permissions: HouseholdPermissionsPatch


@router.patch("/household/{member_id}", summary="Hane üyesi yetkileri")
async def update_household(
    ctx: PeopleManage, member_id: uuid.UUID, body: HouseholdPatch
) -> Written[HouseholdOut]:
    member = await _member(ctx, member_id)
    if member.status == HouseholdStatus.ENDED.value:
        raise ApiError(HTTPStatus.CONFLICT, "already_ended", "Hane üyeliği zaten bitmiş.")
    for key, value in body.permissions.model_dump(exclude_none=True).items():
        setattr(member, f"can_{key}", value)
    names = await _unit_names(ctx, {member.unit_id})
    await ctx.session.flush()
    out = _household_out(member, names.get(member.unit_id, "—"))
    await ctx.session.commit()
    return Written(data=out, message=f"{member.full_name} yetkileri güncellendi.")


@router.post("/household/{member_id}/end", summary="Hane üyeliğini bitir (silmez)")
async def end_household(
    ctx: PeopleManage, member_id: uuid.UUID, today: TodayDep, now: NowDep
) -> Written[HouseholdOut]:
    member = await _member(ctx, member_id)
    try:
        await svc.end_household(ctx.session, member, today=today, now=now)
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    names = await _unit_names(ctx, {member.unit_id})
    out = _household_out(member, names.get(member.unit_id, "—"))
    await ctx.session.commit()
    return Written(data=out, message=f"{member.full_name} hane üyeliği sonlandırıldı.")


# Sakin: yalnız malik ya da kiracı olduğu bölüme hane üyesi ekler (YAP-04 S4).
HOUSEHOLD_INVITER_ROLES = [PartyRole.OWNER.value, PartyRole.TENANT.value]


@router.get("/resident/household", summary="Sakin: hane üyelerim")
async def resident_household(ctx: Resident, today: TodayDep) -> list[HouseholdOut]:
    units = await my_units(ctx, today, HOUSEHOLD_INVITER_ROLES)
    return await _household_list(ctx, list(units))


@router.post(
    "/resident/household", status_code=status.HTTP_201_CREATED, summary="Sakin: hane üyesi davet et"
)
async def resident_add_household(
    ctx: Resident, body: HouseholdIn, today: TodayDep, now: NowDep, response: Response
) -> Written[HouseholdOut]:
    units = await my_units(ctx, today, HOUSEHOLD_INVITER_ROLES)
    unit_id = body.unit_id or next(iter(units), None)
    if unit_id is None or unit_id not in units:
        raise NotFoundError("Bölümünüz bulunamadı.")
    out, name = await _add_household(ctx, unit_id, body, now)
    response.headers["Cache-Control"] = "no-store"
    return Written(data=out, message=f"{name} davet edildi. Davet bağlantısını kendisine iletin.")


@router.post("/resident/household/{member_id}/end", summary="Sakin: hane üyeliğini bitir")
async def resident_end_household(
    ctx: Resident, member_id: uuid.UUID, today: TodayDep, now: NowDep
) -> Written[HouseholdOut]:
    units = await my_units(ctx, today, HOUSEHOLD_INVITER_ROLES)
    member = await _member(ctx, member_id)
    if member.unit_id not in units:
        raise NotFoundError("Hane üyesi bulunamadı.")
    try:
        await svc.end_household(ctx.session, member, today=today, now=now)
    except svc.InvitationRuleError as exc:
        raise _error(exc) from None
    out = _household_out(member, units[member.unit_id])
    await ctx.session.commit()
    return Written(data=out, message=f"{member.full_name} hane üyeliği sonlandırıldı.")
