"""Site bilgisi — `GET /sites/{slug}` (docs/06 §2.4): site + açık modüller + kullanıcının
o sitedeki izinleri. Erişimi olmayan kullanıcı 404 alır."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from site_yonetim.api.deps import SiteContextDep
from site_yonetim.api.v1.me import RoleGrantOut, ScopedPermissionOut

router = APIRouter(prefix="/sites", tags=["site"])


class SiteResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    property_kind: str
    city: str | None
    district: str | None
    role: str = Field(description="ana rol (en üst düzey) — geriye uyum")
    roles: list[RoleGrantOut] = Field(description="şu an etkin bütün roller (P18)")
    permissions: list[str] = Field(description="tesis geneli + blokla sınırlı izinler (birleşim)")
    scoped_permissions: list[ScopedPermissionOut]
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
        role=ctx.access.role,
        roles=[RoleGrantOut.of(g) for g in ctx.access.roles],
        permissions=sorted(p.value for p in ctx.access.usable_permissions),
        scoped_permissions=[
            ScopedPermissionOut(permission=p.value, block_ids=sorted(blocks))
            for p, blocks in sorted(ctx.access.scoped.items())
        ],
        modules=sorted(m.value for m in ctx.modules),
    )
