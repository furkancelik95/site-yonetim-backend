"""Talep departmanları — frontend servis isteği 11, issue #39 (gerçek PostgreSQL).

Kurgu `finance_world`: talep modülü planla açılır; site açılışında varsayılan departmanlar var.
"""

from typing import Any

import httpx2
import pytest

from site_yonetim.models import Plan, Site
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


@pytest.fixture
async def requests_on(world: World) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Standart", max_units=150, allowed_modules=["finance", "requests"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/requests/toggle")).status_code == 200
    return world


async def departments(w: World) -> dict[str, dict[str, Any]]:
    return {d["name"]: d for d in (await w.get("/departments")).json()}


async def patch(w: World, path: str, body: dict[str, Any]) -> httpx2.Response:
    return await w.api.patch(w.url(path), json=body, headers=w.headers)


async def test_varsayilan_departmanlar_ekle_adlandir_pasiflestir(requests_on: World) -> None:
    w = requests_on
    listed = await departments(w)
    assert sorted(listed) == ["Bahçe", "Güvenlik", "Teknik", "Temizlik", "Yönetim"]
    assert all(d["is_active"] and d["request_count"] == 0 for d in listed.values())

    created = await w.post("/departments", {"name": " Peyzaj "})
    assert created.status_code == 201
    assert created.json()["message"] == '"Peyzaj" departmanı eklendi.'
    duplicate = await w.post("/departments", {"name": "TEKNİK"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["message"] == '"Teknik" adında bir departman var.'
    short = await w.post("/departments", {"name": "X"})
    assert short.json()["error"]["fields"] == {"name": "Departman adı en az 2 karakter."}

    peyzaj = created.json()["data"]["id"]
    renamed = await patch(w, f"/departments/{peyzaj}", {"name": "Peyzaj ve Bahçe"})
    assert renamed.json()["data"]["name"] == "Peyzaj ve Bahçe"
    clash = await patch(w, f"/departments/{peyzaj}", {"name": "bahce"})
    assert clash.status_code == 409
    passive = await patch(w, f"/departments/{peyzaj}", {"is_active": False})
    assert passive.json()["message"] == '"Peyzaj ve Bahçe" pasifleştirildi; yeni talep atanamaz.'


async def test_talebe_atama_suzgec_ve_gecmis(requests_on: World) -> None:
    w = requests_on
    teams = await departments(w)
    first = (await w.post("/requests", {"title": "Bahçe sulama arızalı"})).json()["data"]
    second = (await w.post("/requests", {"title": "Asansör sesi"})).json()["data"]
    assert first["department_id"] is None

    routed = await w.post(
        f"/requests/{first['id']}/department", {"department_id": teams["Bahçe"]["id"]}
    )
    assert routed.status_code == 200, routed.text
    assert routed.json()["message"] == 'Talep "Bahçe" departmanına yönlendirildi.'
    detail = routed.json()["data"]
    assert (detail["department_id"], detail["department_name"]) == (teams["Bahçe"]["id"], "Bahçe")
    assert detail["events"][-1]["kind"] == "department_changed"
    assert detail["events"][-1]["description"] == "Departman: Bahçe"
    await w.post(f"/requests/{second['id']}/department", {"department_id": teams["Teknik"]["id"]})

    filtered = (
        await w.get("/requests", params={"department_id": teams["Bahçe"]["id"], "page_size": 1})
    ).json()
    assert (filtered["total"], filtered["items"][0]["title"]) == (1, "Bahçe sulama arızalı")
    assert (await departments(w))["Bahçe"]["request_count"] == 1

    cleared = await w.post(f"/requests/{first['id']}/department", {"department_id": None})
    assert cleared.json()["message"] == "Talebin departmanı kaldırıldı."
    assert cleared.json()["data"]["department_name"] is None

    await patch(w, f"/departments/{teams['Temizlik']['id']}", {"is_active": False})
    passive = await w.post(
        f"/requests/{first['id']}/department", {"department_id": teams["Temizlik"]["id"]}
    )
    assert passive.status_code == 422
    assert passive.json()["error"]["fields"] == {"department_id": "Geçerli bir departman seçin."}


async def test_yetki_ve_site_izolasyonu(requests_on: World) -> None:
    w = requests_on
    request = (await w.post("/requests", {"title": "Su kaçağı"})).json()["data"]
    user = await create_user(w.factory, "denetci@test.local")
    await add_site_membership(w.factory, w.site_id, user.id, "Denetçi")
    auditor = await login_headers(w.api, "denetci@test.local")
    assert (await w.api.get(w.url("/departments"), headers=auditor)).status_code == 403
    tech = await create_user(w.factory, "teknik@test.local")
    await add_site_membership(w.factory, w.site_id, tech.id, "Teknik Personel")
    technician = await login_headers(w.api, "teknik@test.local")
    assert (
        await w.api.post(w.url("/departments"), json={"name": "Havuz"}, headers=technician)
    ).status_code == 201

    # Yıldız'ın departmanı Aksu'nun talebine atanamaz
    yildiz = await w.api.get(w.url("/departments", "yildiz-sitesi"), headers=w.headers)
    assert yildiz.status_code == 404  # Yıldız'da talep modülü kapalı
    other = await w.api.post(
        w.url(f"/requests/{request['id']}/department", "yildiz-sitesi"),
        json={"department_id": None},
        headers=w.headers,
    )
    assert other.status_code == 404
