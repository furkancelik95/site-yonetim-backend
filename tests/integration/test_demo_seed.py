"""Demo verisi ve site kurulumu — docs/10 (gerçek PostgreSQL, RLS'e tabi rol)."""

from datetime import date
from decimal import Decimal

import httpx2
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim import cli
from site_yonetim.core.config import Settings, get_settings
from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.main import create_app
from site_yonetim.models import (
    AccountBalance,
    Announcement,
    Block,
    BudgetPlan,
    Charge,
    ChargeRun,
    LedgerAccount,
    LedgerEntry,
    Organization,
    Payment,
    PaymentAllocation,
    Period,
    Person,
    Plan,
    Request,
    Site,
    SiteModule,
    Unit,
    UnitType,
    User,
)
from site_yonetim.seed.demo import DemoSeedRefusedError, seed_demo
from site_yonetim.seed.demo_data import DEMO_PASSWORD, SITES, occupancy_for
from site_yonetim.services.provisioning import ProvisioningError, provision_site
from tests.conftest import SettingsFactory
from tests.integration.conftest import DatabaseUrls, login_headers

Factory = async_sessionmaker[AsyncSession]


@pytest.fixture
def dev_settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(environment="development", seed_demo_data=True)


async def _count(factory: Factory, model: type[object]) -> int:
    async with factory() as session:
        return (await session.scalar(select(func.count()).select_from(model))) or 0


async def test_demo_verisi_yuklenir(
    dev_settings: Settings, session_factory: Factory, admin_engine: object
) -> None:
    assert await seed_demo(dev_settings, session_factory) is True

    async with session_factory() as session:
        sites = {s.slug: s for s in await session.scalars(select(Site))}
        org = await session.scalar(select(Organization))
    assert set(sites) == {"aksu-konaklari", "yildiz-sitesi", "mimoza-apartmani"}
    assert org is not None
    assert org.tax_number == "1234567890"
    assert all(site.organization_id == org.id for site in sites.values())
    assert await _count(session_factory, Plan) == 4
    assert await _count(session_factory, User) == 8  # 7 personel + sakin

    with all_sites_scope():
        assert await _count(session_factory, Unit) == 84
        tenants = sum(1 for spec in SITES for o in occupancy_for(spec) if o.tenant)
        assert await _count(session_factory, Person) == 84 + tenants
        # her malik: -M ve (kiracı eklenmeden önce) -O; her kiracı: -K
        assert await _count(session_factory, LedgerAccount) == 84 * 2 + tenants
        assert tenants > 0
        assert await _count(session_factory, Block) == 6
        assert await _count(session_factory, UnitType) == 9  # site başına 3

    with site_scope(sites["aksu-konaklari"].id):
        async with session_factory() as session:
            enabled = set(
                await session.scalars(select(SiteModule.module_key).where(SiteModule.enabled))
            )
    assert enabled == {
        "finance", "announcements", "requests", "documents",
        "reservations", "visitors", "packages", "valet",
    }  # fmt: skip


async def test_ikinci_calistirma_hicbir_sey_yapmaz(
    dev_settings: Settings, session_factory: Factory, admin_engine: object
) -> None:
    await seed_demo(dev_settings, session_factory)

    assert await seed_demo(dev_settings, session_factory) is False
    assert await _count(session_factory, User) == 8  # 7 personel + sakin


@pytest.mark.parametrize("environment", ["production", "test"])
async def test_gelistirme_disinda_hicbir_sey_yazilmaz(
    make_settings: SettingsFactory, session_factory: Factory, admin_engine: object, environment: str
) -> None:
    settings = make_settings(environment=environment, seed_demo_data=True, jwt_secret="x" * 40)

    with pytest.raises(DemoSeedRefusedError):
        await seed_demo(settings, session_factory)
    assert await _count(session_factory, User) == 0


