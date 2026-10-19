"""Oturumlar, cihazlar ve kimlik olayları — Yazılım Dokümanı PLT-10, M-24 (gerçek PostgreSQL).

Bitti ölçütü: her giriş/başarısız giriş/kilit/çıkış/parola/oturum kapatma değişmez olarak
yazılır; bilinmeyen e-postada e-posta yazılmaz; kullanıcı yalnız kendi oturumlarını görür ve
kapatır; kapatılan oturumun jetonu hemen geçersizdir.
"""

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.models import AuthEvent, AuthSession, User
from tests.integration.conftest import DEFAULT_PASSWORD, create_user

Factory = async_sessionmaker[AsyncSession]
LOGIN = "/api/v1/auth/login"
EMAIL = "ayse@test.local"
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


@pytest.fixture
async def user(session_factory: Factory, admin_engine: object) -> User:
    return await create_user(session_factory, EMAIL, full_name="Ayşe YILMAZ")


async def login(api: httpx2.AsyncClient, agent: str, password: str = DEFAULT_PASSWORD) -> str:
    response = await api.post(
        LOGIN, json={"email": EMAIL, "password": password}, headers={"User-Agent": agent}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def events(factory: Factory) -> list[tuple[str, object]]:
    async with factory() as session:
        rows = await session.scalars(select(AuthEvent).order_by(AuthEvent.created_at))
        return [(e.kind, e.user_id) for e in rows]


async def test_giris_olaylari_yazilir(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    await login(api, CHROME)
    wrong = await api.post(LOGIN, json={"email": EMAIL, "password": "yanlis-parola"})
    assert wrong.status_code == 401
    unknown = await api.post(LOGIN, json={"email": "yok@test.local", "password": "x"})
    assert unknown.status_code == 401

    assert await events(session_factory) == [
        ("login_succeeded", user.id),
        ("login_failed", user.id),
        ("login_failed", None),  # bilinmeyen e-posta: kişi yok, e-posta yazılmaz
    ]
    async with session_factory() as session:
        success = await session.scalar(select(AuthEvent).where(AuthEvent.kind == "login_succeeded"))
        assert success is not None
        assert (success.user_agent, success.ip) == (CHROME, "127.0.0.1")
        assert success.request_id
        opened = await session.get(AuthSession, success.session_id)
        assert opened is not None
        assert opened.user_agent == CHROME


async def test_bes_hatali_denemede_kilit_olayi(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    for _ in range(5):
        await api.post(LOGIN, json={"email": EMAIL, "password": "yanlis-parola"})
    locked = await api.post(LOGIN, json={"email": EMAIL, "password": DEFAULT_PASSWORD})
    assert locked.status_code == 429
    kinds = [kind for kind, _ in await events(session_factory)]
    assert kinds.count("login_failed") == 6  # 5 hatalı + kilitliyken doğru parola
    assert kinds.count("account_locked") == 1


async def test_oturum_listesi_ve_tek_cihazi_kapatma(api: httpx2.AsyncClient, user: User) -> None:
    laptop = await login(api, CHROME)
    phone = await login(api, IPHONE)

    listed = await api.get("/api/v1/me/sessions", headers=bearer(laptop))
    assert listed.status_code == 200, listed.text
    sessions = listed.json()
    assert sorted(s["device"] for s in sessions) == ["Chrome · Windows", "Safari · iPhone"]
    [current] = [s for s in sessions if s["is_current"]]
    assert current["device"] == "Chrome · Windows"
    other = next(s for s in sessions if not s["is_current"])

    assert (await api.get("/api/v1/me", headers=bearer(phone))).status_code == 200
    closed = await api.delete(f"/api/v1/me/sessions/{other['id']}", headers=bearer(laptop))
    assert closed.status_code == 204
    # kapatılan cihazın jetonu bir sonraki istekte geçersiz
    assert (await api.get("/api/v1/me", headers=bearer(phone))).status_code == 401
    again = await api.delete(f"/api/v1/me/sessions/{other['id']}", headers=bearer(laptop))
    assert again.status_code == 404
    assert [
        s["is_current"]
        for s in (await api.get("/api/v1/me/sessions", headers=bearer(laptop))).json()
    ] == [True]


async def test_baskasinin_oturumu_kapatilamaz(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    await create_user(session_factory, "mehmet@test.local")
    mehmet = await api.post(
        LOGIN, json={"email": "mehmet@test.local", "password": DEFAULT_PASSWORD}
    )
    mehmet_token = mehmet.json()["access_token"]
    ayse = await login(api, CHROME)
    [ayse_session] = (await api.get("/api/v1/me/sessions", headers=bearer(ayse))).json()
    attempt = await api.delete(
        f"/api/v1/me/sessions/{ayse_session['id']}", headers=bearer(mehmet_token)
    )
    assert attempt.status_code == 404
    assert (await api.get("/api/v1/me", headers=bearer(ayse))).status_code == 200


async def test_tum_cihazlardan_cikis(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    first = await login(api, CHROME)
    second = await login(api, IPHONE)
    third = await login(api, CHROME)

    kept = await api.delete("/api/v1/me/sessions", headers=bearer(first))
    assert kept.status_code == 200, kept.text
    assert kept.json() == {
        "data": {"revoked": 2, "current_revoked": False},
        "message": "2 cihazdaki oturum kapatıldı; bu cihazda oturumunuz açık.",
    }
    assert (await api.get("/api/v1/me", headers=bearer(second))).status_code == 401
    assert (await api.get("/api/v1/me", headers=bearer(third))).status_code == 401
    assert (await api.get("/api/v1/me", headers=bearer(first))).status_code == 200

    everything = await api.delete(
        "/api/v1/me/sessions", params={"keep_current": "false"}, headers=bearer(first)
    )
    assert everything.json()["data"] == {"revoked": 1, "current_revoked": True}
    assert (await api.get("/api/v1/me", headers=bearer(first))).status_code == 401
    kinds = [kind for kind, _ in await events(session_factory)]
    assert kinds.count("session_revoked") == 3


async def test_giris_gecmisi_ve_degistirilemezlik(
    api: httpx2.AsyncClient, user: User, session_factory: Factory, admin_engine: object
) -> None:
    await api.post(LOGIN, json={"email": EMAIL, "password": "yanlis-parola"})
    token = await login(api, IPHONE)
    history = await api.get("/api/v1/me/security-events", headers=bearer(token))
    assert history.status_code == 200, history.text
    assert [(e["kind"], e["label"], e["device"]) for e in history.json()] == [
        ("login_succeeded", "Giriş yapıldı", "Safari · iPhone"),
        ("login_failed", "Hatalı giriş denemesi", "Bilinmeyen cihaz"),
    ]

    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="Geçmiş kaydı değiştirilemez"):
            await session.execute(text("UPDATE auth_events SET kind = 'logout'"))
    async with session_factory() as session:
        with pytest.raises(DBAPIError, match="Geçmiş kaydı değiştirilemez"):
            await session.execute(text("DELETE FROM auth_events"))


async def test_cikis_ve_parola_degisikligi_olayi(
    api: httpx2.AsyncClient, user: User, session_factory: Factory
) -> None:
    token = await login(api, CHROME)
    changed = await api.post(
        "/api/v1/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Yeni-Guclu-Parola-2026"},
        headers=bearer(token),
    )
    assert changed.status_code == 204, changed.text
    logout = await api.post("/api/v1/auth/logout", headers=bearer(token))
    assert logout.status_code == 204
    kinds = [kind for kind, _ in await events(session_factory)]
    assert kinds == ["login_succeeded", "password_changed", "logout"]


async def test_hata_govdesi_istek_kimligini_tasir(api: httpx2.AsyncClient, user: User) -> None:
    token = await login(api, CHROME)
    response = await api.get("/api/v1/sites/yok-boyle-site/units", headers=bearer(token))
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["request_id"] == response.headers["X-Request-ID"]
    sent = await api.get(
        "/api/v1/sites/yok-boyle-site/units",
        headers={**bearer(token), "X-Request-ID": "destek-0123456789"},
    )
    assert sent.json()["error"]["request_id"] == "destek-0123456789"
