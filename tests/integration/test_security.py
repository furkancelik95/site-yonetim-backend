"""Kargo, ziyaretçi, güvenlik için daire arama — docs/06 §2.12 (gerçek PostgreSQL).

Kurgu `finance_world`: A-1 maliki Ayşe. Site planı kargo ve ziyaretçi modüllerini içerir.
"""

import uuid
from datetime import date

import pytest

from site_yonetim.models import Plan, Site
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def _user(
    w: World, email: str, role: str, *, kind: str | None = None, **fields: object
) -> dict[str, str]:
    user = await create_user(w.factory, email, **({"kind": kind} if kind else {}))
    await add_site_membership(w.factory, w.site_id, user.id, role, **fields)
    return await login_headers(w.api, email)


@pytest.fixture
async def gate(world: World) -> tuple[World, dict[str, str], dict[str, str]]:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Pro", max_units=500, allowed_modules=["finance", "packages", "visitors"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    for key in ("packages", "visitors"):
        assert (await world.post(f"/modules/{key}/toggle")).status_code == 200
    guard = await _user(world, "guvenlik@test.local", "Güvenlik")
    resident = await _user(
        world, "ayse@test.local", "Sakin", kind="resident", person_id=uuid.UUID(world.person_id)
    )
    return world, guard, resident


