import pytest
from fastapi.testclient import TestClient

from site_yonetim.main import create_app
from tests.conftest import SettingsFactory


def test_saglik_ucu_ok_doner(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_semasi_api_v1_altinda(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]


def test_veritabani_yoksa_hazirlik_503(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_not_configured"


def test_veritabanina_ulasilamazsa_hazirlik_503_ve_ayrinti_sizmaz(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(database_url="postgresql+asyncpg://u:gizli-parola@127.0.0.1:1/db")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "gizli-parola" not in response.text
    assert "127.0.0.1" not in response.text


def test_veritabani_yoksa_siteye_bagli_uc_500_doner(client: TestClient) -> None:
    from site_yonetim.api.deps import SiteContextDep

    app = client.app

    @app.get("/api/v1/sites/{slug}/_probe")  # type: ignore[attr-defined, untyped-decorator]
    async def probe(ctx: SiteContextDep) -> None:
        return None

    response = client.get("/api/v1/sites/aksu-konaklari/_probe")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


async def test_demo_yuklemesi_basarisiz_olsa_da_uygulama_acilir(
    make_settings: SettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import site_yonetim.main as main_module

    async def broken_seed(*_: object) -> bool:
        raise RuntimeError("tablo yok")

    monkeypatch.setattr(main_module, "seed_demo", broken_seed)
    settings = make_settings(
        environment="development",
        seed_demo_data=True,
        database_url="postgresql+asyncpg://u:p@127.0.0.1:1/db",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.session_factory is not None

    # create_app kök log işleyicisini JSON/stdout olarak kurar
    assert "Demo verisi yüklenemedi" in capsys.readouterr().out
