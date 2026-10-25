"""Sakinin kendini kaydetmesi ve yönetici onayı — frontend servis isteği 13 (backend issue #41).

Personel uçları `people.manage`. İki uç **herkese açık** (`/public/registration/{code}`, oturum
yok — `tests/architecture/test_routes.py` beyaz listesinde): IP başına istek sınırı var (GET
dakikada 30, POST dakikada 5), kod tahmin edilemez ve yalnız site adı döner.
"""

import datetime as dt
import uuid
from datetime import timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from site_yonetim.api.deps import (
    CurrentUserDep,
    FactoryDep,
    NowDep,
    SiteContext,
    require_permission,
)
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.api.v1.members import form_error
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.access import Permission
from site_yonetim.domain.invitations import SiteLifecycle
from site_yonetim.domain.members import FormFieldError, Relation, check_applicant
from site_yonetim.domain.structure import StructureRuleError
from site_yonetim.models import Registration, Unit
from site_yonetim.models.audit import AuditAction, record
from site_yonetim.services import rate_limit
from site_yonetim.services import registrations as svc
from site_yonetim.services.vehicles import read_settings

router = APIRouter(prefix="/sites/{slug}", tags=["kayıt başvuruları"])
public_router = APIRouter(prefix="/public/registration", tags=["herkese açık"])
PeopleManage = Annotated[SiteContext, Depends(require_permission(Permission.PEOPLE_MANAGE))]
Paging = Annotated[PageParams, Depends()]

PUBLIC_WINDOW = timedelta(minutes=1)
PUBLIC_GET_LIMIT, PUBLIC_POST_LIMIT = 30, 5


def _rule(exc: svc.RegistrationRuleError) -> ApiError:
    if exc.code == "unit_not_found":
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, {"unit_id": exc.message}
        )
    if exc.code == "reason_required":
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, {"reason": exc.message}
        )
    if exc.code == "share_only_for_owner":
        return ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, {"share_percent": exc.message}
        )
    return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)


# --- Kayıt bağlantısı ---------------------------------------------------------------


class LinkOut(BaseModel):
    code: str = Field(description="herkese açık form: /kayit/{code}")
    is_enabled: bool


class LinkPatch(BaseModel):
    is_enabled: bool


@router.get("/registration-link", summary="Kayıt bağlantısı (yoksa açılır)")
async def get_link(ctx: PeopleManage) -> LinkOut:
    found = await svc.link(ctx.session)
    await ctx.session.commit()
    return LinkOut(code=found.code, is_enabled=found.is_enabled)


@router.post("/registration-link/rotate", summary="Yeni kod; eskisi hemen geçersiz")
async def rotate_link(ctx: PeopleManage) -> Written[LinkOut]:
    found = await svc.rotate(ctx.session)
    await ctx.session.commit()
    return Written(
        data=LinkOut(code=found.code, is_enabled=found.is_enabled),
        message="Kayıt bağlantısı yenilendi; eski bağlantı artık çalışmaz.",
    )


@router.patch("/registration-link", summary="Kayda kapat / aç")
async def update_link(body: LinkPatch, ctx: PeopleManage) -> Written[LinkOut]:
    found = await svc.link(ctx.session)
    found.is_enabled = body.is_enabled
    await ctx.session.commit()
    state = "açıldı" if body.is_enabled else "kapatıldı"
    return Written(
        data=LinkOut(code=found.code, is_enabled=found.is_enabled),
        message=f"Kayıt bağlantısı başvurulara {state}.",
    )


# --- Başvurular ---------------------------------------------------------------------


class RegistrationOut(BaseModel):
    id: uuid.UUID
    reference: str = Field(description="`KB-0001`")
    first_name: str
    last_name: str
    phone: str
    email: str | None
    unit_text: str
    relation: Relation
    explicit_consent: bool
    status: Literal["pending", "approved", "rejected"]
    created_at: dt.datetime
    decided_at: dt.datetime | None
    decided_by: str | None
    reject_reason: str | None
    unit_name: str | None
    requested_unit_id: uuid.UUID | None = Field(
        default=None, description="başvuranın listeden seçtiği bölüm (onayda önerilir)"
    )
    requested_unit_name: str | None = None

    @classmethod
    def of(
        cls, r: Registration, unit_name: str | None, requested_name: str | None = None
    ) -> RegistrationOut:
        return cls(
            id=r.id,
            reference=svc.reference(r.number),
            first_name=r.first_name,
            last_name=r.last_name,
            phone=r.phone,
            email=r.email,
            unit_text=r.unit_text,
            relation=Relation(r.relation),
            explicit_consent=r.explicit_consent_at is not None,
            status=r.status,  # type: ignore[arg-type]
            created_at=r.created_at,
            decided_at=r.decided_at,
            decided_by=r.decided_by_name,
            reject_reason=r.reject_reason,
            unit_name=unit_name,
            requested_unit_id=r.requested_unit_id,
            requested_unit_name=requested_name,
        )


