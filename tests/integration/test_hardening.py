"""Güvenlik sertleştirme — IP bazlı giriş hız sınırı ve geçici parolada zorunlu değişiklik
(docs/05 §8.1, docs/09 §4; gerçek PostgreSQL).
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim import cli
from site_yonetim.api.deps import get_now
from site_yonetim.core.config import get_settings
from site_yonetim.models import AuthSession, LoginThrottle, User
from site_yonetim.services import login_throttle
from site_yonetim.services.provisioning import provision_site
from tests.integration.conftest import DatabaseUrls
from tests.integration.helpers import DEFAULT_PASSWORD, add_site_membership, create_user

Factory = async_sessionmaker[AsyncSession]
LOGIN = "/api/v1/auth/login"
CHANGE = "/api/v1/auth/change-password"
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
NEW_PASSWORD = "Yeni-parola-2026"  # gitleaks:allow — test parolası


@pytest.fixture
def limited(api_app: FastAPI) -> FastAPI:
    api_app.state.settings = api_app.state.settings.model_copy(
        update={"login_ip_max_failures": 3, "login_ip_window_minutes": 15}
    )
    api_app.dependency_overrides[get_now] = lambda: NOW
    return api_app


@pytest.fixture
async def other_ip(api_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=api_app, client=("203.0.113.9", 4000))
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _login(
    api: httpx2.AsyncClient, email: str, password: str = DEFAULT_PASSWORD
) -> httpx2.Response:
    return await api.post(LOGIN, json={"email": email, "password": password})


# --- IP bazlı hız sınırı --------------------------------------------------------------


async def test_ayni_adresten_cok_hatali_deneme_429(
    limited: FastAPI,
    api: httpx2.AsyncClient,
    other_ip: httpx2.AsyncClient,
    session_factory: Factory,
    admin_engine: object,
) -> None:
    await create_user(session_factory, "ayse@test.local")
    # parola püskürtme: farklı hesaplar, aynı adres — hesap kilidi devreye girmez
    statuses = [(await _login(api, f"kisi{i}@test.local", "yanlis")).status_code for i in range(3)]
    assert statuses == [401, 401, 401]

    blocked = await _login(api, "ayse@test.local")  # doğru parola da denenmez
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "too_many_attempts"
    assert blocked.json()["error"]["message"] == (
        "Bu ağdan çok fazla hatalı giriş denemesi yapıldı. 15 dakika sonra tekrar deneyin."
    )
    assert blocked.headers["retry-after"] == "900"
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "ayse@test.local"))
    assert user is not None
    assert user.failed_login_count == 0  # hesap sayacına dokunulmadı

    # başka adres etkilenmez
    assert (await _login(other_ip, "ayse@test.local")).status_code == 200

    # pencere bitince sayaç sıfırdan başlar
    limited.dependency_overrides[get_now] = lambda: NOW + timedelta(minutes=15)
    assert (await _login(api, "ayse@test.local")).status_code == 200


async def test_basarili_giris_sayilmaz(
    limited: FastAPI, api: httpx2.AsyncClient, session_factory: Factory, admin_engine: object
) -> None:
    await create_user(session_factory, "ayse@test.local")
    assert (await _login(api, "ayse@test.local", "yanlis")).status_code == 401
    for _ in range(5):
        assert (await _login(api, "ayse@test.local")).status_code == 200
    async with session_factory() as session:
        failures = await session.scalar(select(LoginThrottle.failures))
    assert failures == 1  # başarılı giriş sayaca dokunmaz (ne artırır ne sıfırlar)


async def test_basarili_giris_araya_girerek_sinir_sifirlanamaz(
    limited: FastAPI, api: httpx2.AsyncClient, session_factory: Factory, admin_engine: object
) -> None:
    await create_user(session_factory, "saldirgan@test.local")
    statuses = []
    for i in range(4):
        statuses.append((await _login(api, f"kurban{i}@test.local", "yanlis")).status_code)
        statuses.append((await _login(api, "saldirgan@test.local")).status_code)
    assert statuses == [401, 200, 401, 200, 401, 429, 429, 429]


async def test_paralel_denemeler_siniri_asamaz(
    limited: FastAPI, api: httpx2.AsyncClient, session_factory: Factory, admin_engine: object
) -> None:
    responses = await asyncio.gather(
        *(_login(api, f"kisi{i}@test.local", "yanlis") for i in range(10))
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [401] * 3 + [429] * 7


async def test_eski_sayaclar_temizlenir(
    limited: FastAPI, api: httpx2.AsyncClient, session_factory: Factory, admin_engine: object
) -> None:
    await _login(api, "kimse@test.local", "yanlis")
    window = timedelta(minutes=15)
    assert (
        await login_throttle.purge(session_factory, now=NOW + timedelta(minutes=5), window=window)
        == 0
    )
    assert await login_throttle.purge(session_factory, now=NOW + window, window=window) == 1


def test_komut_satiri_sayaclari_temizler(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
    admin_engine: object,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    try:
        assert cli.main(["purge-login-throttle"]) == 0
    finally:
        get_settings.cache_clear()
    assert "dolmuş giriş sayacı silindi" in capsys.readouterr().out


# --- geçici parola ------------------------------------------------------------------


@pytest.fixture
async def temporary(session_factory: Factory, admin_engine: object) -> User:
    user = await create_user(session_factory, "yeni@test.local", full_name="Yeni YÖNETİCİ")
    async with session_factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == user.id).values(must_change_password=True)
        )
    return user


async def _bearer(api: httpx2.AsyncClient, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
    response = await _login(api, "yeni@test.local", password)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_gecici_parolada_yalniz_me_ve_degisiklik(
    api: httpx2.AsyncClient, temporary: User, session_factory: Factory, admin_engine: object
) -> None:
    async with session_factory() as session, session.begin():
        site_id = (await provision_site(session, name="Aksu Konakları")).id
    await add_site_membership(session_factory, site_id, temporary.id, "Yönetici")
    login = await _login(api, "yeni@test.local")
    assert login.json()["must_change_password"] is True
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    me = await api.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    assert len(me.json()["sites"]) == 1
    for path in ("/api/v1/sites/aksu-konaklari", "/api/v1/portfolio"):
        blocked = await api.get(path, headers=headers)
        assert blocked.status_code == 403, path
        assert blocked.json()["error"]["code"] == "password_change_required"

    changed = await api.post(
        CHANGE,
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert changed.status_code == 204, changed.text
    assert (await api.get("/api/v1/sites/aksu-konaklari", headers=headers)).status_code == 200
    me = await api.get("/api/v1/me", headers=headers)
    assert me.json()["must_change_password"] is False
    assert (await _login(api, "yeni@test.local")).status_code == 401  # eski parola geçmez
    assert (await _login(api, "yeni@test.local", NEW_PASSWORD)).json()[
        "must_change_password"
    ] is False


async def test_parola_degisikligi_kurallari(
    api: httpx2.AsyncClient, temporary: User, session_factory: Factory
) -> None:
    headers = await _bearer(api)
    for current, new, code, field in (
        ("yanlis", NEW_PASSWORD, "invalid_current_password", "current_password"),
        (DEFAULT_PASSWORD, "kisa", "weak_password", "new_password"),
        (DEFAULT_PASSWORD, DEFAULT_PASSWORD, "password_unchanged", "new_password"),
    ):
        response = await api.post(
            CHANGE, json={"current_password": current, "new_password": new}, headers=headers
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == code
        assert field in str(error)
    async with session_factory() as session:
        user = await session.get(User, temporary.id)
    assert user is not None
    assert (user.must_change_password, user.failed_login_count) == (True, 1)


async def test_hatali_mevcut_parola_hesabi_kilitler(
    api: httpx2.AsyncClient, temporary: User
) -> None:
    headers = await _bearer(api)
    body = {"current_password": "yanlis", "new_password": NEW_PASSWORD}
    statuses = [(await api.post(CHANGE, json=body, headers=headers)).status_code for _ in range(5)]
    assert statuses == [422, 422, 422, 422, 429]
    right = {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD}
    locked = await api.post(CHANGE, json=right, headers=headers)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"


async def test_degisiklik_diger_oturumlari_kapatir(
    api: httpx2.AsyncClient, temporary: User, session_factory: Factory
) -> None:
    other_device = await _bearer(api)
    this_device = await _bearer(api)
    changed = await api.post(
        CHANGE,
        json={"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        headers=this_device,
    )
    assert changed.status_code == 204
    assert (await api.get("/api/v1/me", headers=other_device)).status_code == 401
    assert (await api.get("/api/v1/me", headers=this_device)).status_code == 200
    async with session_factory() as session:
        sessions = list(await session.scalars(select(AuthSession)))
    assert sorted(s.revoked_at is None for s in sessions) == [False, True]
