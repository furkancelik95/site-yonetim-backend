"""Araçlar — servis isteği 22 (YAP-05): plaka sınırı, misafir, başka bölümde etkin plaka,
sakin "Araçlarım", onay, ayarlar, güvenlik plaka araması."""

from typing import Any

from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, enable_modules, new_unit, resident, staff


def _car(world: World, plate: str, **changes: Any) -> dict[str, Any]:
    return {"unit_id": world.unit_id, "plate": plate, "kind": "permanent", **changes}


async def test_arac_ekle_plaka_normalize_ve_sinir(world: World) -> None:
    first = await world.post("/vehicles", _car(world, "34 abc 123", brand_model="Renault Clio"))
    second = await world.post("/vehicles", _car(world, "06XY4567"))
    third = await world.post("/vehicles", _car(world, "35 ZZ 99"))
    guest = await world.post(
        "/vehicles", _car(world, "35 ZZ 99", kind="guest", valid_until="2026-07-01")
    )
    guest_no_date = await world.post("/vehicles", _car(world, "16 AA 11", kind="guest"))
    bad = await world.post("/vehicles", _car(world, "ABC123"))
    again = await world.post("/vehicles", _car(world, "34ABC123"))

    assert first.status_code == 201, first.text
    car = first.json()["data"]
    assert (car["plate"], car["plate_display"], car["status"]) == (
        "34ABC123",
        "34 ABC 123",
        "active",
    )
    assert car["owner_name"] == "Ayşe YILMAZ"
    assert second.status_code == 201
    assert third.status_code == 422
    assert third.json()["error"]["code"] == "plate_limit"
    assert guest.status_code == 201  # misafir sınıra sayılmaz
    assert guest_no_date.json()["error"]["fields"] == {
        "valid_until": "Misafir aracı için geçerlilik tarihi zorunlu."
    }
    assert bad.json()["error"]["fields"] == {"plate": "Plakayı 34 ABC 123 biçiminde yazın."}
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_registered"


async def test_baska_bolumde_etkin_plaka_ve_force(world: World) -> None:
    other = await new_unit(world, "2")
    await world.post("/vehicles", _car(world, "34ABC123"))

    blocked = await world.post("/vehicles", {**_car(world, "34ABC123"), "unit_id": other})
    forced = await world.post(
        "/vehicles", {**_car(world, "34ABC123"), "unit_id": other, "force": True}
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "plate_active_elsewhere"
    assert blocked.json()["error"]["message"].startswith(
        "34 ABC 123 plakası A-1 bölümünde kayıtlı."
    )
    assert forced.status_code == 201


async def test_liste_suzgec_ve_pasif(world: World) -> None:
    car = (await world.post("/vehicles", _car(world, "34ABC123"))).json()["data"]
    await world.post("/vehicles", _car(world, "06XY4567"))

    by_plate = (await world.get("/vehicles", params={"q": "34 abc"})).json()
    patched = await call(world, "PATCH", f"/vehicles/{car['id']}", {"status": "inactive"})
    inactive = (await world.get("/vehicles", params={"status": "inactive"})).json()
    overview = (await world.get(f"/units/{world.unit_id}/overview")).json()

    assert [v["plate"] for v in by_plate["items"]] == ["34ABC123"]
    assert patched.json()["message"] == "34 ABC 123 pasif yapıldı; kapı ekranında görünmez."
    assert inactive["total"] == 1
    assert overview["vehicles"] == 1


async def test_sakin_araclarim_onay_gerektirir(world: World) -> None:
    other = await new_unit(world, "2")
    await world.post("/vehicles", {**_car(world, "34ABC123"), "unit_id": other})
    settings = await call(
        world, "PUT", "/settings/vehicles", {"plate_limit_per_unit": 3, "approval_required": True}
    )
    headers = await resident(world, "ayse@test.local", world.person_id)

    added = await call(world, "POST", "/resident/vehicles", {"plate": "06XY4567"}, headers=headers)
    elsewhere = await call(
        world, "POST", "/resident/vehicles", {"plate": "34ABC123", "unit_id": world.unit_id},
        headers=headers,
    )  # fmt: skip
    foreign_unit = await call(
        world, "POST", "/resident/vehicles", {"plate": "16AA11", "unit_id": other},
        headers=headers,
    )  # fmt: skip
    mine = (await call(world, "GET", "/resident/vehicles", headers=headers)).json()

    assert settings.json()["data"] == {"plate_limit_per_unit": 3, "approval_required": True}
    assert added.status_code == 201, added.text
    assert added.json()["data"]["status"] == "pending"
    assert elsewhere.status_code == 409
    assert elsewhere.json()["error"]["message"].endswith("Site yönetimine başvurun.")
    assert foreign_unit.status_code == 404
    assert (mine["limit"], mine["approval_required"]) == (3, True)
    assert [v["plate"] for v in mine["items"]] == ["06XY4567"]

    approved = await call(
        world, "PATCH", f"/vehicles/{added.json()['data']['id']}", {"status": "active"}
    )
    removed = await call(
        world, "POST", f"/resident/vehicles/{added.json()['data']['id']}/remove", {},
        headers=headers,
    )  # fmt: skip
    assert approved.json()["data"]["status"] == "active"
    assert removed.json()["data"]["status"] == "inactive"
    assert (await call(world, "GET", "/vehicles", headers=headers)).status_code == 403


async def test_ayar_araligi_ve_yetki_ve_izolasyon(world: World) -> None:
    car = (await world.post("/vehicles", _car(world, "34ABC123"))).json()["data"]
    out_of_range = await call(world, "PUT", "/settings/vehicles", {"plate_limit_per_unit": 11})
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    board_write = await call(world, "POST", "/vehicles", _car(world, "06XY4567"), headers=board)
    board_read = await call(world, "GET", "/vehicles", headers=board)
    other_list = (await call(world, "GET", "/vehicles", slug=OTHER)).json()
    other_patch = await call(
        world, "PATCH", f"/vehicles/{car['id']}", {"color": "Kırmızı"}, slug=OTHER
    )

    assert out_of_range.json()["error"]["fields"] == {"plate_limit_per_unit": "1–10 arası."}
    assert board_write.status_code == 403
    assert board_read.status_code == 200
    assert other_list["total"] == 0
    assert other_patch.status_code == 404


async def test_guvenlik_plaka_ile_bolum_bulur(world: World) -> None:
    await enable_modules(world, "visitors")
    await world.post("/vehicles", _car(world, "34ABC123"))
    guard = await staff(world, "guvenlik@test.local", "Güvenlik")

    found = await call(world, "GET", "/units/lookup", headers=guard, params={"q": "34 ABC 123"})

    assert found.status_code == 200, found.text
    assert found.json() == [
        {"unit_id": world.unit_id, "unit_name": "A-1", "occupants": ["Ayşe YILMAZ"]}
    ]
