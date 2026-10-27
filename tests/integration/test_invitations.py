"""Kayıt yöntemleri ve davetler — servis isteği 23 (YAP-07, YAP-08)."""

from typing import Any

import httpx2

from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, set_lifecycle, staff

PUBLIC = "/api/v1/public/invitations"


def _invite(world: World, **changes: Any) -> dict[str, Any]:
    return {
        "full_name": "Test Deneme",
        "phone": "0532 111 22 33",
        "unit_id": world.unit_id,
        "relation": "tenant",
        **changes,
    }


async def _token(response: httpx2.Response) -> str:
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["link"]).removeprefix("/davet/")


async def test_ayarlar(world: World) -> None:
    initial = (await world.get("/settings/registration")).json()
    saved = await call(
        world, "PUT", "/settings/registration", {"invite_valid_days": 3, "kvkk_published": True}
    )
    bad = await call(world, "PUT", "/settings/registration", {"invite_valid_days": 31})
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")

    assert initial == {
        "invite_enabled": True, "qr_enabled": True, "code_enabled": True,
        "owner_can_approve_tenant": False, "owner_document_required": False,
        "invite_valid_days": 7, "kvkk_published": False,
    }  # fmt: skip
    assert (saved.json()["data"]["invite_valid_days"], saved.json()["data"]["kvkk_published"]) == (
        3,
        True,
    )
    assert bad.json()["error"]["fields"] == {"invite_valid_days": "1–30 gün."}
    assert (await call(world, "GET", "/settings/registration", headers=board)).status_code == 403


async def test_davet_olustur_kabul_et_giris_yap(world: World) -> None:
    created = await world.post("/invitations", _invite(world))
    token = await _token(created)
    listed = (await world.get("/invitations")).json()
    again = await world.post("/invitations", _invite(world))

    assert created.json()["data"]["status"] == "sent"
    assert listed["items"][0]["link"] is None  # belirteç yalnız özetle saklanır
    assert again.json()["error"]["code"] == "already_invited"

    info = await world.api.get(f"{PUBLIC}/{token}")
    wrong = await world.api.post(
        f"{PUBLIC}/{token}/accept", json={"phone": "05329998877", "kvkk_ack": True}
    )
    no_email = await world.api.post(
        f"{PUBLIC}/{token}/accept", json={"phone": "05321112233", "kvkk_ack": True}
    )
    accepted = await world.api.post(
        f"{PUBLIC}/{token}/accept",
        json={"phone": "05321112233", "kvkk_ack": True, "email": "Test@Ornek.com"},
    )
    second = await world.api.post(
        f"{PUBLIC}/{token}/accept", json={"phone": "05321112233", "kvkk_ack": True}
    )

    assert info.json() == {
        "site_name": "Aksu Konakları",
        "site_slug": "aksu-konaklari",
        "full_name": "Test Deneme",
        "unit_name": "A-1",
        "relation": "tenant",
        "phone_hint": "2233",
        "status": "opened",  # bağlantı açıldı
        "expires_at": info.json()["expires_at"],
        "email_required": True,
    }
    assert wrong.status_code == 422
    assert "phone" in wrong.json()["error"]["fields"]
    assert no_email.json()["error"]["fields"] == {"email": "Giriş için e-posta adresi gerekli."}
    assert accepted.status_code == 201, accepted.text
    data = accepted.json()["data"]
    assert (data["unit_name"], data["login_email"]) == ("A-1", "test@ornek.com")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_completed"
    # Kişi bölüme kiracı olarak eklendi, giriş hesabı açıldı (geçici parola)
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    assert "tenant" in {p["role"] for p in unit["parties"]}
    login = await world.api.post(
        "/api/v1/auth/login",
        json={"email": "test@ornek.com", "password": data["temporary_password"]},
    )
    assert login.status_code == 200
    status = (await world.get("/invitations", params={"status": "completed"})).json()
    assert status["total"] == 1


