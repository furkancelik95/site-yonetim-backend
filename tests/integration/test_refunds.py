"""İade — frontend servis isteği 03, issue #26 (gerçek PostgreSQL).

Kurgu `finance_world`: A1-O'ya Haziran'da 1.000 TL tahakkuk, 1.250 TL tahsilat → 250 TL alacak.
Bugün 20.06.2026. İade "Banka Hesabı"ndan çıkar.
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers

REASON = "Taşınma, fazla ödeme iadesi"


async def pay(w: World, amount: str, day: str = "2026-06-18") -> dict[str, Any]:
    response = await w.post(
        "/payments",
        {"ledger_account_id": w.account_id, "amount": amount, "date": day, "method": "cash"},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def bank(w: World) -> str:
    accounts = (await w.get("/cash-accounts")).json()["items"]
    return str(next(a["id"] for a in accounts if a["name"] == "Banka Hesabı"))


async def refund(
    w: World, amount: str = "250.00", *, key: str | None = None, **overrides: Any
) -> httpx2.Response:
    body = {
        "ledger_account_id": w.account_id,
        "amount": amount,
        "date": "2026-06-20",
        "cash_account_id": await bank(w),
        "reason": REASON,
        **overrides,
    }
    return await w.post("/refunds", body, headers={"Idempotency-Key": key or str(uuid.uuid4())})


@pytest.fixture
async def credit(world: World) -> World:
    await finalized_plan(world)
    await post_run(world)
    await pay(world, "1250.00")
    assert await world.balance() == Decimal("-250.00")
    return world


async def test_iade_cari_ve_kasada_ayni_islemde(credit: World) -> None:
    response = await refund(credit)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["message"] == "250,00 TL iade kaydedildi."
    data = body["data"]
    assert {k: data[k] for k in ("ledger_account_id", "amount", "date", "reason")} == {
        "ledger_account_id": credit.account_id,
        "amount": "250.00",
        "date": "2026-06-20",
        "reason": REASON,
    }
    assert await credit.balance() == Decimal("0.00")

    entry = (await credit.get(f"/accounts/{credit.account_id}/statement")).json()["entries"][
        "items"
    ][0]
    assert (entry["source"], entry["source_id"], entry["debit"]) == ("refund", data["id"], "250.00")
    assert entry["description"] == f"İade — {REASON}"

    bank_id = await bank(credit)
    movements = (await credit.get(f"/cash-accounts/{bank_id}/statement")).json()["movements"][
        "items"
    ]
    assert (movements[0]["source"], movements[0]["outflow"]) == ("refund", "250.00")
    balances = {
        a["name"]: a["balance"] for a in (await credit.get("/cash-accounts")).json()["items"]
    }
    assert balances["Banka Hesabı"] == "-250.00"  # tahsilat kasaya işlenmemişti

    audit = (await credit.get("/audit", params={"entity": "refunds"})).json()
    assert audit["total"] == 1

    # iade borcu "açık borç" değildir: sonraki tahsilat yalnız yeni tahakkuku kapatır
    credit.set_today(date(2026, 7, 5))
    assert (await post_run(credit, "2026-07-01")).status_code == 201
    paid = await pay(credit, "1000.00", "2026-07-05")
    assert (paid["closed_debt_count"], paid["unapplied"], paid["balance"]) == (1, "0.00", "0.00")


async def test_alacak_yoksa_ve_alacagi_asarsa(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    none = await refund(world)
    assert none.status_code == 409
    assert none.json()["error"]["code"] == "no_credit"
    assert none.json()["error"]["message"] == "Bu hesabın alacak bakiyesi yok; iade yapılamaz."

    await pay(world, "2250.00")  # 1.250 alacak
    over = await refund(world, "1250.01")
    assert over.status_code == 422
    assert over.json()["error"]["fields"] == {"amount": "En fazla 1.250,00 TL iade edilebilir."}
    assert (await refund(world, "1250.00")).status_code == 201


async def test_dogrulama(credit: World) -> None:
    for kwargs, field, message in (
        ({"reason": "  "}, "reason", "Gerekçe zorunlu."),
        ({"reason": None}, "reason", "Gerekçe zorunlu."),
        ({"amount": "0.00"}, "amount", None),
        ({"date": "2026-06-21"}, "date", "İade tarihi ileri bir tarih olamaz."),
        ({"cash_account_id": str(uuid.uuid4())}, "cash_account_id", None),
    ):
        response = await refund(credit, **kwargs)
        assert response.status_code == 422, (kwargs, response.text)
        fields = response.json()["error"]["fields"]
        assert field in fields, (kwargs, response.text)
        if message:
            assert fields[field] == message
    # anahtarsız iade reddedilir (para çıkışı)
    body = {
        "ledger_account_id": credit.account_id,
        "amount": "250.00",
        "date": "2026-06-20",
        "cash_account_id": await bank(credit),
        "reason": REASON,
    }
    missing = await credit.post("/refunds", body)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "idempotency_key_required"
    assert await credit.balance() == Decimal("-250.00")


async def test_ayni_anahtar_ve_eszamanli_iade(credit: World) -> None:
    key = str(uuid.uuid4())
    first = await refund(credit, "100.00", key=key)
    second = await refund(credit, "100.00", key=key)
    assert first.status_code == second.status_code == 201
    assert second.headers.get("idempotent-replayed") == "true"
    assert await credit.balance() == Decimal("-150.00")

    # kalan 150 TL'yi aynı anda iki kez iade etmeye çalış: biri geçer
    results = await asyncio.gather(refund(credit, "150.00"), refund(credit, "150.00"))
    assert sorted(r.status_code for r in results) == [201, 409]
    assert await credit.balance() == Decimal("0.00")


async def test_yetki_ve_site_izolasyonu(credit: World) -> None:
    user = await create_user(credit.factory, "denetci@test.local")
    await add_site_membership(credit.factory, credit.site_id, user.id, "Denetçi")
    auditor = await login_headers(credit.api, "denetci@test.local")
    body = {
        "ledger_account_id": credit.account_id,
        "amount": "250.00",
        "date": "2026-06-20",
        "cash_account_id": await bank(credit),
        "reason": REASON,
    }
    headers = {**auditor, "Idempotency-Key": str(uuid.uuid4())}
    assert (
        await credit.api.post(credit.url("/refunds"), json=body, headers=headers)
    ).status_code == 403

    other = credit.url("/refunds", "yildiz-sitesi")
    headers = {**credit.headers, "Idempotency-Key": str(uuid.uuid4())}
    assert (await credit.api.post(other, json=body, headers=headers)).status_code == 404
    assert await credit.balance() == Decimal("-250.00")
