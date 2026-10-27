"""Kişiler — servis isteği 21 (YAP-04): mükerrer önerisi, birleştirme, malik değişimi (K10)."""

from datetime import date, datetime
from typing import Any

from site_yonetim.services.charging import BUSINESS_TZ
from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, new_unit, staff


async def _person(world: World, first: str, last: str, phone: str | None = None) -> str:
    body: dict[str, Any] = {"first_name": first, "last_name": last}
    if phone:
        body["phone"] = phone
    response = await world.post("/people", body)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


async def test_mukerrer_onerisi_ayni_telefon(world: World) -> None:
    a = await _person(world, "Ali", "Can", "05321112233")
    b = await _person(world, "Ali", "Cann", "+90 532 111 22 33")
    await _person(world, "Veli", "Kara", "05329998877")

    groups = (await world.get("/people/duplicates")).json()

    assert len(groups) == 1
    assert groups[0]["kind"] == "phone"
    assert groups[0]["key"] == "+90 532 *** ** 33"
    assert {p["id"] for p in groups[0]["people"]} == {a, b}


async def test_birlestirme_iliskileri_ve_hesaplari_tasir(world: World) -> None:
    second = await new_unit(world, "2", owner="Ayşe Yılmaz")  # aynı kişinin ikinci kaydı
    detail = (await world.get(f"/units/{second}")).json()
    duplicate = detail["parties"][0]["person"]["id"]

    merged = await world.post("/people/merge", {"keep_id": world.person_id, "merge_id": duplicate})

    assert merged.status_code == 200, merged.text
    assert merged.json()["data"]["kept"]["id"] == world.person_id
    after = (await world.get(f"/units/{second}")).json()
    assert after["parties"][0]["person"]["id"] == world.person_id
    assert {a["person_id"] for a in after["accounts"]} == {world.person_id}
    people = (await world.get("/people")).json()["items"]
    assert duplicate not in {p["id"] for p in people}
    again = await world.post("/people/merge", {"keep_id": world.person_id, "merge_id": duplicate})
    assert again.status_code == 404


async def test_birlestirme_kurallari(world: World) -> None:
    other_person = (
        await call(world, "POST", "/people", {"first_name": "Ali", "last_name": "Can"}, slug=OTHER)
    ).json()["data"]["id"]

    same = await world.post(
        "/people/merge", {"keep_id": world.person_id, "merge_id": world.person_id}
    )
    foreign = await world.post(
        "/people/merge", {"keep_id": world.person_id, "merge_id": other_person}
    )
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    forbidden = await call(
        world, "POST", "/people/merge",
        {"keep_id": world.person_id, "merge_id": other_person}, headers=board,
    )  # fmt: skip

    assert same.status_code == 422
    assert same.json()["error"]["message"] == "Birleştirilecek iki ayrı kişi seçin."
    assert foreign.status_code == 404
    assert forbidden.status_code == 403


async def test_ayni_bolumde_ayni_hesap_turu_varsa_birlestirilemez(world: World) -> None:
    duplicate = await _person(world, "Ayse", "Yilmaz")
    added = await world.post(
        f"/units/{world.unit_id}/parties",
        {"role": "tenant", "start_date": "2020-01-01", "person_id": duplicate},
    )
    assert added.status_code == 201
    # Malik zaten -O hesabına sahip; kiracı -K (occupant) — aynı tür, aynı bölüm.
    response = await world.post(
        "/people/merge", {"keep_id": world.person_id, "merge_id": duplicate}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "accounts_conflict"


def _transfer(**changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "reason": "sale",
        "transfer_date": "2026-07-01",
        "debt_policy": "stays_with_seller",
        "new_owners": [
            {"person": {"first_name": "Test", "last_name": "Deneme", "phone": "05321112233"},
             "share_percent": "50.00"},
            {"person": {"first_name": "Veli", "last_name": "Kara"}, "share_percent": "50.00"},
        ],
    }  # fmt: skip
    body.update(changes)
    return body


async def test_malik_degisimi_satis(world: World) -> None:
    response = await world.post(f"/units/{world.unit_id}/ownership-transfer", _transfer())

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "done"
    owners = [p for p in data["unit"]["parties"] if p["role"] == "owner"]
    old = next(p for p in owners if p["person"]["id"] == world.person_id)
    new = [p for p in owners if p["person"]["id"] != world.person_id]
    assert old["end_date"] == "2026-06-30"  # bitiş günü dahil: devirden bir gün önce
    assert sorted((p["start_date"], p["share_percent"]) for p in new) == [
        ("2026-07-01", "50.00"),
        ("2026-07-01", "50.00"),
    ]
    # K10: eski malikin cari hesapları açık kalır, yenilere yeni cari açılır
    accounts = data["unit"]["accounts"]
    assert all(not a["is_closed"] for a in accounts)
    assert len({a["person_id"] for a in accounts}) == 3
    assert "Satış öncesi borç eski malikte kaldı." in response.json()["message"]


async def test_malik_degisimi_kurallari(world: World) -> None:
    path = f"/units/{world.unit_id}/ownership-transfer"
    partial = _transfer(
        new_owners=[{"person": {"first_name": "Veli", "last_name": "Kara"}, "share_percent": "60"}]
    )

    shares = await world.post(path, partial)
    debt = await world.post(path, _transfer(debt_policy="transfers_to_buyer"))
    locked = await world.post(path, _transfer(transfer_date="2026-05-31"))
    other_site = await call(world, "POST", path, _transfer(), slug=OTHER)

    assert shares.status_code == 422
    assert shares.json()["error"]["fields"] == {
        "new_owners": "Yeni maliklerin hisse toplamı tam %100 olmalı (şu an %60,00)."
    }
    assert debt.json()["error"]["code"] == "not_supported"
    assert locked.json()["error"]["code"] == "period_locked"
    assert other_site.status_code == 404


async def test_malik_degisimi_atomik_ve_mukerrer_kisi_onerisi(world: World) -> None:
    existing = await _person(world, "Ali", "Can", "05321112233")
    response = await world.post(
        f"/units/{world.unit_id}/ownership-transfer", _transfer()
    )  # ilk yeni malik bu telefonla kayıtlı

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "person_exists"
    assert error["fields"]["existing_person_id"] == existing
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    assert [(p["role"], p["end_date"]) for p in unit["parties"]] == [("owner", None)]


async def test_malik_degisimi_onay_kurali_varsa_onaya_duser(world: World) -> None:
    # Yürütmede dönem kuralı talebin açıldığı (gerçek) güne göre işler.
    real = datetime.now(BUSINESS_TZ).date()
    transfer_on = date(real.year + (real.month == 12), real.month % 12 + 1, 1).isoformat()
    rule = await world.post(
        "/approval-rules",
        {"action_type": "ownership_transfer", "steps": [{"roles": ["Yönetici"], "quorum": "any"}],
         "remind_after_days": 2, "escalate_after_days": 5, "effective_from": "2026-06-20"},
    )  # fmt: skip
    assert rule.status_code in (200, 201), rule.text
    approver = await staff(world, "yonetici2@test.local", "Yönetici")

    response = await world.post(
        f"/units/{world.unit_id}/ownership-transfer", _transfer(transfer_date=transfer_on)
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["status"] == "approval_pending"
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    assert len(unit["parties"]) == 1  # onaylanana kadar değişmez

    request_id = data["approval_request_id"]
    decided = await call(world, "POST", f"/approvals/{request_id}/approve", {}, headers=approver)
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    assert len([p for p in unit["parties"] if p["role"] == "owner"]) == 3
