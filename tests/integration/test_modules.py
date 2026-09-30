"""Modül kapısı ve site çözümleme — uçtan uca (gerçek PostgreSQL, RLS'e tabi rol).

Trello "BE · Dilim 1 · Modül kapısı" — bitti ölçütü: bir sitede modül kapatılınca o sitenin
isteği reddedilir, diğer site etkilenmez.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import httpx2
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import SiteContext, SiteContextDep, require_module
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey, ModuleRuleError
from site_yonetim.main import create_app
from site_yonetim.models import Plan, Site, SiteModule
from site_yonetim.services.sites import (
    available_modules,
    provision_site_modules,
    set_module_enabled,
)
from tests.conftest import SettingsFactory
from tests.integration.conftest import DatabaseUrls, TwoSites

Factory = async_sessionmaker[AsyncSession]
STANDART = ["finance", "announcements", "requests", "documents", "visitors", "surveys"]


@pytest.fixture
async def planned_sites(session_factory: Factory, two_sites: TwoSites) -> TwoSites:
    """İki siteye Standart planı atar ve modül satırlarını kurar."""
    async with session_factory() as session, session.begin():
        plan = Plan(name="Standart", max_units=150, allowed_modules=STANDART)
        session.add(plan)
        await session.flush()
        for site_id in (two_sites.site_a, two_sites.site_b):
            site = await session.get(Site, site_id)
            assert site is not None
            site.plan_id = plan.id

    for site_id in (two_sites.site_a, two_sites.site_b):
        with site_scope(site_id):
            async with session_factory() as session, session.begin():
                site = await session.get(Site, site_id)
                assert site is not None
                await provision_site_modules(session, site)
    return two_sites


@pytest.fixture
async def client(
    make_settings: SettingsFactory, session_factory: Factory
) -> AsyncIterator[httpx2.AsyncClient]:
    app: FastAPI = create_app(make_settings())
    app.state.session_factory = session_factory

    @app.get("/api/v1/sites/{slug}/_probe")
    async def probe(ctx: SiteContextDep) -> dict[str, str]:
        return {"site": ctx.site.slug}

    @app.get("/api/v1/sites/{slug}/_requests")
    async def requests_probe(
        ctx: Annotated[SiteContext, Depends(require_module(ModuleKey.REQUESTS))],
    ) -> dict[str, int]:
        # Kapsam açık: kiracı tablosu filtreli okunur.
        count = await ctx.session.scalar(select(func.count()).select_from(SiteModule))
        return {"module_rows": count or 0}

    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


async def _toggle(factory: Factory, site_id: uuid.UUID, key: ModuleKey, *, enable: bool) -> None:
    with site_scope(site_id):
        async with factory() as session, session.begin():
            site = await session.get(Site, site_id)
            assert site is not None
            await set_module_enabled(session, site, key, enable=enable)


async def test_kurulumda_her_modul_icin_satir_ve_varsayilanlar(
    session_factory: Factory, planned_sites: TwoSites
) -> None:
    with site_scope(planned_sites.site_a):
        async with session_factory() as session:
            rows = {
                row.module_key: row.enabled for row in await session.scalars(select(SiteModule))
            }
            site = await session.get(Site, planned_sites.site_a)
            assert site is not None
            available = await available_modules(session, site)

    assert set(rows) == {key.value for key in ModuleKey}
    assert {key for key, on in rows.items() if on} == {
        "finance",
        "announcements",
        "requests",
        "documents",
    }
    assert available == {
        ModuleKey.FINANCE,
        ModuleKey.ANNOUNCEMENTS,
        ModuleKey.REQUESTS,
        ModuleKey.DOCUMENTS,
    }


async def test_acik_modulun_ucu_calisir_ve_kapsam_aciktir(
    client: httpx2.AsyncClient, planned_sites: TwoSites
) -> None:
    response = await client.get("/api/v1/sites/aksu-konaklari/_requests")

    assert response.status_code == 200
    assert response.json() == {"module_rows": len(ModuleKey)}  # yalnız A'nın satırları


async def test_modul_bir_sitede_kapatilinca_yalniz_o_site_reddedilir(
    client: httpx2.AsyncClient, session_factory: Factory, planned_sites: TwoSites
) -> None:
    await _toggle(session_factory, planned_sites.site_a, ModuleKey.REQUESTS, enable=False)

    closed = await client.get("/api/v1/sites/aksu-konaklari/_requests")
    other = await client.get("/api/v1/sites/yildiz-sitesi/_requests")

    assert closed.status_code == 404
    assert closed.json()["error"]["code"] == "not_found"
    assert other.status_code == 200


async def test_kapatilan_modul_yeniden_acilinca_ucu_calisir(
    client: httpx2.AsyncClient, session_factory: Factory, planned_sites: TwoSites
) -> None:
    await _toggle(session_factory, planned_sites.site_a, ModuleKey.REQUESTS, enable=False)
    await _toggle(session_factory, planned_sites.site_a, ModuleKey.REQUESTS, enable=True)

    assert (await client.get("/api/v1/sites/aksu-konaklari/_requests")).status_code == 200


async def test_satir_acik_olsa_bile_planda_yoksa_404(
    client: httpx2.AsyncClient, session_factory: Factory, planned_sites: TwoSites
) -> None:
    # Plan düşürüldü: satır açık kaldı ama plan artık izin vermiyor.
    async with session_factory() as session, session.begin():
        site = await session.get(Site, planned_sites.site_a)
        assert site is not None
        site.plan_id = None

    response = await client.get("/api/v1/sites/aksu-konaklari/_requests")

    assert response.status_code == 404


async def test_cekirdek_kapatilamaz_planda_olmayan_acilamaz(
    session_factory: Factory, planned_sites: TwoSites
) -> None:
    with pytest.raises(ModuleRuleError, match="kapatılamaz"):
        await _toggle(session_factory, planned_sites.site_a, ModuleKey.FINANCE, enable=False)
    with pytest.raises(ModuleRuleError) as exc:
        await _toggle(session_factory, planned_sites.site_a, ModuleKey.VALET, enable=True)
    assert exc.value.code == "module_not_in_plan"


@pytest.mark.parametrize("slug", ["olmayan-site", "Aksu-Konaklari", "aksu_konaklari", "a--b"])
async def test_bilinmeyen_ya_da_bicimsiz_site_404(
    client: httpx2.AsyncClient, planned_sites: TwoSites, slug: str
) -> None:
    response = await client.get(f"/api/v1/sites/{slug}/_probe")

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Aradığınız kayıt bulunamadı."


async def test_site_cozumlenir(client: httpx2.AsyncClient, planned_sites: TwoSites) -> None:
    response = await client.get("/api/v1/sites/yildiz-sitesi/_probe")

    assert response.json() == {"site": "yildiz-sitesi"}


async def test_hazirlik_ucu_veritabanina_baglanir(
    make_settings: SettingsFactory, database_urls: DatabaseUrls, migrated_database: None
) -> None:
    app = create_app(make_settings(database_url=database_urls.app))
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http:
            response = await http.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_modul_servisi_site_kapsami_disinda_calismaz(
    session_factory: Factory, planned_sites: TwoSites
) -> None:
    async with session_factory() as session:
        site = await session.get(Site, planned_sites.site_a)
        assert site is not None
        with pytest.raises(RuntimeError, match="kapsamında"):
            await available_modules(session, site)
        with site_scope(planned_sites.site_b), pytest.raises(RuntimeError):
            await available_modules(session, site)  # başka sitenin kapsamı da olmaz


async def test_satiri_olmayan_modul_acilinca_satir_olusur(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    # Modül satırları kurulmamış, plansız site: çekirdek açılabilir, satırı oluşur.
    await _toggle(session_factory, two_sites.site_a, ModuleKey.FINANCE, enable=True)

    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            keys = list(await session.scalars(select(SiteModule.module_key)))
    assert keys == ["finance"]
