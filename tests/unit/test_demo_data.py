"""Demo verisi tanımı — docs/10 §1–2 (veritabanısız)."""

import pytest

from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import UnitUsage
from site_yonetim.seed.demo import DemoSeedRefusedError, ensure_demo_allowed
from site_yonetim.seed.demo_data import ACCOUNTS, PLANS, SITES, units_for
from tests.conftest import SettingsFactory


def test_toplam_84_bagimsiz_bolum_ve_blok_dagilimi() -> None:
    counts = {site.slug: len(units_for(site)[0]) for site in SITES}

    assert counts == {"aksu-konaklari": 48, "yildiz-sitesi": 24, "mimoza-apartmani": 12}


@pytest.mark.parametrize("site", SITES, ids=lambda s: s.slug)
def test_arsa_paylari_toplami_paydaya_esit(site: object) -> None:
    units, denominator = units_for(site)  # type: ignore[arg-type]

    assert sum(u.land_share_numerator for u in units) == denominator
    assert all(u.land_share_numerator > 0 for u in units)


@pytest.mark.parametrize("site", SITES, ids=lambda s: s.slug)
def test_katlar_blok_sinirinda_ve_numaralar_benzersiz(site: object) -> None:
    units, _ = units_for(site)  # type: ignore[arg-type]
    floors = {b.name: b.floor_count for b in site.blocks}  # type: ignore[attr-defined]

    assert all(0 <= u.floor <= floors[u.block] for u in units)
    assert len({(u.block, u.number) for u in units}) == len(units)
    assert all(u.net_area < u.gross_area for u in units)


def test_sabit_tohum_her_seferinde_ayni_veri() -> None:
    assert [units_for(s) for s in SITES] == [units_for(s) for s in SITES]


def test_dukkanlar_yalniz_karma_sitede() -> None:
    shops = {s.slug: [u for u in units_for(s)[0] if u.usage is UnitUsage.COMMERCIAL] for s in SITES}

    assert len(shops["aksu-konaklari"]) == 4
    assert all(u.block == "C" and u.commercial_title for u in shops["aksu-konaklari"])
    assert shops["yildiz-sitesi"] == shops["mimoza-apartmani"] == []


def test_planlar_docs_01_ile_ayni() -> None:
    by_name = {p.name: p for p in PLANS}

    assert set(by_name["Başlangıç"].modules) == {"finance", "announcements", "requests"}
    assert by_name["Başlangıç"].max_units == 30
    assert by_name["Standart"].max_units == 150
    assert ModuleKey.PORTFOLIO not in by_name["Pro"].modules
    assert set(by_name["Yönetim Şirketi"].modules) == set(ModuleKey)


def test_ek_moduller_sitenin_planinda_var() -> None:
    plans = {p.name: set(p.modules) for p in PLANS}

    for site in SITES:
        assert set(site.extra_modules) <= plans[site.plan], site.slug


def test_demo_hesaplari_docs_10_ile_ayni() -> None:
    emails = {a.email for a in ACCOUNTS}

    assert emails == {
        "platform@demo.local",
        "yonetici@demo.local",
        "muhasebe@demo.local",
        "mimoza@demo.local",
        "guvenlik@demo.local",
        "denetci@demo.local",
        "teknik@demo.local",
    }
    assert all(a.email.endswith("@demo.local") for a in ACCOUNTS)


@pytest.mark.parametrize(
    ("environment", "seed"),
    [("production", True), ("test", True), ("development", False)],
)
def test_demo_verisi_gelistirme_disinda_reddedilir(
    make_settings: SettingsFactory, environment: str, seed: bool
) -> None:
    settings = make_settings(environment=environment, seed_demo_data=seed)

    with pytest.raises(DemoSeedRefusedError, match="development"):
        ensure_demo_allowed(settings)


def test_demo_verisi_gelistirmede_izinli(make_settings: SettingsFactory) -> None:
    ensure_demo_allowed(make_settings(environment="development", seed_demo_data=True))
