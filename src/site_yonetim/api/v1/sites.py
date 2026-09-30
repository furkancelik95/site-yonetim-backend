"""Site bilgisi — `GET /sites/{slug}` (docs/06 §2.4): site + açık modüller + kullanıcının
o sitedeki izinleri. Erişimi olmayan kullanıcı 404 alır."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from site_yonetim.api.deps import SiteContextDep

router = APIRouter(prefix="/sites", tags=["site"])


class SiteResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    property_kind: str
    city: str | None
    district: str | None
    role: str
    permissions: list[str]
    modules: list[str]


@router.get("/{slug}", summary="Site bilgisi, açık modüller ve izinler")
async def get_site(ctx: SiteContextDep) -> SiteResponse:
    site = ctx.site
    return SiteResponse(
        id=site.id,
        name=site.name,
        slug=site.slug,
        property_kind=site.property_kind,
        city=site.city,
        district=site.district,
        role=ctx.access.role.value,
        permissions=sorted(p.value for p in ctx.access.permissions),
        modules=sorted(m.value for m in ctx.modules),
    )
