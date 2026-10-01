"""Kimlik uçları — docs/05 §8, docs/06 §2.1, docs/07 §8.9 (gerçek PostgreSQL)."""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_now
from site_yonetim.core.security import create_access_token
from site_yonetim.models import AuthSession, User
from tests.integration.conftest import DEFAULT_PASSWORD, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
LOGIN = "/api/v1/auth/login"
EMAIL = "ayse@test.local"


@pytest.fixture
async def user(session_factory: Factory, admin_engine: object) -> User:
    return await create_user(session_factory, EMAIL, full_name="Ayşe YILMAZ")


def _fix_time(app: FastAPI, moment: datetime) -> None:
    app.dependency_overrides[get_now] = lambda: moment


async def _login(api: httpx2.AsyncClient, password: str, email: str = EMAIL) -> httpx2.Response:
    return await api.post(LOGIN, json={"email": email, "password": password})


async def test_giris_erisim_jetonu_ve_httponly_cerez(api: httpx2.AsyncClient, user: User) -> None:
    response = await _login(api, DEFAULT_PASSWORD, email="  AYSE@test.local ")

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    cookie = response.headers["set-cookie"]
    assert "sy_refresh=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1/auth" in cookie
    assert response.headers["cache-control"] == "no-store"


async def test_uretimde_cerez_secure(
    api_app: FastAPI, api: httpx2.AsyncClient, user: User, make_settings: object
) -> None:
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"refresh_cookie_secure": True}
    )

    response = await _login(api, DEFAULT_PASSWORD)

    assert "Secure" in response.headers["set-cookie"]


async def test_yanlis_parola_ve_bilinmeyen_eposta_ayni_yanit(
    api: httpx2.AsyncClient, user: User
) -> None:
    wrong_password = await _login(api, "yanlis-parola")
    unknown_email = await _login(api, DEFAULT_PASSWORD, email="kimse@test.local")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["error"]["message"] == "E-posta veya parola hatalı."
    assert "set-cookie" not in wrong_password.headers


async def test_8_9_bes_hatali_giristen_sonra_dogru_parola_reddedilir(
    api_app: FastAPI, api: httpx2.AsyncClient, user: User
) -> None:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    _fix_time(api_app, now)
    statuses = [(await _login(api, "yanlis")).status_code for _ in range(5)]

    locked = await _login(api, DEFAULT_PASSWORD)

    assert statuses == [401, 401, 401, 401, 429]
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"
    assert "15 dakika" in locked.json()["error"]["message"]

    _fix_time(api_app, now + timedelta(minutes=14, seconds=59))
    assert (await _login(api, DEFAULT_PASSWORD)).status_code == 429
    _fix_time(api_app, now + timedelta(minutes=15))
    assert (await _login(api, DEFAULT_PASSWORD)).status_code == 200


async def test_basarili_giris_hata_sayacini_sifirlar(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    for _ in range(3):
        await _login(api, "yanlis")
    assert (await _login(api, DEFAULT_PASSWORD)).status_code == 200

    async with session_factory() as session:
        stored = await session.get(User, user.id)
    assert stored is not None
    assert stored.failed_login_count == 0
    assert stored.last_login_at is not None


async def test_pasif_kullanici_giremez(api: httpx2.AsyncClient, session_factory: Factory) -> None:
    await create_user(session_factory, "pasif@test.local", is_active=False)

    response = await _login(api, DEFAULT_PASSWORD, email="pasif@test.local")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "E-posta veya parola hatalı."


async def test_me_kimliksiz_401_ve_bearer_basligi(api: httpx2.AsyncClient) -> None:
    response = await api.get("/api/v1/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("header", ["Bearer bozuk.jeton.degeri", "Basic dXNlcjpwYXNz", "Bearer"])
async def test_gecersiz_jeton_401(api: httpx2.AsyncClient, user: User, header: str) -> None:
    response = await api.get("/api/v1/me", headers={"Authorization": header})

    assert response.status_code == 401


async def test_me_kullanici_bilgisi(api: httpx2.AsyncClient, user: User) -> None:
    response = await api.get("/api/v1/me", headers=await login_headers(api, EMAIL))

    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(user.id),
        "full_name": "Ayşe YILMAZ",
        "kind": "staff",
        "is_platform_admin": False,
        "can_see_portfolio": False,
        "must_change_password": False,
        "sites": [],
    }


async def test_yenileme_jetonu_doner_ve_yeni_erisim_jetonu_verir(
    api: httpx2.AsyncClient, user: User
) -> None:
    await _login(api, DEFAULT_PASSWORD)
    first_cookie = api.cookies.get("sy_refresh")

    response = await api.post("/api/v1/auth/refresh")

    assert response.status_code == 200
    assert api.cookies.get("sy_refresh") != first_cookie
    me = await api.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
    )
    assert me.status_code == 200