@router.get("/registrations", summary="Kayıt başvuruları (yeni üstte)")
async def list_registrations(
    ctx: PeopleManage,
    paging: Paging,
    status_: Annotated[
        Literal["pending", "approved", "rejected"] | None, Query(alias="status")
    ] = None,
) -> Page[RegistrationOut]:
    query = svc.list_query(status_)
    total = await ctx.session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = list(await ctx.session.scalars(query.offset(paging.offset).limit(paging.page_size)))
    ids = {r.unit_id for r in rows if r.unit_id} | {
        r.requested_unit_id for r in rows if r.requested_unit_id
    }
    names = await svc.unit_names(ctx.session, ids)
    return Page(
        items=[
            RegistrationOut.of(
                r,
                names.get(r.unit_id) if r.unit_id else None,
                names.get(r.requested_unit_id) if r.requested_unit_id else None,
            )
            for r in rows
        ],
        page=paging.page,
        page_size=paging.page_size,
        total=total,
    )


async def _registration(ctx: SiteContext, registration_id: uuid.UUID) -> Registration:
    found = await svc.get(ctx.session, registration_id, lock=True)
    if found is None:
        raise NotFoundError("Başvuru bulunamadı.")
    return found


class ApproveIn(BaseModel):
    unit_id: uuid.UUID
    start_date: dt.date
    share_percent: Decimal | None = Field(
        default=None,
        gt=0,
        le=100,
        decimal_places=2,
        description="yalnız malik başvurusunda: yeni malikin hissesi (boşsa %100). Mevcut "
        "maliklerle toplam %100'ü aşarsa 409 owner_shares_exceed",
    )


class ApprovedOut(RegistrationOut):
    temporary_password: str | None = Field(
        description="yeni sakin giriş hesabının geçici parolası; yalnız bu yanıtta (K5'e dek elden)"
    )


@router.post("/registrations/{registration_id}/approve", summary="Onayla: bölüme kişi ekle")
async def approve(
    registration_id: uuid.UUID,
    body: ApproveIn,
    ctx: PeopleManage,
    current: CurrentUserDep,
    now: NowDep,
    response: Response,
) -> Written[ApprovedOut]:
    registration = await _registration(ctx, registration_id)
    try:
        approved = await svc.approve(
            ctx.session,
            registration,
            unit_id=body.unit_id,
            start_date=body.start_date,
            decided_by=current.user.full_name,
            now=now,
            share_percent=body.share_percent,
        )
    except svc.RegistrationRuleError as exc:
        raise _rule(exc) from exc
    except StructureRuleError as exc:  # ör. malik hissesi dolu: owner_shares_exceed
        raise ApiError(HTTPStatus.CONFLICT, exc.code, exc.message) from exc
    record(ctx.session, AuditAction.UPDATE, "registrations", registration.id,
           {"reference": svc.reference(registration.number), "status": "approved"})  # fmt: skip
    await ctx.session.commit()
    response.headers["Cache-Control"] = "no-store"
    r = approved.registration
    relation = svc.RELATION_LABELS[Relation(r.relation)]
    who = f"{r.first_name} {r.last_name} {approved.unit_name} {relation}"
    notes = {
        "created": " Giriş hesabı açıldı; geçici parolayı kişiye elden iletin (SMS/e-posta "
        "gönderimi henüz yok).",
        "linked": " Kişinin mevcut giriş hesabı bu siteye bağlandı.",
        "none": " E-posta verilmediği için giriş hesabı açılmadı.",
    }
    return Written(
        data=ApprovedOut(
            **RegistrationOut.of(r, approved.unit_name).model_dump(),
            temporary_password=approved.temporary_password,
        ),
        message=f"{who} olarak eklendi.{notes[approved.login]}",
    )


class RejectIn(BaseModel):
    reason: str = Field(max_length=500)


@router.post("/registrations/{registration_id}/reject", summary="Gerekçeyle reddet")
async def reject(
    registration_id: uuid.UUID,
    body: RejectIn,
    ctx: PeopleManage,
    current: CurrentUserDep,
    now: NowDep,
) -> Written[RegistrationOut]:
    registration = await _registration(ctx, registration_id)
    try:
        await svc.reject(
            ctx.session,
            registration,
            reason=body.reason,
            decided_by=current.user.full_name,
            now=now,
        )
    except svc.RegistrationRuleError as exc:
        raise _rule(exc) from exc
    record(ctx.session, AuditAction.UPDATE, "registrations", registration.id,
           {"reference": svc.reference(registration.number), "status": "rejected"})  # fmt: skip
    await ctx.session.commit()
    return Written(
        data=RegistrationOut.of(registration, None),
        message=f"{svc.reference(registration.number)} numaralı başvuru reddedildi.",
    )


# --- Herkese açık -------------------------------------------------------------------


def _not_found() -> ApiError:
    return ApiError(
        HTTPStatus.NOT_FOUND,
        "not_found",
        "Kayıt bağlantısı geçersiz ya da kapatılmış. Site yönetiminden yeni bağlantı isteyin.",
    )