async def test_demo_hesaplariyla_giris_ve_roller(
    dev_settings: Settings, session_factory: Factory, api: httpx2.AsyncClient
) -> None:
    await seed_demo(dev_settings, session_factory)

    async def sites_of(email: str) -> dict[str, str]:
        me = await api.get("/api/v1/me", headers=await login_headers(api, email, DEMO_PASSWORD))
        return {s["slug"]: s["role"] for s in me.json()["sites"]}

    assert await sites_of("yonetici@demo.local") == dict.fromkeys(
        ["aksu-konaklari", "yildiz-sitesi", "mimoza-apartmani"], "Yönetici"
    )
    assert set((await sites_of("muhasebe@demo.local")).values()) == {"Muhasebe"}
    assert await sites_of("mimoza@demo.local") == {"mimoza-apartmani": "Yönetici"}
    assert await sites_of("guvenlik@demo.local") == {"aksu-konaklari": "Güvenlik"}
    assert await sites_of("denetci@demo.local") == {"aksu-konaklari": "Denetçi"}
    assert await sites_of("teknik@demo.local") == {"aksu-konaklari": "Teknik Personel"}
    assert await sites_of("platform@demo.local") == {}


async def test_uygulama_acilisinda_gelistirmede_yuklenir(
    dev_settings: Settings, database_urls: DatabaseUrls, admin_engine: object
) -> None:
    app = create_app(dev_settings.model_copy(update={"database_url": SecretStr(database_urls.app)}))
    async with app.router.lifespan_context(app):
        factory = app.state.session_factory
        assert await _count(factory, Organization) == 1


def test_komut_satiri_gelistirme_disinda_reddeder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    try:
        assert cli.main(["seed-demo"]) == 2
    finally:
        get_settings.cache_clear()
    assert "development" in capsys.readouterr().err


