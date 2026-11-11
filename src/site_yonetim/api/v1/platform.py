"""Platform uçları — docs/06 §2.2. **Yalnız platform yöneticisi; başkası 404.**

Platform yöneticisi müşterilerin borç/sakin verisini görmez; burada yalnız müşteri, site ve
plan yönetimi vardır (docs/05 §5).
"""

import uuid
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, func, select

from site_yonetim.api.deps import FactoryDep, PlatformAdminDep, SessionDep
from site_yonetim.api.schemas import Page, PageParams, Written
from site_yonetim.core.errors import ApiError, NotFoundError
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ORGANIZATION_PLAN_MODULE, PlanScope, plan_scope
from site_yonetim.domain.text import tr_lower
from site_yonetim.domain.validation import (
    normalize_email_address,
    normalize_iban,
    normalize_tax_number,
)
from site_yonetim.models import Organization, Plan, PropertyKind, Site
from site_yonetim.models.platform import site_name_key
from site_yonetim.services import portfolio
from site_yonetim.services.platform import PlatformRuleError, create_customer, require_plan
from site_yonetim.services.provisioning import (
    SITE_NAME_MAX,
    SITE_NAME_MIN,
    ProvisioningError,
    provision_site,
)
from site_yonetim.services.sites import available_modules

router = APIRouter(prefix="/platform", tags=["platform"])


def _rule_error(exc: PlatformRuleError | ProvisioningError) -> ApiError:
    if exc.code.endswith("already_exists"):
        return ApiError(HTTPStatus.CONFLICT, exc.code, exc.message)
    fields = {exc.field: exc.message} if exc.field else None
    return ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, exc.code, exc.message, fields)


# --- Özet (docs/06 §2.2) ---------------------------------------------------------


class SiteUsageOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    organization_name: str | None
    plan_name: str | None
    units: int
    max_units: int | None
    over_cap: bool = Field(description="bölüm sayısı plan tavanını aşmış")


class CustomerUsageOut(BaseModel):
    id: uuid.UUID
    name: str
    plan_name: str | None
    site_count: int
    units: int


class OverviewOut(BaseModel):
    customer_count: int
    site_count: int
    unit_count: int
    over_cap_sites: list[SiteUsageOut]
    customers: Page[CustomerUsageOut]
    sites: Page[SiteUsageOut]


def _site_usage(u: portfolio.SiteUsage) -> SiteUsageOut:
    return SiteUsageOut(
        id=u.site.id,
        name=u.site.name,
        slug=u.site.slug,
        organization_name=u.organization_name,
        plan_name=u.plan_name,
        units=u.units,
        max_units=u.max_units,
        over_cap=u.over_cap,
    )


@router.get("/overview", summary="Platform özeti (yalnız kullanım ölçüsü)")
async def overview(
    _: PlatformAdminDep, factory: FactoryDep, params: Annotated[PageParams, Depends()]
) -> OverviewOut:
    """Müşteri, site, bölüm sayıları ve tavanı aşan siteler. Borç ya da sakin verisi yok."""
    data = await portfolio.overview(factory)
    window = slice(params.offset, params.offset + params.page_size)
    return OverviewOut(
        customer_count=data.customer_count,
        site_count=data.site_count,
        unit_count=data.unit_count,
        over_cap_sites=[_site_usage(u) for u in data.over_cap],
        customers=Page(
            items=[
                CustomerUsageOut(
                    id=c.organization.id,
                    name=c.organization.name,
                    plan_name=c.plan_name,
                    site_count=c.site_count,
                    units=c.units,
                )
                for c in data.customers[window]
            ],
            page=params.page,
            page_size=params.page_size,
            total=data.customer_count,
        ),
        sites=Page(
            items=[_site_usage(u) for u in data.sites[window]],
            page=params.page,
            page_size=params.page_size,
            total=data.site_count,
        ),
    )


# --- Planlar ------------------------------------------------------------------


class PlanOut(BaseModel):
    id: uuid.UUID
    name: str
    max_units: int | None
    max_storage_mb: int | None
    allowed_modules: list[str]
    scope: PlanScope = Field(
        description="site: siteye atanır · organization: yönetim şirketi planı (portföy), "
        "siteye atanamaz"
    )


def _plan_filter(scope: PlanScope | None) -> list[ColumnElement[bool]]:
    if scope is None:
        return []
    has_portfolio = Plan.allowed_modules.contains([ORGANIZATION_PLAN_MODULE.value])
    return [has_portfolio if scope is PlanScope.ORGANIZATION else ~has_portfolio]


@router.get("/plans", summary="Planlar")
async def list_plans(
    _: PlatformAdminDep,
    session: SessionDep,
    params: Annotated[PageParams, Depends()],
    scope: Annotated[
        PlanScope | None,
        Query(description="site → yalnız siteye atanabilen planlar (site formu); boş → hepsi"),
    ] = None,
) -> Page[PlanOut]:
    where = _plan_filter(scope)
    total = await session.scalar(select(func.count()).select_from(Plan).where(*where)) or 0
    plans = await session.scalars(
        select(Plan)
        .where(*where)
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
            scope=plan_scope(p.allowed_modules),
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
            admin=CustomerAdminOut(
                user_id=admin.id, email=admin.email or "", full_name=admin.full_name
            ),
            temporary_password=created.temporary_password,
        ),
        message=(
            f"{org.name} müşterisi açıldı. Geçici parola yalnız bu ekranda bir kez gösterilir; "
            "yetkiliye güvenli bir yolla iletin."
        ),
    )


class CustomerPatch(BaseModel):
    """Yalnız müşteri (yönetim şirketi) adı; vergi no ve plan buradan değişmez. Müşteride
    iletişim/adres alanı yok (yetkilinin iletişimi kullanıcı hesabındadır)."""

    name: str = Field(min_length=3, max_length=150)

    @field_validator("name")
    @classmethod
    def _collapse(cls, value: str) -> str:
        return " ".join(value.split())


