from fastapi.testclient import TestClient

from site_yonetim.main import create_app
from tests.conftest import SettingsFactory

PROD = {"environment": "production", "jwt_secret": "x" * 40, "api_docs_enabled": False}


def test_api_yanitinda_guvenlik_basliklari(client: TestClient) -> None:
    headers = client.get("/api/v1/health").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]


def test_hsts_yalniz_uretimde(client: TestClient, make_settings: SettingsFactory) -> None:
    assert "Strict-Transport-Security" not in client.get("/api/v1/health").headers

    with TestClient(create_app(make_settings(**PROD))) as prod_client:
        headers = prod_client.get("/api/v1/health").headers
    assert headers["Strict-Transport-Security"].startswith("max-age=")


def test_hata_yanitinda_da_guvenlik_basliklari(client: TestClient) -> None:
    headers = client.get("/api/v1/yok").headers

    assert headers["X-Content-Type-Options"] == "nosniff"


def test_istek_kimligi_uretilir_ve_yanita_yazilir(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert len(response.headers["X-Request-ID"]) == 32


def test_guvenli_istek_kimligi_korunur(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "frontend-abc-12345"})

    assert response.headers["X-Request-ID"] == "frontend-abc-12345"


def test_guvensiz_istek_kimligi_yenisiyle_degistirilir(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "kotu deger; x=1"})

    assert response.headers["X-Request-ID"] != "kotu deger; x=1"
    assert len(response.headers["X-Request-ID"]) == 32


def test_izinli_origin_cors_alir(client: TestClient) -> None:
    response = client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )

    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert response.headers["Access-Control-Allow-Credentials"] == "true"


def test_izinsiz_origin_cors_alamaz(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Origin": "https://kotu.example"})

    assert "Access-Control-Allow-Origin" not in response.headers


def test_izinsiz_host_reddedilir(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"Host": "saldirgan.example"})

    assert response.status_code == 400


def test_uretimde_api_dokumani_kapatilabilir(make_settings: SettingsFactory) -> None:
    with TestClient(create_app(make_settings(**PROD))) as prod_client:
        assert prod_client.get("/api/v1/openapi.json").status_code == 404
        assert prod_client.get("/api/v1/docs").status_code == 404
