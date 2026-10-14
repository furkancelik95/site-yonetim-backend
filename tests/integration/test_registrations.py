"""Sakinin kendini kaydetmesi ve yönetici onayı — frontend servis isteği 13, issue #41.

Kurgu `finance_world`: A-1 maliki Ayşe Yılmaz (%100, telefonsuz). Herkese açık uçlar oturumsuz.
"""

from typing import Any

import httpx2
from sqlalchemy import func, select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Person
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

FORM = {
    "first_name": "test",
    "last_name": "deneme",
    "phone": "0532 111 22 33",
    "email": "Test.Deneme@Ornek.local",
    "unit_text": "A blok 1",
    "relation": "tenant",
    "explicit_consent": False,
    "kvkk_ack": True,
}


async def code(w: World) -> str:
    response = await w.get("/registration-link")
    assert response.status_code == 200, response.text
    value: str = response.json()["code"]
    return value


async def apply(w: World, link: str, **overrides: Any) -> httpx2.Response:
    return await w.api.post(f"/api/v1/public/registration/{link}", json={**FORM, **overrides})


async def pending(w: World) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = (
        await w.get("/registrations", params={"status": "pending"})
    ).json()["items"]
    return items


async def approve(w: World, reg_id: str, **body: Any) -> httpx2.Response:
    payload = {"unit_id": w.unit_id, "start_date": "2026-06-20", **body}
    return await w.post(f"/registrations/{reg_id}/approve", payload)


async def persons(w: World) -> int:
    with site_scope(w.site_id):
        async with w.factory() as session:
            return int(await session.scalar(select(func.count()).select_from(Person)) or 0)


async def test_herkese_acik_form_ve_basvuru(world: World) -> None:
    link = await code(world)
    assert len(link) >= 10
    site = await world.api.get(f"/api/v1/public/registration/{link}")
    assert site.json() == {"site_name": "Aksu Konakları", "site_slug": "aksu-konaklari"}
    bad = await world.api.get("/api/v1/public/registration/yanlis-kod-123")
    assert bad.status_code == 404
    assert bad.json()["error"]["message"].startswith("Kayıt bağlantısı geçersiz ya da kapatılmış.")

    response = await apply(world, link)
    assert response.status_code == 201, response.text
    assert response.json() == {
        "data": {"reference": "KB-0001"},
        "message": "Başvurunuz alındı (KB-0001). Site yönetimi onaylayınca giriş bilgileriniz "
        "size iletilecek.",
    }
    [registration] = await pending(world)
    assert (registration["first_name"], registration["last_name"]) == ("Test", "DENEME")
    assert (registration["phone"], registration["email"]) == (
        "+905321112233",
        "test.deneme@ornek.local",
    )
    assert (registration["status"], registration["explicit_consent"]) == ("pending", False)

    again = await apply(world, link, phone="+90 532 111 22 33")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_pending"


