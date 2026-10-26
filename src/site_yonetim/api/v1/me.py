"""Oturumdaki kullanıcı — `GET /me` (docs/05 §4, docs/06 §2.1).

Frontend yönlendirmeyi buna göre yapar: platform yöneticisi → platform paneli, sakin → sakin
ekranı, tek siteli personel → o site, çok siteli → portföy.
"""

import datetime as dt
import uuid

from fastapi import APIRouter
from pydantic import BaseModel, Field

from site_yonetim.api.deps import AuthenticatedUserDep, FactoryDep, NowDep, TodayDep
from site_yonetim.domain.access import SiteAccess, UserAccess
from site_yonetim.domain.mfa import MfaStatus
from site_yonetim.services.access import load_user_access
from site_yonetim.services.mfa import required_for

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
    must_change_password: bool = Field(
        description="true: geçici parola — önce `POST /auth/change-password`"
    )
    sites: list[SiteAccessOut]
    email: str | None = None
    phone: str | None = None
    mfa_enabled: bool = False
    mfa_required: bool = Field(default=False, description="yönetici rolü: 2FA zorunlu")
    mfa_setup_deadline: dt.datetime | None = Field(
        default=None, description="zorunlu ve kurulmamış: geçiş süresinin sonu (uyarı bandı)"
    )
    mfa_setup_required: bool = Field(
        default=False,
        description="true: süre doldu — 2FA kurulum ekranına yönlendir (diğer uçlar 403 "
        "`mfa_setup_required`)",
    )

    @classmethod
    def of(cls, access: UserAccess, *, must_change_password: bool = False) -> MeResponse:
        return cls(
            user_id=access.user_id,
            full_name=access.full_name,
            kind=access.kind.value,
            is_platform_admin=access.is_platform_admin,
            can_see_portfolio=access.can_see_portfolio,
            must_change_password=must_change_password,
            sites=[SiteAccessOut.of(site) for site in access.sites],
        )


@router.get("/me", summary="Oturumdaki kullanıcı ve erişimleri")
async def me(
    current: AuthenticatedUserDep, factory: FactoryDep, today: TodayDep, now: NowDep
) -> MeResponse:
    """Geçici paroladayken ve 2FA kurulumu beklerken de çalışır (frontend yönlendirmeyi
    buradan yapar). İki adımlı kod bekleyen oturumda 401 `mfa_required`."""
    user = current.user
    access = await load_user_access(factory, user, today=today)
    required = required_for(access)
    state = MfaStatus(required, user.mfa_enabled_at is not None, user.mfa_grace_until)
    out = MeResponse.of(access, must_change_password=user.must_change_password)
    return out.model_copy(
        update={
            "email": user.email,
            "phone": user.phone,
            "mfa_enabled": state.enabled,
            "mfa_required": required,
            "mfa_setup_deadline": user.mfa_grace_until if state.setup_pending() else None,
            "mfa_setup_required": state.setup_blocking(now),
        }
    )
