"""Platform uçları — docs/06 §2.2. **Yalnız platform yöneticisi; başkası 404.**

Platform yöneticisi müşterilerin borç/sakin verisini görmez; burada yalnız müşteri, site ve
plan yönetimi vardır (docs/05 §5).
"""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from site_yonetim.api.deps import PlatformAdminDep, SessionDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_iban,
    normalize_tax_number,
)
from site_yonetim.models import Organization, Plan, PropertyKind, Site
from site_yonetim.services.platform import PlatformRuleError, create_customer, require_plan
from site_yonetim.services.provisioning import ProvisioningError, provision_site
from site_yonetim.services.sites import available_modules

router = APIRouter(prefix="/platform", tags=["platform"])


def _rule_error(exc: PlatformRuleError | ProvisioningError) -> ApiError:
    if exc.code.endswith("already_exists"):
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


# --- Planlar ------------------------------------------------------------------


class PlanOut(BaseModel):
    id: uuid.UUID
    name: str
    max_units: int | None
    max_storage_mb: int | None
    allowed_modules: list[str]


@router.get("/plans", summary="Planlar")
async def list_plans(
    _: PlatformAdminDep, session: SessionDep, params: Annotated[PageParams, Depends()]
) -> Page[PlanOut]:
    total = await session.scalar(select(func.count()).select_from(Plan)) or 0
    plans = await session.scalars(
        select(Plan)
        .order_by(Plan.sort_order, Plan.name)
        .offset(params.offset)
        .limit(params.page_size)
    )
    items = [
        PlanOut(
            id=p.id,
            name=p.name,
            max_units=p.max_units,
            max_storage_mb=p.max_storage_mb,
            allowed_modules=list(p.allowed_modules),
        )
        for p in plans
    ]
    return Page(items=items, page=params.page, page_size=params.page_size, total=total)


# --- Müşteri ------------------------------------------------------------------


class CustomerCreate(BaseModel):
    name: str = Field(min_length=3, max_length=150)
    tax_number: str | None = None
    plan_id: uuid.UUID
    admin_full_name: str = Field(min_length=3, max_length=120)
    admin_email: str = Field(max_length=254)

    @field_validator("name", "admin_full_name")
    @classmethod
    def _collapse(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("tax_number")
    @classmethod
    def _tax(cls, value: str | None) -> str | None:
        return normalize_tax_number(value) if value else None

    @field_validator("admin_email")
    @classmethod
    def _email(cls, value: str) -> str:
        return normalize_email_address(value)


class CustomerAdminOut(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str


class CustomerOut(BaseModel):
    organization_id: uuid.UUID
    name: str
    tax_number: str | None
    plan_id: uuid.UUID
    admin: CustomerAdminOut
    temporary_password: str = Field(
        description="Yalnız bu yanıtta, bir kez gösterilir. Saklanmaz; güvenli yolla iletin."
    )


@router.post("/customers", status_code=status.HTTP_201_CREATED, summary="Müşteri aç")
async def create_customer_endpoint(
    body: CustomerCreate, _: PlatformAdminDep, session: SessionDep, response: Response
) -> Written[CustomerOut]:
    try:
        created = await create_customer(
            session,
            name=body.name,
            tax_number=body.tax_number,
            plan_id=body.plan_id,
            admin_full_name=body.admin_full_name,
            admin_email=body.admin_email,
        )
    except PlatformRuleError as exc:
        raise _rule_error(exc) from None
    await session.commit()
    response.headers["Cache-Control"] = "no-store"  # geçici parola önbelleğe düşmesin
    org, admin = created.organization, created.admin
    return Written(
        data=CustomerOut(
            organization_id=org.id,
            name=org.name,
            tax_number=org.tax_number,
            plan_id=body.plan_id,
            admin=CustomerAdminOut(user_id=admin.id, email=admin.email, full_name=admin.full_name),
            temporary_password=created.temporary_password,
        ),
        message=(
            f"{org.name} müşterisi açıldı. Geçici parola yalnız bu ekranda bir kez gösterilir; "
            "yetkiliye güvenli bir yolla iletin."
        ),
    )


# --- Site ---------------------------------------------------------------------


class SiteCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    slug: str | None = Field(default=None, max_length=60)
    plan_id: uuid.UUID
    organization_id: uuid.UUID | None = None
    property_kind: PropertyKind = PropertyKind.RESIDENTIAL
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=80)
    district: str | None = Field(default=None, max_length=80)
    iban: str | None = None
    bank_name: str | None = Field(default=None, max_length=120)

    @field_validator("iban")
    @classmethod
    def _iban(cls, value: str | None) -> str | None:
        return normalize_iban(value) if value else None


class SiteOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_id: uuid.UUID | None
    organization_id: uuid.UUID | None
    property_kind: str
    city: str | None
    district: str | None
    iban: str | None
    bank_name: str | None
    modules: list[str]


async def _site_out(session: SessionDep, site: Site) -> SiteOut:
    with site_scope(site.id):
        modules = await available_modules(session, site)
    return SiteOut(
        id=site.id,
        name=site.name,
        slug=site.slug,
        plan_id=site.plan_id,
        organization_id=site.organization_id,
        property_kind=site.property_kind,
        city=site.city,
        district=site.district,
        iban=site.iban,
        bank_name=site.bank_name,
        modules=sorted(m.value for m in modules),
    )


@router.post("/sites", status_code=status.HTTP_201_CREATED, summary="Site aç")
async def create_site_endpoint(
    body: SiteCreate, _: PlatformAdminDep, session: SessionDep
) -> Written[SiteOut]:
    try:
        await require_plan(session, body.plan_id)
        if (
            body.organization_id is not None
            and await session.get(Organization, body.organization_id) is None
        ):
            raise PlatformRuleError(
                "organization_not_found", "organization_id", "Seçilen müşteri bulunamadı."
            )
        site = await provision_site(
            session,
            name=body.name,
            slug=body.slug,
            plan_id=body.plan_id,
            organization_id=body.organization_id,
            property_kind=body.property_kind,
            address=body.address,
            city=body.city,
            district=body.district,
            iban=body.iban,
            bank_name=body.bank_name,
        )
    except (PlatformRuleError, ProvisioningError) as exc:
        raise _rule_error(exc) from None
    out = await _site_out(session, site)
    await session.commit()
    return Written(
        data=out,
        message=f"{site.name} sitesi açıldı. Sıradaki adım: daire listesini Excel'den aktarın.",
    )


class SitePlanUpdate(BaseModel):
    plan_id: uuid.UUID


@router.patch("/sites/{site_id}/plan", summary="Sitenin planını değiştir")
async def change_site_plan(
    site_id: uuid.UUID, body: SitePlanUpdate, _: PlatformAdminDep, session: SessionDep
) -> Written[SiteOut]:
    site = await session.get(Site, site_id)
    if site is None:
        raise NotFoundError
    try:
        plan = await require_plan(session, body.plan_id)
    except PlatformRuleError as exc:
        raise _rule_error(exc) from None
    site.plan_id = plan.id
    await session.flush()
    out = await _site_out(session, site)
    await session.commit()
    # Planın izin vermediği modüller kapanmaz (veri silinmez) ama kullanılamaz hale gelir.
    return Written(
        data=out, message=f"{site.name} sitesinin planı '{plan.name}' olarak değiştirildi."
    )
