"""Özel alanlar — servis isteği 24 (YAP-10): sınır, özel nitelikli veri uyarısı, rol
görünürlüğü, değer doğrulama, denetim."""

from typing import Any

from sqlalchemy import select

from site_yonetim.models import AuditLog
from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, scope, staff


def _field(**changes: Any) -> dict[str, Any]:
    return {
        "entity": "unit",
        "label": "Evcil hayvan",
        "type": "select",
        "options": ["Yok", "Kedi", "Köpek"],
        "kvkk_category": "other",
        "view_roles": ["Yönetici", "Güvenlik"],
        "edit_roles": ["Yönetici"],
        "resident_can_view": True,
        **changes,
    }


async def test_alan_ekle_anahtar_sabit_tip_degismez(world: World) -> None:
    created = await world.post("/custom-fields", _field())
    field = created.json()["data"]
    renamed = await call(
        world, "PATCH", f"/custom-fields/{field['id']}", {"label": "Evcil dostlar"}
    )
    retyped = await call(world, "PATCH", f"/custom-fields/{field['id']}", {"type": "text"})
    few_options = await world.post("/custom-fields", _field(label="Renk", options=["Mavi"]))

    assert created.status_code == 201, created.text
    assert (field["key"], field["is_archived"]) == ("evcil_hayvan", False)
    assert renamed.json()["data"]["key"] == "evcil_hayvan"
    assert retyped.json()["error"]["fields"] == {
        "type": "Tip sonradan değiştirilemez; yeni alan açın."
    }
    assert "options" in few_options.json()["error"]["fields"]


async def test_otuz_alan_siniri_arsivlenen_sayilmaz(world: World) -> None:
    ids = []
    for n in range(30):
        response = await world.post(
            "/custom-fields", _field(label=f"Alan {n}", type="text", options=[])
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["data"]["id"])

    thirty_first = await world.post("/custom-fields", _field(label="Fazla", type="text"))
    await world.post(f"/custom-fields/{ids[0]}/archive")
    after_archive = await world.post("/custom-fields", _field(label="Fazla", type="text"))
    archived = (
        await world.get("/custom-fields", params={"entity": "unit", "archived": "true"})
    ).json()
    person_field = await world.post(
        "/custom-fields",
        _field(entity="person", label="İkinci telefon", type="text", kvkk_category="contact"),
    )

    assert thirty_first.status_code == 409
    assert thirty_first.json()["error"]["message"] == "Özel alan sınırına ulaşıldı (30)."
    assert after_archive.status_code == 201
    assert [f["id"] for f in archived] == [ids[0]]
    assert person_field.status_code == 201  # sınır nesne türü başına


async def test_ozel_nitelikli_etiket_uyarisi_ve_gerekceyle_gecis(world: World) -> None:
    blocked = await world.post("/custom-fields", _field(label="Sağlık durumu", type="text"))
    passed = await world.post(
        "/custom-fields",
        _field(label="Sağlık durumu", type="text", acknowledge_sensitive=True,
               sensitive_reason="Acil durumda tahliye önceliği için YK kararı"),
    )  # fmt: skip
    harmless = await world.post("/custom-fields", _field(label="Dinlenme alanı", type="bool"))

    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "sensitive_label"
    assert passed.status_code == 201
    assert harmless.status_code == 201
    with scope(world):
        async with world.factory() as session:
            reasons = list(
                await session.scalars(
                    select(AuditLog.reason).where(
                        AuditLog.entity == "custom_fields", AuditLog.reason.is_not(None)
                    )
                )
            )
    assert reasons == ["Acil durumda tahliye önceliği için YK kararı"]


async def test_degerler_dogrulama_ve_rol_gorunurlugu(world: World) -> None:
    pet = (await world.post("/custom-fields", _field())).json()["data"]
    secret = (
        await world.post(
            "/custom-fields",
            _field(label="Depo no", type="number", view_roles=["Yönetici"], options=[]),
        )
    ).json()["data"]
    path = f"/units/{world.unit_id}/custom-values"

    bad = await call(world, "PUT", path, {"values": {"evcil_hayvan": "Kuş", "depo_no": "x"}})
    saved = await call(world, "PUT", path, {"values": {"evcil_hayvan": "Kedi", "depo_no": "12"}})
    guard = await staff(world, "guvenlik@test.local", "Güvenlik")
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    guard_read = await call(world, "GET", path, headers=guard)
    board_read = await call(world, "GET", path, headers=board)
    board_fields = await call(
        world, "GET", "/custom-fields", headers=board, params={"entity": "unit"}
    )
    cleared = await call(world, "PUT", path, {"values": {"depo_no": ""}})

    assert bad.status_code == 422
    assert bad.json()["error"]["fields"] == {
        "evcil_hayvan": "Evcil hayvan için listeden seçin.",
        "depo_no": "Depo no sayı olmalı.",
    }
    assert saved.json()["data"]["values"] == {"evcil_hayvan": "Kedi", "depo_no": "12"}
    assert guard_read.status_code == 403  # güvenlik units.read iznine sahip değil
    assert board_read.json() == {"values": {}}  # YK hiçbir alanın view_roles'ünde değil
    assert board_fields.json() == []
    assert cleared.json()["data"]["values"] == {"evcil_hayvan": "Kedi"}
    del pet, secret


async def test_duzenleme_rolu_disinda_403_ve_izolasyon(world: World) -> None:
    await world.post("/custom-fields", _field(view_roles=["Yönetici", "Muhasebe"]))
    accounting = await staff(world, "muhasebe@test.local", "Muhasebe")
    path = f"/units/{world.unit_id}/custom-values"

    read = await call(world, "GET", path, headers=accounting)
    write = await call(world, "PUT", path, {"values": {"evcil_hayvan": "Kedi"}}, headers=accounting)
    other_site = await call(world, "GET", path, slug=OTHER)
    other_fields = (
        await call(world, "GET", "/custom-fields", slug=OTHER, params={"entity": "unit"})
    ).json()

    assert read.status_code == 200
    assert write.status_code == 403
    assert other_site.status_code == 404
    assert other_fields == []