async def test_alan_dogrulama(world: World) -> None:
    link = await code(world)
    response = await apply(
        world,
        link,
        first_name="A1",
        last_name="",
        phone="123",
        email="x@",
        relation="",
        kvkk_ack=False,
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["message"] == "Formda düzeltilmesi gereken alanlar var."
    assert set(error["fields"]) == {
        "first_name",
        "last_name",
        "phone",
        "email",
        "relation",
        "kvkk_ack",
    }
    assert await pending(world) == []


async def test_onay_bolume_kiraci_ekler_ve_giris_hesabi_acar(world: World) -> None:
    await apply(world, await code(world))
    [registration] = await pending(world)
    before = await persons(world)

    approved = await approve(world, registration["id"])
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["message"].startswith("Test DENEME A-1 kiracı olarak eklendi. Giriş hesabı açıldı;")
    data = body["data"]
    assert (data["status"], data["unit_name"], data["decided_by"]) == (
        "approved",
        "A-1",
        "Kerem YILDIRIM",
    )
    assert approved.headers["cache-control"] == "no-store"
    assert await persons(world) == before + 1

    parties = (await world.get(f"/units/{world.unit_id}")).json()
    assert "Test DENEME" in str(parties)
    login = await world.api.post(
        "/api/v1/auth/login",
        json={"email": "test.deneme@ornek.local", "password": data["temporary_password"]},
    )
    assert login.json()["must_change_password"] is True

    twice = await approve(world, registration["id"])
    assert twice.status_code == 409
    assert twice.json()["error"]["code"] == "already_decided"
    audit = (await world.get("/audit", params={"entity": "registrations"})).json()["items"]
    assert audit[0]["after"] == {"reference": "KB-0001", "status": "approved"}


async def test_kayitli_kisi_yeniden_acilmaz_eposta_yoksa_hesap_yok(world: World) -> None:
    created = await world.post(
        "/people", {"first_name": "Test", "last_name": "Deneme", "phone": "05321112233"}
    )
    assert created.status_code == 201, created.text
    await apply(world, await code(world), email="")
    [registration] = await pending(world)
    before = await persons(world)
    approved = await approve(world, registration["id"])
    assert approved.status_code == 200, approved.text
    assert await persons(world) == before  # telefonla eşleşen kişi kullanıldı
    assert approved.json()["data"]["temporary_password"] is None
    assert approved.json()["message"].endswith("E-posta verilmediği için giriş hesabı açılmadı.")


async def test_malik_hissesi_doluysa_409(world: World) -> None:
    await apply(world, await code(world), relation="owner")
    [registration] = await pending(world)
    response = await approve(world, registration["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "owner_shares_exceed"
    assert len(await pending(world)) == 1  # başvuru bekliyor; yönetici önce daireyi düzeltir


async def test_ret_gerekce_ister(world: World) -> None:
    await apply(world, await code(world))
    [registration] = await pending(world)
    empty = await world.post(f"/registrations/{registration['id']}/reject", {"reason": " "})
    assert empty.status_code == 422
    rejected = await world.post(
        f"/registrations/{registration['id']}/reject", {"reason": "Bu bölümde kayıtlı değil"}
    )
    assert rejected.json()["message"] == "KB-0001 numaralı başvuru reddedildi."
    listed = (await world.get("/registrations", params={"status": "rejected"})).json()["items"]
    assert listed[0]["reject_reason"] == "Bu bölümde kayıtlı değil"
    # reddedilen başvurudan sonra aynı telefonla yeniden başvurulabilir
    assert (await apply(world, await code(world))).status_code == 201


async def test_baglanti_yenileme_ve_kayda_kapatma(world: World) -> None:
    old = await code(world)
    rotated = await world.post("/registration-link/rotate")
    new = rotated.json()["data"]["code"]
    assert new != old
    assert (await world.api.get(f"/api/v1/public/registration/{old}")).status_code == 404
    closed = await world.api.patch(
        world.url("/registration-link"), json={"is_enabled": False}, headers=world.headers
    )
    assert closed.json()["message"] == "Kayıt bağlantısı başvurulara kapatıldı."
    assert (await apply(world, new)).status_code == 404


async def test_istek_siniri(world: World) -> None:
    link = await code(world)
    statuses = [(await apply(world, link, phone=f"053211122{i:02d}")).status_code for i in range(6)]
    assert statuses == [201] * 5 + [429]
    limited = await apply(world, link, phone="05321112299")
    assert (
        limited.json()["error"]["message"]
        == "Çok fazla deneme yaptınız, biraz sonra tekrar deneyin."
    )


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    await apply(world, await code(world))
    [registration] = await pending(world)
    user = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    auditor = await login_headers(world.api, "denetci@test.local")
    assert (await world.api.get(world.url("/registrations"), headers=auditor)).status_code == 403
    other = await world.api.post(
        world.url(f"/registrations/{registration['id']}/approve", "yildiz-sitesi"),
        json={"unit_id": world.unit_id, "start_date": "2026-06-20"},
        headers=world.headers,
    )
    assert other.status_code == 404
    yildiz = (
        await world.api.get(world.url("/registrations", "yildiz-sitesi"), headers=world.headers)
    ).json()
    assert yildiz["total"] == 0
