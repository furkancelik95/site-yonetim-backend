"""Güvenlik olay kaydı ve kayıp eşya — frontend servis istekleri 09, 10 (issue #37, #38).

Kurgu `finance_world`: site planı ziyaretçi modülünü içerir ve açıktır; görevli "Güvenlik".
"""

from typing import Any

import httpx2
import pytest

from site_yonetim.models import Plan, Site
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


@pytest.fixture
async def guard(world: World) -> dict[str, str]:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Pro", max_units=500, allowed_modules=["finance", "visitors"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/visitors/toggle")).status_code == 200
    user = await create_user(world.factory, "guvenlik@test.local", full_name="Recep Er")
    await add_site_membership(world.factory, world.site_id, user.id, "Güvenlik")
    return await login_headers(world.api, "guvenlik@test.local")


async def call(
    w: World, method: str, path: str, headers: dict[str, str], body: Any = None
) -> httpx2.Response:
    return await w.api.request(method, w.url(path), json=body, headers=headers)


INCIDENT = {
    "kind": "damage",
    "location": "A blok giriş",
    "description": "Giriş kapısının camı kırık bulundu",
    "occurred_at": "2026-06-20T08:30:00Z",
}


async def test_olay_kaydi_ve_kapanis(world: World, guard: dict[str, str]) -> None:
    created = await call(world, "POST", "/incidents", guard, {**INCIDENT, "unit_id": world.unit_id})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["message"] == "#1 numaralı olay kaydedildi."
    incident = body["data"]
    assert (incident["number"], incident["status"], incident["unit_name"]) == (1, "open", "A-1")
    assert (incident["recorded_by"], incident["occurred_at"]) == (
        "Recep Er",
        "2026-06-20T08:30:00Z",
    )
    second = (
        await call(world, "POST", "/incidents", guard, {**INCIDENT, "occurred_at": None})
    ).json()
    assert second["data"]["number"] == 2

    empty = await call(world, "POST", f"/incidents/{incident['id']}/close", guard, {"note": " "})
    assert empty.json()["error"]["fields"] == {"note": "Ne yapıldığını yazın."}
    closed = await call(
        world, "POST", f"/incidents/{incident['id']}/close", guard, {"note": "Cam değiştirildi"}
    )
    assert closed.json()["message"] == "#1 numaralı olay kapatıldı."
    assert (closed.json()["data"]["status"], closed.json()["data"]["closed_note"]) == (
        "closed", "Cam değiştirildi"
    )  # fmt: skip
    again = await call(world, "POST", f"/incidents/{incident['id']}/close", guard, {"note": "x"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_closed"

    open_only = (await call(world, "GET", "/incidents?status=open", guard)).json()
    assert [i["number"] for i in open_only["items"]] == [2]
    listing = (await call(world, "GET", "/incidents?page_size=1", guard)).json()
    assert (listing["total"], len(listing["items"])) == (2, 1)
    audit = (await world.get("/audit", params={"entity": "incidents"})).json()
    assert audit["total"] == 3  # iki kayıt + kapanış


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"kind": "uzaylı"}, "kind", "Olay türünü seçin."),
        ({"location": ""}, "location", "Olayın yerini yazın."),
        ({"description": "kırk"}, "description", "Ne olduğunu kısaca yazın."),
    ],
)
async def test_olay_dogrulama(
    world: World, guard: dict[str, str], overrides: dict[str, str], field: str, message: str
) -> None:
    response = await call(world, "POST", "/incidents", guard, {**INCIDENT, **overrides})
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {field: message}


async def test_kayip_esya_teslim_ve_elden_cikarma(world: World, guard: dict[str, str]) -> None:
    body = {"description": "Siyah sırt çantası", "location": "Havuz kenarı", "found_by": "Recep Er"}
    item = (await call(world, "POST", "/lost-items", guard, body)).json()
    assert item["message"] == "Kayıp eşya #1 kaydedildi."
    item_id = item["data"]["id"]
    missing = await call(world, "POST", f"/lost-items/{item_id}/return", guard, {})
    assert missing.json()["error"]["fields"] == {"returned_to": "Teslim alanın adını yazın."}
    returned = await call(
        world, "POST", f"/lost-items/{item_id}/return", guard, {"returned_to": "Ayşe Demir"}
    )
    assert returned.json()["message"] == "Kayıp eşya #1 Ayşe Demir kişisine teslim edildi."
    assert returned.json()["data"]["status"] == "returned"
    twice = await call(world, "POST", f"/lost-items/{item_id}/return", guard, {"disposed": True})
    assert twice.status_code == 409
    assert twice.json()["error"]["code"] == "not_waiting"

    other = (
        await call(world, "POST", "/lost-items", guard, {**body, "description": "Şemsiye"})
    ).json()
    disposed = await call(
        world, "POST", f"/lost-items/{other['data']['id']}/return", guard, {"disposed": True}
    )
    assert disposed.json()["message"] == "Kayıp eşya #2 elden çıkarıldı olarak işaretlendi."
    waiting = (await call(world, "GET", "/lost-items?status=waiting", guard)).json()
    assert waiting["total"] == 0
    bad = await call(world, "POST", "/lost-items", guard, {"description": "", "location": "x"})
    assert bad.json()["error"]["fields"] == {"description": "Eşyayı tarif edin."}


async def test_yetki_modul_ve_site_izolasyonu(world: World, guard: dict[str, str]) -> None:
    incident = (await call(world, "POST", "/incidents", guard, INCIDENT)).json()["data"]
    user = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    auditor = await login_headers(world.api, "denetci@test.local")
    assert (await call(world, "GET", "/incidents", auditor)).status_code == 403  # kişisel veri
    assert (await call(world, "GET", "/incidents", world.headers)).status_code == 200  # Yönetici

    other = await world.api.post(
        world.url(f"/incidents/{incident['id']}/close", "yildiz-sitesi"),
        json={"note": "x"},
        headers=world.headers,
    )
    assert other.status_code == 404  # Yıldız'da modül kapalı ve kayıt yok
    assert (await world.post("/modules/visitors/toggle")).status_code == 200  # kapat
    assert (await call(world, "GET", "/incidents", guard)).status_code == 404