async def test_yeniden_gonder_iptal_ve_gecersiz_belirtec(world: World) -> None:
    created = await world.post("/invitations", _invite(world))
    old = await _token(created)
    invitation_id = created.json()["data"]["id"]

    resent = await world.post(f"/invitations/{invitation_id}/resend")
    new = await _token_from(resent)
    old_info = await world.api.get(f"{PUBLIC}/{old}")
    cancelled = await world.post(f"/invitations/{invitation_id}/cancel")
    new_info = await world.api.get(f"{PUBLIC}/{new}")
    other_site = await call(world, "POST", f"/invitations/{invitation_id}/cancel", slug=OTHER)

    assert old_info.status_code == 404
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert new_info.status_code == 404
    assert new_info.json()["error"]["message"].startswith("Davet bağlantısı geçersiz")
    assert other_site.status_code == 404


async def _token_from(response: httpx2.Response) -> str:
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["link"]).removeprefix("/davet/")


async def test_yontem_kapali_ve_kurulumdaki_site(world: World) -> None:
    await call(world, "PUT", "/settings/registration", {"invite_enabled": False})
    disabled = await world.post("/invitations", _invite(world))
    await call(world, "PUT", "/settings/registration", {"invite_enabled": True})
    await set_lifecycle(world, "setup")
    setup = await world.post("/invitations", _invite(world))
    fields = await world.post("/invitations", {"full_name": "X", "phone": "123"})

    assert disabled.status_code == 409
    assert disabled.json()["error"]["code"] == "method_disabled"
    assert setup.status_code == 409
    assert setup.json()["error"]["code"] == "site_not_live"
    assert set(fields.json()["error"]["fields"]) == {"full_name", "phone", "relation", "unit_id"}


async def test_herkese_acik_form_blok_bolum_listesi_ve_yontem(world: World) -> None:
    code = (await world.get("/registration-link")).json()["code"]

    form = (await world.api.get(f"/api/v1/public/registration/{code}")).json()
    applied = await world.api.post(
        f"/api/v1/public/registration/{code}",
        json={"first_name": "Ali", "last_name": "Can", "phone": "05327776655",
              "unit_text": "A-1", "relation": "tenant", "kvkk_ack": True,
              "unit_id": world.unit_id},
    )  # fmt: skip
    registrations = (await world.get("/registrations")).json()["items"]
    await call(world, "PUT", "/settings/registration", {"qr_enabled": False, "code_enabled": False})
    disabled = await world.api.get(f"/api/v1/public/registration/{code}")
    await call(world, "PUT", "/settings/registration", {"qr_enabled": True})
    await set_lifecycle(world, "setup")
    not_live = await world.api.get(f"/api/v1/public/registration/{code}")

    assert form["blocks"] == [
        {"id": world.lookups["block"], "name": "A", "units": [{"id": world.unit_id, "name": "A-1"}]}
    ]
    assert "Ayşe" not in str(form)  # KVKK: kişi adı yok
    assert applied.status_code == 201, applied.text
    assert (registrations[0]["requested_unit_id"], registrations[0]["requested_unit_name"]) == (
        world.unit_id,
        "A-1",
    )
    assert disabled.status_code == 404
    assert disabled.json()["error"]["code"] == "method_disabled"
    assert not_live.status_code == 409
    assert not_live.json()["error"]["code"] == "site_not_live"


async def test_malik_hissesi_doluysa_kabul_tamamlanmaz(world: World) -> None:
    created = await world.post("/invitations", _invite(world, relation="owner"))
    token = await _token(created)

    response = await world.api.post(
        f"{PUBLIC}/{token}/accept",
        json={"phone": "05321112233", "kvkk_ack": True, "email": "sahip@ornek.com"},
    )
    retry_info = await world.api.get(f"{PUBLIC}/{token}")

    assert response.status_code == 409
    assert response.json()["error"]["message"].startswith("Bölüm kaydınız tamamlanamadı")
    assert retry_info.json()["status"] == "opened"  # hiçbir şey yazılmadı