async def test_kargo_kodla_teslim_edilir(
    gate: tuple[World, dict[str, str], dict[str, str]],
) -> None:
    w, guard, resident = gate
    received = await w.api.post(
        w.url("/packages"), json={"unit_id": w.unit_id, "carrier": "Yurtiçi"}, headers=guard
    )
    assert received.status_code == 201, received.text
    package = received.json()["data"]
    assert "pickup_code" not in package  # güvenlik kodu görmez
    assert (package["unit_name"], package["status"], package["received_by"]) == (
        "A-1", "waiting", "Test KULLANICI"
    )  # fmt: skip

    mine = (await w.api.get(w.url("/resident/packages"), headers=resident)).json()
    [item] = mine
    code = item["pickup_code"]
    assert len(code) == 4 and code.isdigit()  # noqa: PT018

    wrong = "1000" if code != "1000" else "1001"
    bad = await w.api.post(
        w.url(f"/packages/{package['id']}/deliver"),
        json={"pickup_code": wrong, "delivered_to": "Ayşe Yılmaz"},
        headers=guard,
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["message"] == "Teslim kodu hatalı. Kodu sakinden yeniden isteyin."
    ok = await w.api.post(
        w.url(f"/packages/{package['id']}/deliver"),
        json={"pickup_code": code, "delivered_to": "Ayşe Yılmaz"},
        headers=guard,
    )
    assert ok.status_code == 200, ok.text
    assert (ok.json()["data"]["status"], ok.json()["data"]["delivered_to"]) == (
        "delivered",
        "Ayşe Yılmaz",
    )
    again = await w.api.post(
        w.url(f"/packages/{package['id']}/deliver"),
        json={"pickup_code": code, "delivered_to": "Ayşe Yılmaz"},
        headers=guard,
    )
    assert again.status_code == 409
    assert (await w.api.get(w.url("/resident/packages"), headers=resident)).json() == []
    waiting = (
        await w.api.get(w.url("/packages"), params={"status": "waiting"}, headers=guard)
    ).json()
    assert waiting["total"] == 0
    assert (await w.api.get(w.url("/packages"), headers=guard)).json()["total"] == 1


async def test_package_validation(gate: tuple[World, dict[str, str], dict[str, str]]) -> None:
    w, guard, _ = gate
    missing = await w.api.post(
        w.url("/packages"), json={"unit_id": str(uuid.uuid4())}, headers=guard
    )
    assert missing.json()["error"]["fields"] == {"unit_id": "Seçilen bölüm bulunamadı."}
    bad_code = await w.api.post(
        w.url(f"/packages/{uuid.uuid4()}/deliver"),
        json={"pickup_code": "12a4", "delivered_to": "X Y"},
        headers=guard,
    )
    assert bad_code.status_code == 422
    assert (
        await w.api.post(w.url(f"/packages/{uuid.uuid4()}/deliver"),
                         json={"pickup_code": "1234", "delivered_to": "X Y"}, headers=guard)
    ).status_code == 404  # fmt: skip


async def test_ziyaretci_giris_cikis(gate: tuple[World, dict[str, str], dict[str, str]]) -> None:
    w, guard, _ = gate
    walk_in = await w.api.post(
        w.url("/visitors"),
        json={
            "unit_id": w.unit_id,
            "full_name": "  Mehmet   Kaya ",
            "plate_number": "34 abc 123",
            "kind": "guest",
        },
        headers=guard,
    )
    assert walk_in.status_code == 201, walk_in.text
    data = walk_in.json()["data"]
    assert (data["full_name"], data["plate_number"], data["status"]) == (
        "Mehmet Kaya",
        "34ABC123",
        "entered",
    )
    assert walk_in.json()["message"] == "Mehmet Kaya (A-1) içeri alındı."
    out = await w.api.post(w.url(f"/visitors/{data['id']}/exit"), headers=guard)
    assert out.json()["data"]["status"] == "exited"
    assert (
        await w.api.post(w.url(f"/visitors/{data['id']}/exit"), headers=guard)
    ).status_code == 409

    expected = await w.api.post(
        w.url("/visitors"),
        json={
            "unit_id": w.unit_id,
            "full_name": "Usta Ali",
            "kind": "contractor",
            "expected_on": "2026-06-22",
        },
        headers=guard,
    )
    later = expected.json()["data"]
    assert (later["status"], later["visit_date"]) == ("expected", "2026-06-22")
    assert (
        await w.api.post(w.url(f"/visitors/{later['id']}/exit"), headers=guard)
    ).status_code == 409
    entered = await w.api.post(w.url(f"/visitors/{later['id']}/enter"), headers=guard)
    assert entered.json()["data"]["status"] == "entered"
    assert (
        await w.api.post(w.url(f"/visitors/{later['id']}/enter"), headers=guard)
    ).status_code == 409

    today = (await w.api.get(w.url("/visitors"), headers=guard)).json()
    assert [v["full_name"] for v in today["items"]] == ["Mehmet Kaya"]  # bugün: 20.06.2026
    on_22 = (
        await w.api.get(
            w.url("/visitors"), params={"date": date(2026, 6, 22).isoformat()}, headers=guard
        )
    ).json()
    assert [v["full_name"] for v in on_22["items"]] == ["Usta Ali"]


async def test_lookup_shows_only_unit_and_occupants(
    gate: tuple[World, dict[str, str], dict[str, str]],
) -> None:
    w, guard, _ = gate
    found = (await w.api.get(w.url("/units/lookup"), params={"q": "A-1"}, headers=guard)).json()
    assert found == [{"unit_id": w.unit_id, "unit_name": "A-1", "occupants": ["Ayşe YILMAZ"]}]
    by_name = (await w.api.get(w.url("/units/lookup"), params={"q": "ayşe"}, headers=guard)).json()
    assert [u["unit_name"] for u in by_name] == ["A-1"]
    await w.post(
        f"/units/{w.unit_id}/parties",
        {
            "role": "tenant",
            "start_date": "2026-01-01",
            "person": {"first_name": "Elif", "last_name": "Demir"},
        },
    )
    occupied = (await w.api.get(w.url("/units/lookup"), params={"q": "A1"}, headers=guard)).json()
    assert occupied[0]["occupants"] == ["Elif DEMİR"]  # oturan kiracı; malik gösterilmez
    # borç, telefon, hesap gibi alan yok
    assert set(occupied[0]) == {"unit_id", "unit_name", "occupants"}
    assert (
        await w.api.get(w.url("/units/lookup"), params={"q": "yok"}, headers=guard)
    ).json() == []


async def test_permissions_modules_and_isolation(
    gate: tuple[World, dict[str, str], dict[str, str]],
) -> None:
    w, guard, resident = gate
    accountant = await _user(w, "muhasebe@test.local", "Muhasebe")
    for path in ("/packages", "/visitors"):
        assert (await w.api.get(w.url(path), headers=accountant)).status_code == 403
    assert (
        await w.api.get(w.url("/units/lookup"), params={"q": "A"}, headers=accountant)
    ).status_code == 403
    assert (
        await w.api.get(w.url("/units/lookup"), params={"q": "A"}, headers=resident)
    ).status_code == 403
    assert (await w.api.get(w.url("/finance"), headers=guard)).status_code == 404
    assert (await w.api.get(w.url("/debtors"), headers=guard)).status_code == 403  # borç görmez

    package_id = (
        await w.api.post(w.url("/packages"), json={"unit_id": w.unit_id}, headers=guard)
    ).json()["data"]["id"]
    other = await w.api.post(
        w.url(f"/packages/{package_id}/deliver", "yildiz-sitesi"),
        json={"pickup_code": "1234", "delivered_to": "X Y"},
        headers=w.headers,
    )
    assert other.status_code == 404  # B'de kargo modülü yok / kayıt yok

    assert (await w.post("/modules/packages/toggle")).json()["data"]["enabled"] is False
    assert (await w.api.get(w.url("/packages"), headers=guard)).status_code == 404
    assert (await w.api.get(w.url("/resident/packages"), headers=resident)).status_code == 404