class CustomerSummaryOut(BaseModel):
    organization_id: uuid.UUID
    name: str
    tax_number: str | None
    plan_id: uuid.UUID | None


@router.patch("/customers/{organization_id}", summary="Müşteri adını düzenle")
async def update_customer(
    organization_id: uuid.UUID, body: CustomerPatch, _: PlatformAdminDep, session: SessionDep
) -> Written[CustomerSummaryOut]:
    org = await session.get(Organization, organization_id, with_for_update=True)
    if org is None:
        raise NotFoundError("Müşteri bulunamadı.")
    org.name = body.name
    await session.flush()
    out = CustomerSummaryOut(
        organization_id=org.id, name=org.name, tax_number=org.tax_number, plan_id=org.plan_id
    )
    await session.commit()
    return Written(data=out, message=f"{org.name} müşterisinin bilgileri güncellendi.")


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


class SiteCreatedOut(SiteOut):
    requested_slug: str | None = Field(
        description="istekte gönderilen adres eki (gönderilmediyse null; ad'dan üretildi)"
    )
    slug_adjusted: bool = Field(
        description="gönderilen adres eki kurala göre düzeltildi (küçük harf, Türkçe harf "
        "eşleme, boşluk → tire); kullanılan ek `slug`"
    )


def site_label(name: str) -> str:
    """Mesajda site adı: ad zaten "… Sitesi" / "… Site" ile bitiyorsa "sitesi" eklenmez
    ("Yıldız Sitesi sitesi" olmasın). Karşılaştırma tr-TR küçük harfle."""
    words = tr_lower(name).split()
    return name if words and words[-1] in ("sitesi", "site") else f"{name} sitesi"


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


async def _require_site_plan(session: SessionDep, plan_id: uuid.UUID) -> Plan:
    """Siteye yalnız site planı atanır; yönetim şirketi planı (portföy) şirketindir → 422."""
    plan = await require_plan(session, plan_id)
    if plan_scope(plan.allowed_modules) is PlanScope.ORGANIZATION:
        raise PlatformRuleError(
            "organization_plan",
            "plan_id",
            f"'{plan.name}' bir yönetim şirketi planıdır; siteye atanamaz. Site planlarından "
            "birini seçin.",
        )
    return plan


@router.post("/sites", status_code=status.HTTP_201_CREATED, summary="Site aç")
async def create_site_endpoint(
    body: SiteCreate, _: PlatformAdminDep, session: SessionDep
) -> Written[SiteCreatedOut]:
    """Adres eki (`slug`) kurala göre düzeltilir (docs/04 §11); düzeltildiyse yanıtta
    `slug_adjusted` ve mesajda kullanılan ek bildirilir."""
    try:
        await _require_site_plan(session, body.plan_id)
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
            lifecycle_status="setup",  # YAP-01: kontrol listesi tamamlanınca canlıya alınır
        )
    except (PlatformRuleError, ProvisioningError) as exc:
        raise _rule_error(exc) from None
    requested = body.slug.strip() if body.slug and body.slug.strip() else None
    adjusted = requested is not None and requested != site.slug
    out = SiteCreatedOut(
        **(await _site_out(session, site)).model_dump(),
        requested_slug=requested,
        slug_adjusted=adjusted,
    )
    await session.commit()
    slug_note = (
        f"Girdiğiniz adres eki '{requested}' kurala göre '{site.slug}' olarak düzeltildi. "
        if adjusted
        else f"Adres eki: '{site.slug}'. "
    )
    return Written(
        data=out,
        message=(
            f"{site_label(site.name)} kurulum durumunda açıldı. {slug_note}Sıradaki adım: daire "
            "listesini Excel'den aktarın; kontrol listesi tamamlanınca site yöneticisi canlıya "
            "alır."
        ),
    )


class SitePatch(BaseModel):
    """Yalnız ad, adres ve konum; adres eki (`slug`) ve plan buradan değişmez."""

    name: str | None = Field(default=None, min_length=SITE_NAME_MIN, max_length=SITE_NAME_MAX)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=80)
    district: str | None = Field(default=None, max_length=80)

    @field_validator("name", "address", "city", "district")
    @classmethod
    def _collapse(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None


@router.patch("/sites/{site_id}", summary="Site adı ve adresini düzenle")
async def update_site(
    site_id: uuid.UUID, body: SitePatch, _: PlatformAdminDep, session: SessionDep
) -> Written[SiteOut]:
    """Gönderilen alanlar değişir; boş metin adres/il/ilçeyi temizler. Aynı ad (Türkçe büyük/
    küçük harf duyarsız) başka sitede varsa 409 `site_already_exists`."""
    site = await session.get(Site, site_id, with_for_update=True)
    if site is None:
        raise NotFoundError("Site bulunamadı.")
    sent = body.model_fields_set
    if "name" in sent and body.name is not None and body.name != site.name:
        clash = await session.scalar(
            select(Site.id).where(Site.name_key == site_name_key(body.name), Site.id != site.id)
        )
        if clash is not None:
            raise ApiError(
                HTTPStatus.CONFLICT,
                "site_already_exists",
                "Bu adla bir site zaten var. Farklı bir ad girin.",
            )
        site.name = body.name
    for field in ("address", "city", "district"):
        if field in sent:
            setattr(site, field, getattr(body, field) or None)
    await session.flush()
    out = await _site_out(session, site)
    await session.commit()
    return Written(data=out, message=f"{site_label(site.name)} bilgileri güncellendi.")


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
        plan = await _require_site_plan(session, body.plan_id)
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
