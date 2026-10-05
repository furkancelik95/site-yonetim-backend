"""Yeni site kurulumu — docs/10-demo-veri.md §3 (her ortamda).

Tek transaction içinde: site + her modül için satır + varsayılan daire tipleri. Bugün kurulan
kısım bu; gecikme politikası, gider kategorileri, tahakkuk tipleri, dağıtım kuralları ve kasa
hesapları ilgili tablolar gelince buraya eklenir.

Oturum site kapsamına sabitlendiği için her site kendi oturumunda kurulur.
"""

import uuid
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.text import slugify
from site_yonetim.models import PropertyKind, Site, UnitType
from site_yonetim.models.platform import site_name_key
from site_yonetim.services import departments
from site_yonetim.services.cash import ensure_default_accounts
from site_yonetim.services.finance_setup import ensure_finance_setup
from site_yonetim.services.sites import provision_site_modules

SITE_NAME_MIN, SITE_NAME_MAX = 3, 120
SLUG_MIN = 3
# docs/10 §3.7
DEFAULT_UNIT_TYPES: tuple[tuple[str, Decimal], ...] = (
    ("1+1", Decimal("1.0")),
    ("2+1", Decimal("1.35")),
    ("3+1", Decimal("1.7")),
)


class ProvisioningError(Exception):
    """Kurulum kuralı ihlali. `field` alan adı, `message` Türkçe."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


async def provision_site(
    session: AsyncSession,
    *,
    name: str,
    slug: str | None = None,
    plan_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    property_kind: PropertyKind = PropertyKind.RESIDENTIAL,
    address: str | None = None,
    city: str | None = None,
    district: str | None = None,
    iban: str | None = None,
    bank_name: str | None = None,
) -> Site:
    """Siteyi ve varsayılan kurulumunu yazar (commit çağırana aittir)."""
    name = " ".join(name.split())
    if not SITE_NAME_MIN <= len(name) <= SITE_NAME_MAX:
        raise ProvisioningError(
            "invalid_site_name",
            "name",
            f"Site adı {SITE_NAME_MIN}–{SITE_NAME_MAX} karakter olmalı.",
        )
    slug = slugify(slug or name)
    if len(slug) < SLUG_MIN:
        raise ProvisioningError(
            "invalid_slug", "slug", f"Adres eki en az {SLUG_MIN} karakter olmalı."
        )
    clash = await session.scalar(
        select(Site.id).where(or_(Site.name_key == site_name_key(name), Site.slug == slug))
    )
    if clash is not None:
        raise ProvisioningError(
            "site_already_exists",
            "slug",
            "Bu ad ya da adres ekiyle bir site zaten var. Farklı bir ad veya adres eki girin.",
        )

    site = Site(
        name=name,
        slug=slug,
        plan_id=plan_id,
        organization_id=organization_id,
        property_kind=property_kind.value,
        address=address,
        city=city,
        district=district,
        iban=iban,
        bank_name=bank_name,
    )
    session.add(site)
    await session.flush()

    with site_scope(site.id):
        await provision_site_modules(session, site)
        session.add_all(
            UnitType(name=type_name, weight=weight, sort_order=order)
            for order, (type_name, weight) in enumerate(DEFAULT_UNIT_TYPES)
        )
        await ensure_finance_setup(session)
        await departments.ensure_defaults(session)
        await ensure_default_accounts(session, iban=site.iban)
    return site