async def _limit(
    factory: FactoryDep, request: Request, now: dt.datetime, kind: str, limit: int
) -> None:
    ip = request.client.host if request.client else "unknown"
    allowed = await rate_limit.hit(
        factory, key=f"registration-{kind}:{ip}", now=now, limit=limit, window=PUBLIC_WINDOW
    )
    if not allowed:
        raise ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            "too_many_requests",
            "Çok fazla deneme yaptınız, biraz sonra tekrar deneyin.",
            headers={"Retry-After": str(int(PUBLIC_WINDOW.total_seconds()))},
        )


class PublicUnitOut(BaseModel):
    id: uuid.UUID
    name: str


class PublicBlockOut(BaseModel):
    id: uuid.UUID
    name: str
    units: list[PublicUnitOut]


class PublicSiteOut(BaseModel):
    site_name: str
    site_slug: str
    blocks: list[PublicBlockOut] = Field(
        description="yalnız blok ve bölüm adları — kişi bilgisi yok (KVKK)"
    )
    document_required: bool = Field(
        description="malik başvurusunda belge (ortak kararı bekliyor; yükleme henüz yok)"
    )
    relations: list[Relation]


async def _open_site(factory: FactoryDep, code: str) -> svc.PublicSite:
    """Kod geçerli, site canlı ve QR/site kodu yöntemlerinden biri açık olmalı (istek 23)."""
    site = await svc.site_by_code(factory, code)
    if site is None:
        raise _not_found()
    if site.lifecycle_status != SiteLifecycle.LIVE.value:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "site_not_live",
            "Bu site henüz kayıt kabul etmiyor. Site yönetiminizle iletişime geçin.",
        )
    with site_scope(site.site_id):
        async with factory() as session:
            config = await read_settings(session)
    if not (config.qr_enabled or config.code_enabled):
        raise ApiError(
            HTTPStatus.NOT_FOUND,
            "method_disabled",
            "Bu site bu yöntemle başvuru kabul etmiyor. Site yönetiminizle iletişime geçin.",
        )
    return site


@public_router.get("/{code}", summary="Kayıt formu: site adı, blok/bölüm adları")
async def public_site(
    code: str, request: Request, factory: FactoryDep, now: NowDep
) -> PublicSiteOut:
    await _limit(factory, request, now, "get", PUBLIC_GET_LIMIT)
    site = await _open_site(factory, code)
    with site_scope(site.site_id):
        async with factory() as session:
            blocks = await svc.public_blocks(session)
            config = await read_settings(session)
    return PublicSiteOut(
        site_name=site.name,
        site_slug=site.slug,
        blocks=[
            PublicBlockOut(
                id=b.id,
                name=b.name,
                units=[PublicUnitOut(id=i, name=n) for i, n in b.units],
            )
            for b in blocks
        ],
        document_required=config.owner_document_required,
        relations=[Relation.OWNER, Relation.TENANT],
    )


class ApplicationIn(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    unit_text: str | None = Field(default=None, max_length=100)
    relation: str | None = Field(default=None, max_length=20)
    explicit_consent: bool = False
    kvkk_ack: bool = False
    unit_id: uuid.UUID | None = Field(
        default=None, description="listeden seçilen bölüm (isteğe bağlı; unit_text yine gönderilir)"
    )


class ReferenceOut(BaseModel):
    reference: str


@public_router.post("/{code}", status_code=status.HTTP_201_CREATED, summary="Kayıt başvurusu")
async def public_apply(
    code: str, body: ApplicationIn, request: Request, factory: FactoryDep, now: NowDep
) -> Written[ReferenceOut]:
    await _limit(factory, request, now, "post", PUBLIC_POST_LIMIT)
    site = await _open_site(factory, code)
    try:
        applicant = check_applicant(
            first_name=body.first_name,
            last_name=body.last_name,
            phone=body.phone,
            email=body.email,
            unit_text=body.unit_text,
            relation=body.relation,
            kvkk_ack=body.kvkk_ack,
        )
    except FormFieldError as exc:
        raise form_error(exc) from exc
    ip = request.client.host if request.client else None
    with site_scope(site.site_id):
        async with factory() as session, session.begin():
            unit_id = None
            if body.unit_id is not None:
                found = await session.scalar(
                    select(Unit.id).where(Unit.id == body.unit_id, Unit.is_active)
                )
                if found is None:
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "validation_error",
                        "Formda düzeltilmesi gereken alanlar var.",
                        {"unit_id": "Seçilen bölüm bulunamadı."},
                    )
                unit_id = found
            try:
                registration = await svc.submit(
                    session,
                    applicant,
                    explicit_consent=body.explicit_consent,
                    now=now,
                    ip=ip,
                    unit_id=unit_id,
                )
            except svc.RegistrationRuleError as exc:
                raise _rule(exc) from exc
            ref = svc.reference(registration.number)
    return Written(
        data=ReferenceOut(reference=ref),
        message=(
            f"Başvurunuz alındı ({ref}). Site yönetimi onaylayınca giriş bilgileriniz size "
            "iletilecek."
        ),
    )
