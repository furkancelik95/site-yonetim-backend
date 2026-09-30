"""Siteye erişim ve izin — docs/05 §4–§7, docs/07 §8.

Trello "BE · Dilim 2 · Kimlik, site üyeliği ve roller" — bitti ölçütü: aynı kişi iki siteye
farklı rolle girer, her istek yalnız seçili siteyi görür.
"""

from typing import Annotated

import httpx2
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import SiteContext, require_permission
from site_yonetim.domain.access import Permission
from site_yonetim.models import Organization, Site, Unit
from site_yonetim.models import OrganizationMembership as OrgMembershipRow
from tests.integration.conftest import (
    TwoSites,
    add_site_membership,
    create_user,
    login_headers,
)

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
def probes(api_app: FastAPI) -> FastAPI:
    """Henüz gerçek finans ucu yok; aynı bağımlılık zinciriyle deneme uçları."""

    @api_app.get("/api/v1/sites/{slug}/_finance")
    async def finance_probe(
        ctx: Annotated[SiteContext, Depends(require_permission(Permission.FINANCE_READ))],
    ) -> dict[str, int]:
        count = await ctx.session.scalar(select(func.count()).select_from(Unit))
        return {"units": count or 0}

    @api_app.get("/api/v1/sites/{slug}/_units")
    async def units_probe(
        ctx: Annotated[SiteContext, Depends(require_permission(Permission.SECURITY_VISITORS))],
    ) -> dict[str, list[str]]:
        rows = await ctx.session.scalars(select(Unit.site_id).distinct())
        return {"site_ids": [str(r) for r in rows]}

    return api_app


@pytest.fixture
async def ayse(
    api: httpx2.AsyncClient, probes: FastAPI, session_factory: Factory, two_sites: TwoSites
) -> dict[str, str]:
    """A'da Yönetici, B'de Güvenlik."""
    user = await create_user(session_factory, "ayse@test.local", full_name="Ayşe YILMAZ")
    await add_site_membership(session_factory, two_sites.site_a, user.id, "Yönetici")
    await add_site_membership(session_factory, two_sites.site_b, user.id, "Güvenlik")
    return await login_headers(api, "ayse@test.local")


async def test_ayni_kisi_iki_sitede_farkli_rolle(
    api: httpx2.AsyncClient, ayse: dict[str, str]
) -> None:
    aksu = (await api.get("/api/v1/sites/aksu-konaklari", headers=ayse)).json()
    yildiz = (await api.get("/api/v1/sites/yildiz-sitesi", headers=ayse)).json()

    assert aksu["role"] == "Yönetici"
    assert "finance.read" in aksu["permissions"]
    assert aksu["modules"] == ["finance"]
    assert yildiz["role"] == "Güvenlik"
    assert "finance.read" not in yildiz["permissions"]


async def test_her_istek_yalniz_secili_sitenin_verisini_gorur(
    api: httpx2.AsyncClient, ayse: dict[str, str], two_sites: TwoSites
) -> None:
    aksu = await api.get("/api/v1/sites/aksu-konaklari/_finance", headers=ayse)
    yildiz = await api.get("/api/v1/sites/yildiz-sitesi/_units", headers=ayse)

    assert aksu.json() == {"units": 4}
    assert yildiz.json() == {"site_ids": [str(two_sites.site_b)]}


async def test_8_1_guvenlik_finans_ucundan_403(
    api: httpx2.AsyncClient, ayse: dict[str, str]
) -> None:
    response = await api.get("/api/v1/sites/yildiz-sitesi/_finance", headers=ayse)

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Bu işlem için yetkiniz yok."


async def test_8_5_erisimi_olmayan_site_404_403_degil(
    api: httpx2.AsyncClient, ayse: dict[str, str], session_factory: Factory
) -> None:
    async with session_factory() as session, session.begin():
        session.add(Site(name="Mimoza Apartmanı", slug="mimoza-apartmani"))

    no_access = await api.get("/api/v1/sites/mimoza-apartmani/_finance", headers=ayse)
    missing = await api.get("/api/v1/sites/olmayan-site/_finance", headers=ayse)

    assert no_access.status_code == 404
    assert no_access.json() == missing.json()  # varlık sızmaz


async def test_kimliksiz_site_ucu_401(api: httpx2.AsyncClient, probes: FastAPI) -> None:
    assert (await api.get("/api/v1/sites/aksu-konaklari")).status_code == 401


async def test_8_7_platform_yoneticisi_site_verisini_goremez(
    api: httpx2.AsyncClient, probes: FastAPI, session_factory: Factory, two_sites: TwoSites
) -> None:
    admin = await create_user(session_factory, "platform@test.local", is_platform_admin=True)
    await add_site_membership(session_factory, two_sites.site_a, admin.id, "Yönetici")
    headers = await login_headers(api, "platform@test.local")

    me = (await api.get("/api/v1/me", headers=headers)).json()
    site = await api.get("/api/v1/sites/aksu-konaklari/_finance", headers=headers)

    assert me["is_platform_admin"] is True
    assert me["sites"] == []
    assert site.status_code == 404


async def test_sirket_uyeligi_turetilmis_erisim_ve_acik_uyelik_ezer(
    api: httpx2.AsyncClient, probes: FastAPI, session_factory: Factory, two_sites: TwoSites
) -> None:
    async with session_factory() as session, session.begin():
        org = Organization(name="Kent Yönetim")
        session.add(org)
        await session.flush()
        for site_id in (two_sites.site_a, two_sites.site_b):
            site = await session.get(Site, site_id)
            assert site is not None
            site.organization_id = org.id
    user = await create_user(session_factory, "muhasebe@test.local")
    async with session_factory() as session, session.begin():
        session.add(OrgMembershipRow(organization_id=org.id, user_id=user.id, role="Muhasebe"))
    await add_site_membership(session_factory, two_sites.site_b, user.id, "Denetçi")
    headers = await login_headers(api, "muhasebe@test.local")

    sites = {s["slug"]: s for s in (await api.get("/api/v1/me", headers=headers)).json()["sites"]}

    assert sites["aksu-konaklari"]["role"] == "Muhasebe"
    assert sites["aksu-konaklari"]["is_derived"] is True
    assert sites["aksu-konaklari"]["organization_role"] == "Muhasebe"
    assert sites["yildiz-sitesi"]["role"] == "Denetçi"
    assert sites["yildiz-sitesi"]["is_derived"] is False
    assert (await api.get("/api/v1/me", headers=headers)).json()["can_see_portfolio"] is True


async def test_pasif_uyelik_erisim_vermez(
    api: httpx2.AsyncClient, probes: FastAPI, session_factory: Factory, two_sites: TwoSites
) -> None:
    user = await create_user(session_factory, "eski@test.local")
    await add_site_membership(
        session_factory, two_sites.site_a, user.id, "Yönetici", is_active=False
    )
    headers = await login_headers(api, "eski@test.local")

    assert (await api.get("/api/v1/sites/aksu-konaklari", headers=headers)).status_code == 404