def test_komut_satiri_yukler_sonra_atlar(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
    admin_engine: object,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("SEED_DEMO_DATA", "true")
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    try:
        assert cli.main(["seed-demo"]) == 0
        assert cli.main(["seed-demo"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "Demo verisi yüklendi." in out
    assert "zaten var" in out


# --- Site kurulumu (docs/10 §3) -------------------------------------------------


async def test_site_kurulumu_slug_uretir_ve_varsayilanlari_kurar(
    session_factory: Factory, admin_engine: object
) -> None:
    async with session_factory() as session, session.begin():
        site = await provision_site(session, name="  Çamlıca   Konutları ")

    assert (site.name, site.slug) == ("Çamlıca Konutları", "camlica-konutlari")
    with site_scope(site.id):
        async with session_factory() as session:
            types = [
                (t.name, str(t.weight))
                for t in await session.scalars(select(UnitType).order_by(UnitType.sort_order))
            ]
            modules = await session.scalar(select(func.count()).select_from(SiteModule))
    assert types == [("1+1", "1.0000"), ("2+1", "1.3500"), ("3+1", "1.7000")]
    assert modules == 12


@pytest.mark.parametrize(
    ("name", "slug", "code"),
    [("AB", None, "invalid_site_name"), ("Çok Güzel Site", "-!", "invalid_slug")],
)
async def test_site_kurulumu_gecersiz_girdi(
    session_factory: Factory, admin_engine: object, name: str, slug: str | None, code: str
) -> None:
    async with session_factory() as session:
        with pytest.raises(ProvisioningError) as exc:
            await provision_site(session, name=name, slug=slug)
    assert exc.value.code == code


async def test_site_kurulumu_ayni_ad_ya_da_slug_reddedilir(
    session_factory: Factory, admin_engine: object
) -> None:
    async with session_factory() as session, session.begin():
        await provision_site(session, name="Aksu Konakları")

    for name, slug in (("AKSU KONAKLARI", "baska-ek"), ("Başka Ad", "aksu-konaklari")):
        async with session_factory() as session:
            with pytest.raises(ProvisioningError) as exc:
                await provision_site(session, name=name, slug=slug)
        assert exc.value.code == "site_already_exists"


async def test_demo_finansi_motordan_gecer(
    dev_settings: Settings, session_factory: Factory, admin_engine: object
) -> None:
    """docs/10 §1.4: kesinleşmiş proje + son 6 ayın tahakkuku; tutarlar elle yazılmaz."""
    await seed_demo(dev_settings, session_factory, today=date(2026, 9, 30))
    async with session_factory() as session:
        sites = {s.slug: s.id for s in await session.scalars(select(Site))}
    for spec in SITES:
        with site_scope(sites[spec.slug]):
            async with session_factory() as session:
                plans = (await session.scalars(select(BudgetPlan))).all()
                runs = (
                    await session.execute(
                        select(Period.month, func.sum(Charge.amount))
                        .join(ChargeRun, ChargeRun.period_id == Period.id)
                        .join(Charge, Charge.charge_run_id == ChargeRun.id)
                        .group_by(Period.month)
                        .order_by(Period.month)
                    )
                ).all()
                ledger_total = await session.scalar(
                    select(func.sum(LedgerEntry.debit - LedgerEntry.credit))
                )
                summary_total = await session.scalar(select(func.sum(AccountBalance.balance)))
        assert [p.status for p in plans] == ["finalized"]
        assert [month for month, _ in runs] == [4, 5, 6, 7, 8, 9]
        # Asansörsüz sitede (Mimoza) yıllık bütçe kalan kalemlere oranlanır; aylık kalem her ay
        # ayrı yuvarlanır (K3) — kuruş farkı olabilir.
        assert all(abs(total - spec.monthly_budget) < Decimal("0.10") for _, total in runs), runs
        assert ledger_total == summary_total  # özet bakiye defterle tutar


async def test_demo_tahsilat_ve_sakin(
    dev_settings: Settings, session_factory: Factory, admin_engine: object, api: httpx2.AsyncClient
) -> None:
    """docs/10 §1.4, §2: tahsilatlar FIFO'dan geçer; sakin en borçlu oturan hesabın kişisidir."""
    await seed_demo(dev_settings, session_factory, today=date(2026, 9, 30))
    async with session_factory() as session:
        sites = {s.slug: s.id for s in await session.scalars(select(Site))}
    rates = {}
    for spec in SITES:
        with site_scope(sites[spec.slug]):
            async with session_factory() as session:
                charged = await session.scalar(select(func.sum(Charge.amount)))
                paid = await session.scalar(select(func.sum(Payment.amount)))
                allocated = await session.scalar(select(func.sum(PaymentAllocation.amount)))
                summary = await session.scalar(select(func.sum(AccountBalance.balance)))
        assert charged is not None
        assert paid is not None
        assert paid == allocated  # demo tahsilatı borç tutarında: avans yok
        assert summary == charged - paid
        rates[spec.slug] = paid / charged
    assert rates["aksu-konaklari"] > rates["yildiz-sitesi"] > rates["mimoza-apartmani"]

    headers = await login_headers(api, "sakin@demo.local", DEMO_PASSWORD)
    me = (await api.get("/api/v1/me", headers=headers)).json()
    [aksu] = me["sites"]
    assert aksu["role"] == "Sakin"
    with site_scope(sites["aksu-konaklari"]):
        async with session_factory() as session:
            worst = await session.scalar(
                select(LedgerAccount)
                .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
                .where(LedgerAccount.kind == "occupant")
                .order_by(AccountBalance.balance.desc(), LedgerAccount.reference_code)
                .limit(1)
            )
    assert worst is not None
    statement = await api.get(
        f"/api/v1/sites/aksu-konaklari/accounts/{worst.id}/statement", headers=headers
    )
    assert statement.status_code == 200
    assert Decimal(statement.json()["account"]["balance"]) > 0


async def test_demo_duyuru_ve_talep(
    dev_settings: Settings, session_factory: Factory, admin_engine: object, api: httpx2.AsyncClient
) -> None:
    """docs/10 §1.4: duyurular ve farklı durum/öncelikte talepler; sakin yalnız yürürlükteki
    duyuruları görür."""
    await seed_demo(dev_settings, session_factory, today=date(2026, 9, 30))
    async with session_factory() as session:
        sites = {s.slug: s.id for s in await session.scalars(select(Site))}
    for slug in sites.values():
        with site_scope(slug):
            async with session_factory() as session:
                statuses = set(await session.scalars(select(Request.status)))
                announcement_count = await session.scalar(
                    select(func.count()).select_from(Announcement)
                )
        assert statuses == {"open", "in_progress", "waiting", "resolved", "closed"}
        assert announcement_count == 4

    resident = await login_headers(api, "sakin@demo.local", DEMO_PASSWORD)
    listed = (await api.get("/api/v1/sites/aksu-konaklari/announcements", headers=resident)).json()
    assert [a["title"] for a in listed["items"]] == [
        "Olağan genel kurul toplantısı",
        "Planlı su kesintisi",
        "Aidat ödeme hatırlatması",
    ]
    manager = await login_headers(api, "yonetici@demo.local", DEMO_PASSWORD)
    requests_page = (await api.get("/api/v1/sites/aksu-konaklari/requests", headers=manager)).json()
    assert requests_page["total"] == 6
