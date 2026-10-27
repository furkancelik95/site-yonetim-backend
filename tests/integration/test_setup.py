"""Kurulum durumu ve canlıya geçiş — servis isteği 26 (YAP-01); D-19: hisse toplamı %100
olmayan bölüm canlıya geçişi engeller."""

from typing import Any

from sqlalchemy import select

from site_yonetim.models import ChargeSchedule
from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, new_unit, scope, set_lifecycle, staff


def _item(body: dict[str, Any], key: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = body["checklist"]
    return next(i for i in items if i["key"] == key)


async def _complete(world: World) -> None:
    """Zorunlu maddeleri tamamlar: banka IBAN, otomatik tahakkuk, mutabakat, KVKK."""
    await world.post(
        "/cash-accounts",
        {"name": "Ziraat", "kind": "bank", "iban": "TR330006100519786457841326",
         "bank_name": "Ziraat"},
    )  # fmt: skip
    with scope(world):
        async with world.factory() as session, session.begin():
            schedule = await session.scalar(select(ChargeSchedule))
            if schedule is None:
                session.add(ChargeSchedule(enabled=True))
            else:
                schedule.enabled = True
    await world.post("/setup/opening-reconciled", {"note": "Mutabakat tutanağı 2026/1"})
    await call(world, "PUT", "/settings/registration", {"kvkk_published": True})


async def test_kontrol_listesi_ve_canliya_alma(world: World) -> None:
    await set_lifecycle(world, "setup")
    invite = {"full_name": "Test Deneme", "phone": "05321112233", "unit_id": world.unit_id,
              "relation": "tenant"}  # fmt: skip

    before = (await world.get("/setup")).json()
    refused = await world.post("/setup/go-live")
    blocked_invite = await world.post("/invitations", invite)
    await _complete(world)
    ready = (await world.get("/setup")).json()
    live = await world.post("/setup/go-live")
    again = await world.post("/setup/go-live")
    allowed_invite = await world.post("/invitations", invite)

    assert (before["status"], before["ready"], before["finance_mode"]) == (
        "setup",
        False,
        "platform",
    )
    assert [i["key"] for i in before["checklist"]] == [
        "structure", "owners", "shares", "land_share", "bank", "charge_rule", "opening",
        "manager", "mfa", "kvkk", "approvals", "registration",
    ]  # fmt: skip
    assert _item(before, "structure") == {
        "key": "structure", "label": "Yapı tanımlı (blok ve bölümler)", "ok": True,
        "detail": "1 blok, 1 bölüm", "link": "daireler", "required": True,
    }  # fmt: skip
    assert _item(before, "mfa")["required"] is False
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "checklist_incomplete"
    assert blocked_invite.json()["error"]["code"] == "site_not_live"
    assert ready["ready"] is True, [i for i in ready["checklist"] if i["required"] and not i["ok"]]
    assert live.status_code == 200, live.text
    assert live.json()["data"]["status"] == "live"
    assert again.json()["error"]["code"] == "already_live"
    assert allowed_invite.status_code == 201


async def test_maliksiz_ve_eksik_hisseli_bolum_canliya_almayi_engeller(world: World) -> None:
    await set_lifecycle(world, "setup")
    await _complete(world)
    await new_unit(world, "2", owner=None)
    half = await new_unit(world, "3", owner=None)
    await world.post(
        f"/units/{half}/parties",
        {"role": "owner", "start_date": "2020-01-01", "share_percent": "75.00",
         "person": {"first_name": "Ali", "last_name": "Can"}},
    )  # fmt: skip

    body = (await world.get("/setup")).json()
    refused = await world.post("/setup/go-live")

    assert body["ready"] is False
    assert _item(body, "owners")["detail"] == "1 bölümde malik yok: A-2"
    assert _item(body, "shares")["detail"] == "1 bölümde hisse toplamı %100 değil: A-3 (%75,00)"
    message = refused.json()["error"]["message"]
    assert "Her bölümün en az bir maliki var" in message
    assert "Malik hisseleri tam %100" in message


async def test_yetki_ve_izolasyon(world: World) -> None:
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")

    forbidden = await call(world, "GET", "/setup", headers=board)
    other = (await call(world, "GET", "/setup", slug=OTHER)).json()

    assert forbidden.status_code == 403
    assert _item(other, "structure")["ok"] is False  # diğer sitenin yapısı boş
