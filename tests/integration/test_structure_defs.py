"""Yapı tanımları — servis isteği 19 (YAP-02): ortak alanlar, bölüm tipi, yürürlük tarihli
öznitelikler ve geçmişi, toplu düzenleme, yapı özeti."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select

from site_yonetim.models import Period, Unit
from site_yonetim.services import structure_jobs
from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, new_unit, scope, staff

# world.today = 2026-06-20 → en erken yürürlük 2026-06-01


async def test_ortak_alan_ekle_listele_duzenle(world: World) -> None:
    created = await world.post(
        "/common-areas",
        {"name": "Kapalı havuz", "kind": "pool", "block_id": world.lookups["block"],
         "is_reservable": True},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    area = created.json()["data"]
    assert (area["block_name"], area["is_reservable"], area["is_active"]) == ("A", True, True)

    duplicate = await world.post("/common-areas", {"name": "KAPALI HAVUZ", "kind": "pool"})
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["fields"] == {"name": "Bu adla bir ortak alan var."}

    patched = await call(world, "PATCH", f"/common-areas/{area['id']}", {"is_active": False})
    assert patched.json()["data"]["is_active"] is False
    listed = (await world.get("/common-areas")).json()
    assert [a["name"] for a in listed] == ["Kapalı havuz"]


async def test_ortak_alan_site_izolasyonu_ve_yetki(world: World) -> None:
    area = (await world.post("/common-areas", {"name": "Bahçe", "kind": "garden"})).json()["data"]
    other_list = await call(world, "GET", "/common-areas", slug=OTHER)
    other_patch = await call(
        world, "PATCH", f"/common-areas/{area['id']}", {"name": "X"}, slug=OTHER
    )
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    forbidden = await call(
        world, "POST", "/common-areas", {"name": "Salon", "kind": "hall"}, headers=board
    )

    assert other_list.json() == []
    assert other_patch.status_code == 404
    assert forbidden.status_code == 403
    assert (await call(world, "GET", "/common-areas", headers=board)).status_code == 200


async def test_bolum_tipi_ekle_duzenle(world: World) -> None:
    created = await world.post("/unit-types", {"name": "4+1", "weight": "1.25"})
    assert created.status_code == 201, created.text
    type_id = created.json()["data"]["id"]
    assert created.json()["data"]["weight"] == "1.2500"

    dup = await world.post("/unit-types", {"name": "2+1"})  # varsayılan tiplerden
    patched = await call(world, "PATCH", f"/unit-types/{type_id}", {"weight": "1.5"})

    assert dup.status_code == 422
    assert dup.json()["error"]["fields"]["name"] == "Bu adla bir bölüm tipi var."
    assert patched.json()["data"]["weight"] == "1.5000"


async def test_ozniteliklerde_kapanmis_doneme_yururluk_reddedilir(world: World) -> None:
    response = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-05-31", "gross_area": "120.00"},
    )  # fmt: skip

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "period_locked"
    assert body["fields"] == {
        "effective_from": "Yürürlük tarihi geçmiş bir döneme düşemez; en erken 01.06.2026."
    }


async def test_kapali_donem_varsa_en_erken_sonraki_ay(world: World) -> None:
    with scope(world):
        async with world.factory() as session, session.begin():
            session.add(Period(year=2026, month=6, status="locked"))

    response = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-06-20", "gross_area": "120.00"},
    )  # fmt: skip

    assert response.json()["error"]["fields"]["effective_from"].endswith("en erken 01.07.2026.")


async def test_bugun_yururluk_hemen_uygulanir_ileri_tarih_planlanir(world: World) -> None:
    now = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-06-01", "gross_area": "140.00",
         "land_share_numerator": 12, "land_share_denominator": 1000},
    )  # fmt: skip
    later = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-08-01", "gross_area": "142.50"},
    )  # fmt: skip
    same = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-06-20", "gross_area": "140.00"},
    )  # fmt: skip
    pair = await call(
        world, "PATCH", f"/units/{world.unit_id}/attributes",
        {"effective_from": "2026-06-20", "land_share_numerator": 5},
    )  # fmt: skip

    assert now.status_code == 200, now.text
    assert now.json()["data"]["gross_area"] == "140.00"
    assert later.json()["data"]["gross_area"] == "140.00"  # henüz yürürlükte değil
    assert later.json()["message"] == "Değişiklik 01.08.2026 tarihinde yürürlüğe girecek."
    assert same.json()["error"]["code"] == "no_change"
    assert pair.status_code == 422
    history = (await world.get(f"/units/{world.unit_id}/attribute-history")).json()
    assert [(h["effective_from"], h["applied"]) for h in history] == [
        ("2026-08-01", False),
        ("2026-06-01", True),
    ]
    assert history[0]["changes"] == {"gross_area": {"from": "140.00", "to": "142.50"}}
    assert history[0]["changed_by"] == "Kerem YILDIRIM"

    # Tarih gelince gece işi uygular
    results = await structure_jobs.run_all(
        world.factory, today=date(2026, 8, 1), now=datetime(2026, 8, 1, 3, tzinfo=UTC)
    )
    assert [r.applied for r in results] == [1]
    with scope(world):
        async with world.factory() as session:
            unit = await session.scalar(select(Unit).where(Unit.id == uuid.UUID(world.unit_id)))
            assert unit is not None
            assert str(unit.gross_area) == "142.50"


async def test_bolum_patch_m2_degisikligi_gecmise_yazilir(world: World) -> None:
    """D-18: m²/arsa payı/tip `PATCH units/{id}` ile de üzerine yazılmaz; geçmiş tutulur."""
    response = await call(world, "PATCH", f"/units/{world.unit_id}", {"gross_area": "99.00"})

    assert response.status_code == 200, response.text
    history = (await world.get(f"/units/{world.unit_id}/attribute-history")).json()
    assert history[0]["changes"] == {"gross_area": {"from": None, "to": "99.00"}}
    assert history[0]["effective_from"] == "2026-06-20"


async def test_toplu_duzenleme_kismi_hata(world: World) -> None:
    second = await new_unit(world, "2")
    other_unit = (
        await call(world, "POST", "/units", {"block_id": await _other_block(world), "number": "9"},
                   slug=OTHER)
    ).json()["data"]["id"]  # fmt: skip
    type_id = (await world.get("/unit-types")).json()["items"][0]["id"]

    response = await world.post(
        "/units/bulk-update",
        {"unit_ids": [world.unit_id, second, other_unit],
         "changes": {"usage": "commercial", "unit_type_id": type_id}},
    )  # fmt: skip
    empty = await world.post(
        "/units/bulk-update", {"unit_ids": [], "changes": {"usage": "storage"}}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["updated"] == 2
    assert data["failed"] == [{"unit_id": other_unit, "message": "Bölüm bulunamadı."}]
    unit = (await world.get(f"/units/{second}")).json()
    assert (unit["usage"], unit["unit_type"]["id"]) == ("commercial", type_id)
    assert (await world.get(f"/units/{second}/attribute-history")).json()[0]["changes"][
        "unit_type_id"
    ]["to"] == unit["unit_type"]["name"]
    assert empty.status_code == 422
    assert empty.json()["error"]["fields"] == {"unit_ids": "En az bir bölüm seçin."}


async def test_yapi_ozeti_malik_ve_hisse_uyarilari(world: World) -> None:
    no_owner = await new_unit(world, "2", owner=None)
    half = await new_unit(world, "3", owner=None)
    await world.post(
        f"/units/{half}/parties",
        {"role": "owner", "start_date": "2020-01-01", "share_percent": "50.00",
         "person": {"first_name": "Ali", "last_name": "Can"}},
    )  # fmt: skip
    await call(
        world, "PATCH", f"/units/{world.unit_id}",
        {"land_share_numerator": 400, "land_share_denominator": 1000},
    )  # fmt: skip
    await world.post("/common-areas", {"name": "Otopark", "kind": "parking"})

    summary = (await world.get("/structure/summary")).json()

    assert summary["unit_count"] == 3
    assert summary["block_count"] == 1
    assert summary["units_without_owner"] == [{"id": no_owner, "display_name": "A-2"}]
    assert summary["units_shares_incomplete"] == [
        {"id": half, "display_name": "A-3", "total": "50.00"}
    ]
    assert (summary["land_share_sum"], summary["land_share_denominator"]) == (400, 1000)
    assert summary["land_share_mismatch"] is True
    assert summary["units_without_land_share"] == 2
    assert summary["common_area_count"] == 1


# --- yardımcılar --------------------------------------------------------------------------


async def _other_block(world: World) -> str:
    response = await call(world, "POST", "/blocks", {"name": "Y"}, slug=OTHER)
    return str(response.json()["data"]["id"])
