import pytest

from site_yonetim.db.rls import enable_tenant_rls
from site_yonetim.db.session import create_engine_from_settings, create_session_factory
from site_yonetim.db.tenancy import TenantSession
from tests.conftest import SettingsFactory


def test_veritabani_adresi_yoksa_acik_hata(make_settings: SettingsFactory) -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_engine_from_settings(make_settings(database_url=None))


async def test_oturumlar_kiraci_korumali(make_settings: SettingsFactory) -> None:
    settings = make_settings(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db")
    engine = create_engine_from_settings(settings)
    try:
        factory = create_session_factory(engine)
        assert factory.kw["sync_session_class"] is TenantSession
    finally:
        await engine.dispose()


class _FakeOp:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sqltext: str) -> None:
        self.statements.append(sqltext)


def test_rls_politikasi_zorunlu_ve_yazmayi_da_kapsar() -> None:
    op = _FakeOp()
    enable_tenant_rls(op, "units")

    assert 'ALTER TABLE "units" FORCE ROW LEVEL SECURITY' in op.statements
    assert "WITH CHECK" in op.statements[-1]


@pytest.mark.parametrize("bad", ['units"; DROP TABLE sites; --', "Units", "", "1units"])
def test_rls_tablo_adi_dogrulanir(bad: str) -> None:
    with pytest.raises(ValueError, match="tablo adı"):
        enable_tenant_rls(_FakeOp(), bad)


def test_yavas_sorgu_parametresiz_loglanir(caplog: pytest.LogCaptureFixture) -> None:
    from sqlalchemy import create_engine, text

    from site_yonetim.db.session import install_slow_query_log

    engine = create_engine("sqlite://")
    install_slow_query_log(engine, threshold_ms=-1)
    with caplog.at_level("WARNING", logger="site_yonetim.db"), engine.connect() as conn:
        conn.execute(text("SELECT :email"), {"email": "ayse@example.com"})

    assert "Yavaş sorgu" in caplog.text
    assert "ayse@example.com" not in caplog.text