async def test_eski_yenileme_jetonu_tekrar_kullanilirsa_oturum_iptal(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    await _login(api, DEFAULT_PASSWORD)
    stolen = api.cookies.get("sy_refresh")
    assert (await api.post("/api/v1/auth/refresh")).status_code == 200
    legit = api.cookies.get("sy_refresh")
    assert stolen is not None
    assert legit is not None

    # Saldırgan eski jetonu kullanır → reddedilir ve oturum tümden iptal edilir.
    api.cookies.set("sy_refresh", stolen, path="/api/v1/auth")
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401
    api.cookies.set("sy_refresh", legit, path="/api/v1/auth")
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401

    async with session_factory() as session:
        sessions = (await session.scalars(select(AuthSession))).all()
    assert all(s.revoked_at is not None for s in sessions)


async def test_cerezsiz_yenileme_401(api: httpx2.AsyncClient) -> None:
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401


async def test_oturum_suresi_dolunca_yenileme_401(
    api_app: FastAPI, api: httpx2.AsyncClient, user: User
) -> None:
    now = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    _fix_time(api_app, now)
    await _login(api, DEFAULT_PASSWORD)

    _fix_time(api_app, now + timedelta(hours=8))
    response = await api.post("/api/v1/auth/refresh")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "session_expired"


async def test_cikis_aninda_etkili_erisim_jetonu_da_gecersiz(
    api: httpx2.AsyncClient, user: User
) -> None:
    headers = await login_headers(api, EMAIL)
    assert (await api.get("/api/v1/me", headers=headers)).status_code == 200

    response = await api.post("/api/v1/auth/logout", headers=headers)

    assert response.status_code == 204
    assert (await api.get("/api/v1/me", headers=headers)).status_code == 401
    assert (await api.post("/api/v1/auth/refresh")).status_code == 401


async def test_cikis_oturumsuz_da_204(api: httpx2.AsyncClient) -> None:
    assert (await api.post("/api/v1/auth/logout")).status_code == 204


async def test_yabanci_kaynaktan_cerezli_istek_reddedilir(
    api: httpx2.AsyncClient, user: User
) -> None:
    await _login(api, DEFAULT_PASSWORD)

    evil = await api.post("/api/v1/auth/refresh", headers={"Origin": "https://kotu.example"})
    good = await api.post("/api/v1/auth/refresh", headers={"Origin": "http://localhost:5173"})

    assert evil.status_code == 403
    assert good.status_code == 200


async def test_iptal_edilmis_oturumun_jetonu_gecersiz(
    api: httpx2.AsyncClient, api_app: FastAPI, user: User, session_factory: Factory
) -> None:
    await _login(api, DEFAULT_PASSWORD)
    async with session_factory() as session, session.begin():
        auth_session = await session.scalar(select(AuthSession))
        assert auth_session is not None
        auth_session.revoked_at = datetime.now(UTC)
    token = create_access_token(
        api_app.state.settings, user_id=user.id, session_id=auth_session.id, now=datetime.now(UTC)
    )

    response = await api.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


async def test_devre_disi_birakilan_kullanicinin_jetonu_gecersiz(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    headers = await login_headers(api, EMAIL)
    async with session_factory() as session, session.begin():
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.is_active = False

    assert (await api.get("/api/v1/me", headers=headers)).status_code == 401


async def test_bozuk_jetonla_cikis_yine_204_ve_cerezle_oturum_kapanir(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    await _login(api, DEFAULT_PASSWORD)

    response = await api.post("/api/v1/auth/logout", headers={"Authorization": "Bearer bozuk"})

    assert response.status_code == 204
    async with session_factory() as session:
        auth_session = await session.scalar(select(AuthSession))
    assert auth_session is not None
    assert auth_session.revoked_at is not None


async def test_pasiflestirilen_kullanici_oturum_yenileyemez(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    await _login(api, DEFAULT_PASSWORD)
    async with session_factory() as session, session.begin():
        stored = await session.get(User, user.id)
        assert stored is not None
        stored.is_active = False

    assert (await api.post("/api/v1/auth/refresh")).status_code == 401


async def test_zayif_parametreli_eski_hash_giriste_yukseltilir(
    api: httpx2.AsyncClient, session_factory: Factory
) -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    async with session_factory() as session, session.begin():
        session.add(User(email="eski@test.local", password_hash=weak, full_name="Eski HESAP"))

    assert (await _login(api, DEFAULT_PASSWORD, email="eski@test.local")).status_code == 200

    async with session_factory() as session:
        stored = await session.scalar(select(User).where(User.email == "eski@test.local"))
    assert stored is not None
    assert stored.password_hash != weak
    assert (await _login(api, DEFAULT_PASSWORD, email="eski@test.local")).status_code == 200
