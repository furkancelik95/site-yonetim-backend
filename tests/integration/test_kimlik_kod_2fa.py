"""Kimlik: SMS koduyla giriş, iki adımlı doğrulama, parola sıfırlama, telefon değişikliği, step-up
— Yazılım Dokümanı PLT-01, PLT-10, §4.7 (PM kararı P5). Gerçek PostgreSQL.

Saat `get_now` ile sabitlenir; test sağlayıcısının kodu yanıttaki `test_code`'dan okunur
(test ortamında açık, üretimde kapalı).
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx2
import pytest
from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import CurrentUser, get_now, require_step_up
from site_yonetim.domain.mfa import totp
from site_yonetim.models import AuthEvent, OtpChallenge, User
from site_yonetim.services.channels import TEST_INBOX
from tests.integration.finance_world import World
from tests.integration.helpers import DEFAULT_PASSWORD, add_site_membership, create_user

Factory = async_sessionmaker[AsyncSession]
PHONE = "+905321234567"
NEW_PW = "Yeni-Parola-2026"  # gitleaks:allow — test parolası


class Clock:
    def __init__(self) -> None:
        # Erişim jetonu (JWT, 15 dk) gerçek saatle denetlenir: saat gerçeğin 10 dk gerisinden
        # başlar, jetonlu testlerde 10 dakikadan fazla ilerletilmez (gelecekte iat reddedilir).
        self.now = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(api_app: FastAPI) -> Iterator[Clock]:
    c = Clock()
    api_app.dependency_overrides[get_now] = lambda: c.now
    yield c
    api_app.dependency_overrides.pop(get_now, None)


async def request_code(api: httpx2.AsyncClient, phone: str = PHONE) -> httpx2.Response:
    return await api.post("/api/v1/auth/otp/request", json={"phone": phone})


async def verify(api: httpx2.AsyncClient, code: str, phone: str = PHONE) -> httpx2.Response:
    return await api.post("/api/v1/auth/otp/verify", json={"phone": phone, "code": code})


async def events(factory: Factory, user_id: uuid.UUID | None = None) -> list[str]:
    async with factory() as session:
        query = select(AuthEvent.kind).order_by(AuthEvent.created_at, AuthEvent.id)
        if user_id is not None:
            query = query.where(AuthEvent.user_id == user_id)
        return list(await session.scalars(query))


async def resident(factory: Factory, site_id: uuid.UUID, phone: str = PHONE) -> User:
    async with factory() as session, session.begin():
        user = User(phone=phone, email=None, password_hash=None, full_name="Elif SAKİN",
                    kind="resident")  # fmt: skip
        session.add(user)
    await add_site_membership(factory, site_id, user.id, "Sakin")
    return user


def bearer(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


# --- Telefonla giriş ----------------------------------------------------------------------


async def test_kayitli_ve_kayitsiz_numaraya_ayni_yanit(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    await resident(world.factory, world.site_id)
    known = await request_code(api, "0532 123 45 67")
    unknown = await request_code(api, "05329998877")
    assert known.status_code == unknown.status_code == 200
    a, b = known.json(), unknown.json()
    assert a["message"] == b["message"] == "Kod gönderildi. 3 dakika içinde girin."
    assert set(a["data"]) == set(b["data"])
    assert a["data"]["sent_to"] == "+90******4567"
    assert a["data"]["resend_after_seconds"] == 60
    assert len(a["data"]["test_code"]) == 6
    # kod düz metin saklanmaz
    async with world.factory() as session:
        stored = list(await session.scalars(select(OtpChallenge.code_digest)))
    assert a["data"]["test_code"] not in str(stored)
    assert all(len(d) == 64 for d in stored)
    # kayıtsız numara: kod doğrulanınca 404 no_account (kayıt başvurusu)
    none = await verify(api, b["data"]["test_code"], "05329998877")
    assert none.status_code == 404
    assert none.json()["error"]["code"] == "no_account"


async def test_kodla_giris_ve_hatalar(api: httpx2.AsyncClient, world: World, clock: Clock) -> None:
    user = await resident(world.factory, world.site_id)
    code = (await request_code(api)).json()["data"]["test_code"]
    bad = await verify(api, wrong(code))
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "invalid_code"
    assert bad.json()["error"]["message"] == "Kod hatalı. Kalan deneme: 4"
    ok = await verify(api, code)
    assert ok.status_code == 200, ok.text
    headers = {"Authorization": f"Bearer {ok.json()['access_token']}"}
    me = (await api.get("/api/v1/me", headers=headers)).json()
    assert (me["kind"], me["phone"], me["mfa_required"]) == ("resident", PHONE, False)
    # kod tek kullanımlık
    again = await verify(api, code)
    assert again.json()["error"]["code"] == "code_expired"
    # süresi dolan kod
    clock.advance(seconds=61)
    code2 = (await request_code(api)).json()["data"]["test_code"]
    clock.advance(minutes=3, seconds=1)
    expired = await verify(api, code2)
    assert expired.status_code == 422
    assert expired.json()["error"]["code"] == "code_expired"
    kinds = await events(world.factory, user.id)
    assert {"code_sent", "code_failed", "login_succeeded"} <= set(kinds)


async def test_oran_sinirlari_60_sn_gunde_10_ve_kilit(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    first = await request_code(api)
    assert first.status_code == 200
    soon = await request_code(api)
    assert soon.status_code == 429
    assert soon.json()["error"]["code"] == "code_too_soon"
    assert int(soon.headers["retry-after"]) == 60
    for _ in range(9):
        clock.advance(seconds=61)
        assert (await request_code(api)).status_code == 200
    clock.advance(seconds=61)
    daily = await request_code(api)
    assert daily.status_code == 429
    assert daily.json()["error"]["code"] == "code_daily_limit"

    # başka numarada: 5 hatalı denemede 15 dk kilit (S4)
    other = "05335554433"
    code = (await request_code(api, other)).json()["data"]["test_code"]
    for left in (4, 3, 2, 1):
        response = await verify(api, wrong(code), other)
        assert response.json()["error"]["message"] == f"Kod hatalı. Kalan deneme: {left}"
    fifth = await verify(api, wrong(code), other)
    assert fifth.status_code == 429
    assert fifth.json()["error"]["code"] == "code_locked"
    sixth = await verify(api, code, other)
    assert sixth.status_code == 429
    clock.advance(seconds=61)
    assert (await request_code(api, other)).json()["error"]["code"] == "code_locked"
    clock.advance(minutes=15)
    assert (await request_code(api, other)).status_code == 200
    assert "code_locked" in await events(world.factory)


async def test_pasif_hesap_ve_dogrulama(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    user = await resident(world.factory, world.site_id)
    async with world.factory() as session, session.begin():
        await session.execute(
            text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user.id}
        )
    code = (await request_code(api)).json()["data"]["test_code"]
    closed = await verify(api, code)
    assert closed.status_code == 403
    assert closed.json()["error"]["code"] == "account_disabled"
    invalid = await request_code(api, "123")
    assert invalid.status_code == 422
    assert "phone" in invalid.json()["error"]["fields"]


# --- İki adımlı doğrulama -------------------------------------------------------------------


async def setup_mfa(
    api: httpx2.AsyncClient, headers: dict[str, str], now: datetime
) -> tuple[str, list[str]]:
    setup = await api.post("/api/v1/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    secret = body["secret"]
    bad = await api.post("/api/v1/auth/mfa/enable", json={"code": "000000"}, headers=headers)
    assert bad.status_code == 422
    enabled = await api.post(
        "/api/v1/auth/mfa/enable", json={"code": totp(secret, now)}, headers=headers
    )
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()["data"]["codes"]
    assert len(codes) == 10
    return secret, codes


async def password_login(api: httpx2.AsyncClient, email: str) -> httpx2.Response:
    return await api.post("/api/v1/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})


async def test_yonetici_2fa_kurulum_ve_girisi(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    login = await password_login(api, "yonetici@test.local")
    token = login.json()
    assert token["mfa_required"] is False
    assert token["mfa_setup_deadline"] is not None  # geçiş süresi (yönetici rolü)
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    me = (await api.get("/api/v1/me", headers=headers)).json()
    assert (me["mfa_required"], me["mfa_enabled"], me["mfa_setup_required"]) == (True, False, False)

    secret, codes = await setup_mfa(api, headers, clock.now)
    security = (await api.get("/api/v1/me/security", headers=headers)).json()
    assert security["mfa"]["enabled"] is True
    assert security["mfa"]["recovery_codes_left"] == 10

    # yeni giriş: kod girilmeden her uç 401 mfa_required (S2)
    clock.advance(minutes=1)
    pending = await password_login(api, "yonetici@test.local")
    assert pending.json()["mfa_required"] is True
    ph = {"Authorization": f"Bearer {pending.json()['access_token']}"}
    for path in ("/api/v1/me", "/api/v1/sites/aksu-konaklari", "/api/v1/me/sessions"):
        blocked = await api.get(path, headers=ph)
        assert blocked.status_code == 401
        assert blocked.json()["error"]["code"] == "mfa_required"
    wrong_code = await api.post("/api/v1/auth/mfa/verify", json={"code": "123456"}, headers=ph)
    assert wrong_code.status_code == 422
    ok = await api.post(
        "/api/v1/auth/mfa/verify", json={"code": totp(secret, clock.now)}, headers=ph
    )
    assert ok.status_code == 200, ok.text
    assert (await api.get("/api/v1/me", headers=ph)).status_code == 200
    # aynı TOTP adımı ikinci kez kullanılamaz; yedek kod bir kez
    again = await password_login(api, "yonetici@test.local")
    ah = {"Authorization": f"Bearer {again.json()['access_token']}"}
    replay = await api.post(
        "/api/v1/auth/mfa/verify", json={"code": totp(secret, clock.now)}, headers=ah
    )
    assert replay.status_code == 422
    rec = await api.post("/api/v1/auth/mfa/verify", json={"code": codes[0]}, headers=ah)
    assert rec.status_code == 200
    third = await password_login(api, "yonetici@test.local")
    th = {"Authorization": f"Bearer {third.json()['access_token']}"}
    reuse = await api.post("/api/v1/auth/mfa/verify", json={"code": codes[0]}, headers=th)
    assert reuse.status_code == 422
    # yönetici rolünde kapatılamaz
    disable = await api.post("/api/v1/auth/mfa/disable", json={"code": codes[1]}, headers=headers)
    assert disable.status_code == 409
    assert disable.json()["error"]["code"] == "mfa_required_for_role"
    # yedek kodları yenile
    clock.advance(seconds=30)
    fresh = await api.post(
        "/api/v1/auth/mfa/recovery-codes", json={"code": totp(secret, clock.now)}, headers=headers
    )
    assert fresh.status_code == 200
    assert set(fresh.json()["data"]["codes"]).isdisjoint(codes)
    kinds = await events(world.factory)
    assert {"mfa_enabled", "mfa_failed", "recovery_code_used", "recovery_codes_regenerated"} <= set(
        kinds
    )
    # sır şifreli saklanır
    async with world.factory() as session:
        sealed = await session.scalar(
            select(User.mfa_secret_sealed).where(User.email == "yonetici@test.local")
        )
    assert sealed is not None
    assert secret not in sealed


async def test_2fa_bes_hatada_kilit(api: httpx2.AsyncClient, world: World, clock: Clock) -> None:
    headers = bearer(await password_login(api, "yonetici@test.local"))
    await setup_mfa(api, headers, clock.now)
    pending = await password_login(api, "yonetici@test.local")
    ph = {"Authorization": f"Bearer {pending.json()['access_token']}"}
    for _ in range(4):
        r = await api.post("/api/v1/auth/mfa/verify", json={"code": "000001"}, headers=ph)
        assert r.status_code == 422
    locked = await api.post("/api/v1/auth/mfa/verify", json={"code": "000001"}, headers=ph)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"
    assert (await api.get("/api/v1/me", headers=ph)).status_code == 401


async def test_gecis_suresi_bitince_yalniz_kurulum(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    headers = bearer(await password_login(api, "yonetici@test.local"))
    async with world.factory() as session, session.begin():
        await session.execute(
            text("UPDATE users SET mfa_grace_until = :t WHERE email = 'yonetici@test.local'"),
            {"t": clock.now - timedelta(minutes=1)},
        )
    blocked = await api.get("/api/v1/sites/aksu-konaklari", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "mfa_setup_required"
    me = (await api.get("/api/v1/me", headers=headers)).json()
    assert me["mfa_setup_required"] is True
    assert (await api.get("/api/v1/me/security", headers=headers)).status_code == 200
    await setup_mfa(api, headers, clock.now)
    assert (await api.get("/api/v1/sites/aksu-konaklari", headers=headers)).status_code == 200

    # saha/sakin rolünde 2FA zorunlu değil: süre etkilemez
    tech = await create_user(world.factory, "teknik@test.local", full_name="Teknik KİŞİ")
    await add_site_membership(world.factory, world.site_id, tech.id, "Teknik Personel")
    t = (await password_login(api, "teknik@test.local")).json()
    assert t["mfa_setup_deadline"] is None


# --- Step-up -----------------------------------------------------------------------------


async def test_step_up_son_5_dakika(
    api: httpx2.AsyncClient, api_app: FastAPI, world: World, clock: Clock
) -> None:
    router = APIRouter()

    @router.post("/api/v1/_test/hassas")
    async def sensitive(
        current: Annotated[CurrentUser, Depends(require_step_up())],
    ) -> dict[str, str]:
        return {"ok": current.user.full_name}

    api_app.include_router(router)
    login = await password_login(api, "yonetici@test.local")
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await api.post("/api/v1/_test/hassas", headers=headers)).status_code == 200
    clock.advance(minutes=6)
    stale = await api.post("/api/v1/_test/hassas", headers=headers)
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "step_up_required"
    bad = await api.post("/api/v1/auth/step-up", json={"password": "yanlis"}, headers=headers)
    assert bad.status_code == 422
    good = await api.post(
        "/api/v1/auth/step-up", json={"password": DEFAULT_PASSWORD}, headers=headers
    )
    assert good.status_code == 200, good.text
    assert (await api.post("/api/v1/_test/hassas", headers=headers)).status_code == 200
    assert "step_up" in await events(world.factory)


# --- Parola sıfırlama ----------------------------------------------------------------------


async def test_parola_sifirlama_numaralandirma_yok(
    api: httpx2.AsyncClient, world: World, clock: Clock
) -> None:
    other = await password_login(api, "yonetici@test.local")
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    TEST_INBOX.clear()
    known = await api.post("/api/v1/auth/password/forgot", json={"email": "Yonetici@test.local"})
    unknown = await api.post("/api/v1/auth/password/forgot", json={"email": "yok@test.local"})
    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]
    assert [i.to for i in TEST_INBOX.items()] == ["yonetici@test.local"]  # yalnız kayıtlıya gider
    code = known.json()["data"]["test_code"]
    weak = await api.post(
        "/api/v1/auth/password/reset",
        json={"email": "yonetici@test.local", "code": code, "new_password": "kisa"},
    )
    assert weak.status_code == 422
    reset = await api.post(
        "/api/v1/auth/password/reset",
        json={"email": "yonetici@test.local", "code": code, "new_password": NEW_PW},
    )
    assert reset.status_code == 200, reset.text
    # bütün oturumlar kapandı, yeni parolayla giriş
    assert (await api.get("/api/v1/me", headers=other_headers)).status_code == 401
    login = await api.post(
        "/api/v1/auth/login",
        json={"email": "yonetici@test.local", "password": NEW_PW},
    )
    assert login.status_code == 200
    ghost = await api.post(
        "/api/v1/auth/password/reset",
        json={
            "email": "yok@test.local",
            "code": unknown.json()["data"]["test_code"],
            "new_password": NEW_PW,
        },
    )
    assert ghost.status_code == 422
    assert "password_reset" in await events(world.factory)


# --- Telefon değişikliği -------------------------------------------------------------------


async def test_telefon_degisikligi(api: httpx2.AsyncClient, world: World, clock: Clock) -> None:
    user = await resident(world.factory, world.site_id)
    code = (await request_code(api)).json()["data"]["test_code"]
    headers = {"Authorization": f"Bearer {(await verify(api, code)).json()['access_token']}"}
    sent = await api.post("/api/v1/me/phone-change", json={"phone": "05337776655"}, headers=headers)
    assert sent.status_code == 200, sent.text
    TEST_INBOX.clear()
    confirmed = await api.post(
        "/api/v1/me/phone-change/confirm",
        json={"phone": "05337776655", "code": sent.json()["data"]["test_code"]},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["phone"] == "+905337776655"
    assert [i.to for i in TEST_INBOX.items()] == [PHONE]  # eski numaraya bilgilendirme
    security = (await api.get("/api/v1/me/security", headers=headers)).json()
    assert security["phone"] == "+905337776655"
    assert security["phone_changed_at"] is not None
    assert "phone_changed" in await events(world.factory, user.id)

    # başka hesaptaki numara: kod doğrulandıktan sonra 409
    await resident(world.factory, world.site_id, "+905441112233")
    clock.advance(seconds=61)
    sent2 = await api.post(
        "/api/v1/me/phone-change", json={"phone": "05441112233"}, headers=headers
    )
    taken = await api.post(
        "/api/v1/me/phone-change/confirm",
        json={"phone": "05441112233", "code": sent2.json()["data"]["test_code"]},
        headers=headers,
    )
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "phone_taken"


async def test_kod_kaydi_sayisi(world: World, api: httpx2.AsyncClient, clock: Clock) -> None:
    await request_code(api)
    async with world.factory() as session:
        count = await session.scalar(select(func.count()).select_from(OtpChallenge))
    assert count == 1
