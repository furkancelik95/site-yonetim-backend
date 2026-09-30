"""Oturumdaki kullanıcı — `GET /me` (docs/05 §4, docs/06 §2.1).

Frontend yönlendirmeyi buna göre yapar: platform yöneticisi → platform paneli, sakin → sakin
ekranı, tek siteli personel → o site, çok siteli → portföy.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from site_yonetim.api.deps import UserAccessDep
from site_yonetim.domain.access import SiteAccess, UserAccess

router = APIRouter(tags=["kimlik"])


class SiteAccessOut(BaseModel):
    site_id: uuid.UUID
    name: str
    slug: str
    role: str
    permissions: list[str]
    person_id: uuid.UUID | None
    is_derived: bool
    organization_id: uuid.UUID | None
    organization_role: str | None

    @classmethod
    def of(cls, access: SiteAccess) -> SiteAccessOut:
        return cls(
            site_id=access.site_id,
            name=access.name,
            slug=access.slug,
            role=access.role.value,
            permissions=sorted(p.value for p in access.permissions),
            person_id=access.person_id,
            is_derived=access.is_derived,
            organization_id=access.organization_id,
            organization_role=access.organization_role.value if access.organization_role else None,
        )


class MeResponse(BaseModel):
    user_id: uuid.UUID
    full_name: str
    kind: str
    is_platform_admin: bool
    can_see_portfolio: bool
    sites: list[SiteAccessOut]

    @classmethod
    def of(cls, access: UserAccess) -> MeResponse:
        return cls(
            user_id=access.user_id,
            full_name=access.full_name,
            kind=access.kind.value,
            is_platform_admin=access.is_platform_admin,
            can_see_portfolio=access.can_see_portfolio,
            sites=[SiteAccessOut.of(site) for site in access.sites],
        )


@router.get("/me", summary="Oturumdaki kullanıcı ve erişimleri")
async def me(access: UserAccessDep) -> MeResponse:
    return MeResponse.of(access)
