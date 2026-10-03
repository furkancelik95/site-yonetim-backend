"""Devir bakiye — frontend servis isteği 02, issue #25 (gerçek PostgreSQL).

Kurgu `finance_world`: A-1 oturan hesabı (A1-O), henüz hareketi yok; bugün 20.06.2026.
"""

import uuid
from decimal import Decimal

from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

BODY = {
    "amount": "1500.00",
    "direction": "debit",
    "date": "2026-06-01",
    "description": "2025 yönetiminden devir",
}


def path(w: World) -> str:
    return f"/accounts/{w.account_id}/opening-balance"


async def test_devir_borc_ekstrede_ve_borclularda(world: World) -> None:
    response = await world.post(path(world), BODY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["message"] == "1.500,00 TL devir borç olarak işlendi."
    data = body["data"]
    assert {k: data[k] for k in ("amount", "direction", "date", "description")} == BODY

    entry = (await world.get(f"/accounts/{world.account_id}/statement")).json()["entries"]["items"][
        0
    ]
    assert entry["id"] == data["id"]
    assert (entry["source"], entry["debit"], entry["due_date"]) == (
        "opening",
        "1500.00",
        "2026-06-01",
    )
    assert entry["description"] == "Devir bakiye — 2025 yönetiminden devir"
    assert entry["running_balance"] == "1500.00"
    assert await world.balance() == Decimal("1500.00")

    [debtor] = (await world.get("/debtors")).json()["items"]
    assert (debtor["id"], debtor["oldest_open_due_date"], debtor["overdue_days"]) == (
        world.account_id, "2026-06-01", 19
    )  # fmt: skip

    # FIFO mahsuba girer: tahsilat devir borcunu kapatır
    payment = {"ledger_account_id": world.account_id, "amount": "1500.00", "method": "cash"}
    paid = await world.post("/payments", {**payment, "date": "2026-06-18"})
    assert paid.json()["data"]["closed_debt_count"] == 1
    assert (await world.get("/debtors")).json()["total"] == 0

    # pano özeti (tahakkuk/tahsilat) devirden etkilenmez; denetim kaydında tek satır
    audit = (await world.get("/audit", params={"entity": "opening_balances"})).json()
    assert audit["total"] == 1
    assert audit["items"][0]["after"]["amount"] == "1500.00"


async def test_hesap_basina_bir_kez(world: World) -> None:
    assert (await world.post(path(world), BODY)).status_code == 201
    again = await world.post(path(world), {**BODY, "direction": "credit"})
    assert again.status_code == 409
    assert again.json()["error"] == {
        "code": "already_exists",
        "message": "Bu hesaba devir bakiye daha önce girildi. Düzeltmek için ters kayıt kullanın.",
        "fields": None,
    }


async def test_devir_alacak_avans_olur(world: World) -> None:
    response = await world.post(
        path(world), {"amount": "250.00", "direction": "credit", "date": "2026-06-01"}
    )
    assert response.status_code == 201
    assert response.json()["message"] == "250,00 TL devir alacak olarak işlendi."
    assert response.json()["data"]["description"] is None
    entry = (await world.get(f"/accounts/{world.account_id}/statement")).json()["entries"]["items"][
        0
    ]
    assert (entry["credit"], entry["due_date"], entry["description"]) == (
        "250.00",
        None,
        "Devir bakiye",
    )
    assert await world.balance() == Decimal("-250.00")


async def test_dogrulama(world: World) -> None:
    for override, field in (
        ({"amount": "0.00"}, "amount"),
        ({"amount": "-5.00"}, "amount"),
        ({"date": "2026-06-21"}, "date"),
    ):
        response = await world.post(path(world), {**BODY, **override})
        assert response.status_code == 422, response.text
        assert field in response.json()["error"]["fields"], response.text
    future = await world.post(path(world), {**BODY, "date": "2026-06-21"})
    assert future.json()["error"]["fields"]["date"] == "Devir tarihi bugünden sonra olamaz."
    assert await world.balance() == Decimal("0.00")


async def test_ayni_anahtarla_tek_kayit(world: World) -> None:
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await world.post(path(world), BODY, headers=key)
    second = await world.post(path(world), BODY, headers=key)
    assert first.status_code == second.status_code == 201
    assert second.headers.get("idempotent-replayed") == "true"
    assert first.json() == second.json()
    assert await world.balance() == Decimal("1500.00")


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    user = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    auditor = await login_headers(world.api, "denetci@test.local")
    denied = await world.api.post(world.url(path(world)), json=BODY, headers=auditor)
    assert denied.status_code == 403
    other = await world.api.post(
        world.url(path(world), "yildiz-sitesi"), json=BODY, headers=world.headers
    )
    assert other.status_code == 404
    missing = await world.post(f"/accounts/{uuid.uuid4()}/opening-balance", BODY)
    assert missing.status_code == 404
    assert await world.balance() == Decimal("0.00")
