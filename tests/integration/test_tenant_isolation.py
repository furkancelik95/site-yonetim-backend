"""Kiracı (tenant) izolasyonu — docs/07-test-senaryolari.md §6 (zorunlu).

Kurgu: A sitesi (1 blok, 4 daire) · B sitesi (1 blok, 3 daire). Testler uygulama rolüyle
(RLS'e tabi) çalışır.
"""

import uuid

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from site_yonetim.db.tenancy import (
    TenantLeakError,
    TenantScopeError,
    all_sites_scope,
    current_scope,
    site_scope,
)
from site_yonetim.models import Block, Unit
from tests.integration.conftest import TwoSites

Factory = async_sessionmaker[AsyncSession]


async def _count(factory: Factory, model: type[Block] | type[Unit]) -> int:
    async with factory() as session:
        return (await session.scalar(select(func.count()).select_from(model))) or 0


async def test_6_1_a_kapsaminda_yalniz_a_verisi(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            units = (await session.scalars(select(Unit))).all()
            blocks = (await session.scalars(select(Block))).all()

    assert len(units) == 4
    assert len(blocks) == 1
    assert {unit.site_id for unit in units} == {two_sites.site_a}
    assert blocks[0].name == "Aksu A"


async def test_6_2_b_kapsaminda_yalniz_b_verisi(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_b):
        async with session_factory() as session:
            units = (await session.scalars(select(Unit))).all()

    assert len(units) == 3
    assert {unit.site_id for unit in units} == {two_sites.site_b}


async def test_6_3_diger_sitenin_kaydi_kimlikle_bile_getirilemez(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    b_unit = two_sites.units_b[0]
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            by_get = await session.get(Unit, b_unit)
            by_query = await session.scalar(select(Unit).where(Unit.id == b_unit))

    assert by_get is None
    assert by_query is None


async def test_6_4_iliskili_veri_uzerinden_de_sizmaz(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            units = (await session.scalars(select(Unit).options(selectinload(Unit.block)))).all()

    assert {unit.block.name for unit in units} == {"Aksu A"}
    assert {unit.block.site_id for unit in units} == {two_sites.site_a}


async def test_6_5_baska_sitenin_site_id_siyle_kayit_eklemek_hata(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            session.add(Block(name="Sızdırılan", site_id=two_sites.site_b))
            with pytest.raises(TenantLeakError, match="sızıntı"):
                await session.flush()

    assert await _count_in(session_factory, two_sites.site_b, Block) == 1


async def test_6_6_site_baglami_acilmadan_kayit_eklenemez(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    assert current_scope() is None
    async with session_factory() as session:
        session.add(Block(name="Kapsamsız"))
        with pytest.raises(TenantScopeError, match="Site bağlamı"):
            await session.flush()


async def test_6_6_site_baglami_acilmadan_kiraci_tablosu_okunamaz(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    async with session_factory() as session:
        with pytest.raises(TenantScopeError, match="Site bağlamı"):
            await session.scalars(select(Unit))


async def test_6_7_yeni_kayda_gecerli_site_otomatik_damgalanir(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session, session.begin():
            block = Block(name="Yeni blok")
            session.add(block)

    assert block.site_id == two_sites.site_a
    assert await _count_in(session_factory, two_sites.site_a, Block) == 2


async def test_6_8_baska_sitenin_kaydi_guncellenemez(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_b):
        async with session_factory() as session_b:
            b_unit = await session_b.get(Unit, two_sites.units_b[0])
    assert b_unit is not None

    with site_scope(two_sites.site_a):
        async with session_factory() as session_a:
            session_a.add(b_unit)
            b_unit.number = "ele-gecirildi"
            with pytest.raises(TenantLeakError, match="sızıntı"):
                await session_a.flush()


async def test_6_8_toplu_guncelleme_baska_siteye_dokunmaz(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session, session.begin():
            result = await session.execute(update(Unit).values(floor=7))

    assert result.rowcount == 4  # type: ignore[attr-defined]
    with site_scope(two_sites.site_b):
        async with session_factory() as session:
            floors = set((await session.scalars(select(Unit.floor))).all())
    assert floors == {None}


async def test_6_8_baska_sitenin_kaydi_silinemez(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_b):
        async with session_factory() as session_b:
            b_unit = await session_b.get(Unit, two_sites.units_b[0])

    with site_scope(two_sites.site_a):
        async with session_factory() as session_a:
            session_a.add(b_unit)
            await session_a.delete(b_unit)
            with pytest.raises(TenantLeakError):
                await session_a.flush()


async def test_6_9_kapsam_kapaninca_onceki_site_geri_gelir(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        assert await _count(session_factory, Unit) == 4
        with site_scope(two_sites.site_b):
            assert await _count(session_factory, Unit) == 3
        assert await _count(session_factory, Unit) == 4
    assert current_scope() is None


async def test_6_10_tum_siteler_kapsami_her_seyi_gorur(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with all_sites_scope():
        assert await _count(session_factory, Unit) == 7
        assert await _count(session_factory, Block) == 2


async def test_6_11_rls_uygulama_filtresi_atlansa_bile_yalniz_a(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            site_ids = set((await session.scalars(text("SELECT site_id FROM units"))).all())

    assert site_ids == {two_sites.site_a}


# --- Ek güvenceler ----------------------------------------------------------


async def test_rls_kapsam_degiskeni_yoksa_hicbir_satir_gorunmez(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    # Uygulama katmanını tamamen atlayan ham bağlantı: kapalı başarısızlık.
    async with session_factory() as session:
        connection = await session.connection()
        rows: int = (await connection.execute(text("SELECT count(*) FROM units"))).scalar_one()

    assert rows == 0


async def test_rls_ham_sql_ile_baska_siteye_yazmayi_reddeder(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            with pytest.raises(DBAPIError, match="row-level security"):
                await session.execute(
                    text(
                        "INSERT INTO blocks (id, site_id, name, has_elevator, sort_order) "
                        "VALUES (gen_random_uuid(), :site, 'x', false, 0)"
                    ),
                    {"site": two_sites.site_b},
                )


async def test_daire_baska_sitenin_blogune_baglanamaz(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            session.add(Unit(block_id=two_sites.block_b, number="99"))
            with pytest.raises(IntegrityError):
                await session.flush()


async def test_oturum_tek_kapsama_sabitlenir(session_factory: Factory, two_sites: TwoSites) -> None:
    async with session_factory() as session:
        with site_scope(two_sites.site_a):
            await session.scalars(select(Unit))
        with site_scope(two_sites.site_b), pytest.raises(TenantScopeError, match="yeni oturum"):
            await session.scalars(select(Unit))


async def test_uygulama_rolu_rls_atlayamaz(session_factory: Factory, two_sites: TwoSites) -> None:
    async with session_factory() as session:
        row = (
            await session.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()

    assert tuple(row) == (False, False)


async def _count_in(factory: Factory, site_id: uuid.UUID, model: type[Block] | type[Unit]) -> int:
    with site_scope(site_id):
        return await _count(factory, model)


async def test_kendi_kaydinin_site_id_si_baska_siteye_tasinamaz(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with site_scope(two_sites.site_a):
        async with session_factory() as session:
            block = await session.get(Block, two_sites.block_a)
            assert block is not None
            block.site_id = two_sites.site_b
            with pytest.raises(TenantLeakError):
                await session.flush()


async def test_tum_siteler_kapsaminda_yeni_kayit_site_id_ister(
    session_factory: Factory, two_sites: TwoSites
) -> None:
    with all_sites_scope():
        async with session_factory() as session:
            session.add(Block(name="Sahipsiz"))
            with pytest.raises(TenantScopeError, match="açıkça"):
                await session.flush()

        async with session_factory() as session, session.begin():
            session.add(Block(name="Gece işi", site_id=two_sites.site_b))

    assert await _count_in(session_factory, two_sites.site_b, Block) == 2


def test_site_kapsami_uuid_ister() -> None:
    with pytest.raises(TypeError), site_scope("aksu-konaklari"):  # type: ignore[arg-type]
        pass
