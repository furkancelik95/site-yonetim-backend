"""Tahsilat, FIFO mahsup, ekstre, borçlular, makbuz verisi — docs/07 §3.5–3.9 (gerçek PostgreSQL).

Kurgu `tests/integration/finance_world.py`: tek daire, tek kişi, oturan hesabı; aylık 1.000.
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import httpx2
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import LedgerAccount, PaymentAllocation, Period
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


@pytest.fixture
async def charged(world: World) -> World:
    """Kesinleşmiş proje + Haziran 2026 tahakkuku (1.000, vade 15.06.2026)."""
    await finalized_plan(world)
    assert (await post_run(world)).status_code == 201
    return world


async def pay(w: World, amount: str, day: str = "2026-06-18", **extra: object) -> httpx2.Response:
    headers = extra.pop("headers", {})
    body: dict[str, object] = {
        "ledger_account_id": w.account_id,
        "amount": amount,
        "date": day,
        "method": "bank_transfer",
    }
    body.update(extra)
    return await w.post("/payments", body, headers=headers)


async def allocated_total(w: World) -> Decimal:
    with site_scope(w.site_id):
        async with w.factory() as session:
            total = await session.scalar(select(func.sum(PaymentAllocation.amount)))
    return total or Decimal(0)


# --- 07 §3 ----------------------------------------------------------------------------


async def test_3_5_tahsilat_borcu_kapatir(charged: World) -> None:
    response = await pay(charged, "1000.00")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["applied"], data["unapplied"], data["closed_debt_count"]) == ("1000.00", "0.00", 1)
    assert data["balance"] == "0.00"
    assert response.json()["message"] == (
        "A-1 — Ayşe YILMAZ: 1.000,00 TL tahsilat kaydedildi, 1 borç kaydına mahsup edildi."
    )
    assert await charged.balance() == Decimal("0.00")


async def test_3_6_kismi_odeme_kalan_borcu_birakir(charged: World) -> None:
    await pay(charged, "400.00")
    assert await charged.balance() == Decimal("600.00")


async def test_3_7_fazla_odeme_avans_olarak_kalir(charged: World) -> None:
    data = (await pay(charged, "1500.00")).json()
    assert (data["data"]["applied"], data["data"]["unapplied"]) == ("1000.00", "500.00")
    assert data["message"].endswith("1 borç kaydına mahsup edildi, 500,00 TL avans olarak kaldı.")
    assert await charged.balance() == Decimal("-500.00")


async def test_3_8_ikinci_tahsilat_ayni_borca_tekrar_mahsup_edilmez(charged: World) -> None:
    await pay(charged, "1000.00")
    second = (await pay(charged, "200.00")).json()
    assert second["data"]["closed_debt_count"] == 0
    assert second["message"].endswith("tamamı avans olarak kaldı.")
    assert await charged.balance() == Decimal("-200.00")
    assert await allocated_total(charged) == Decimal("1000.00")  # 1.200 değil


@pytest.mark.parametrize("amount", ["0.00", "-50.00"])
async def test_3_9_sifir_veya_negatif_tutar_reddedilir(charged: World, amount: str) -> None:
    response = await pay(charged, amount)
    assert response.status_code == 422
    assert "amount" in response.json()["error"]["fields"]
    assert await charged.balance() == Decimal("1000.00")


# --- FIFO ve ters kayıt -----------------------------------------------------------------


async def test_oldest_debt_is_closed_first(charged: World) -> None:
    await post_run(charged, "2026-07-01")
    data = (await pay(charged, "1500.00", "2026-06-20")).json()["data"]
    assert data["closed_debt_count"] == 2
    receipt = (await charged.get(f"/payments/{data['payment']['id']}")).json()
    assert [(a["description"], a["amount"]) for a in receipt["allocations"]] == [
        ("06/2026 tahakkuku — Aidat", "1000.00"),
        ("07/2026 tahakkuku — Aidat", "500.00"),
    ]
    account = (await charged.get(f"/accounts/{charged.account_id}/statement")).json()["account"]
    assert account["balance"] == "500.00"
    assert account["oldest_open_due_date"] == "2026-07-15"


async def test_reversed_debt_is_not_open(charged: World) -> None:
    run_id = (await charged.get("/charge-runs")).json()["items"][0]["id"]
    await charged.post(f"/charge-runs/{run_id}/reverse", {"reason": "Hata"})
    data = (await pay(charged, "500.00")).json()["data"]
    assert (data["closed_debt_count"], data["unapplied"]) == (0, "500.00")
    assert await charged.balance() == Decimal("-500.00")


async def test_concurrent_payments_do_not_close_same_debt_twice(charged: World) -> None:
    first, second = await asyncio.gather(pay(charged, "700.00"), pay(charged, "700.00"))
    assert first.status_code == second.status_code == 201
    assert await allocated_total(charged) == Decimal("1000.00")
    assert await charged.balance() == Decimal("-400.00")


# --- doğrulama, idempotency, değişmezlik ---------------------------------------------


async def test_payment_rules(charged: World) -> None:
    future = await pay(charged, "10.00", "2026-06-21")
    assert future.json()["error"]["fields"] == {"date": "Tahsilat tarihi ileri bir tarih olamaz."}
    foreign = await charged.post(
        "/payments",
        {
            "ledger_account_id": str(uuid.uuid4()),
            "amount": "10.00",
            "date": "2026-06-18",
            "method": "cash",
        },
    )
    assert foreign.json()["error"]["code"] == "account_not_found"
    number = await pay(charged, 10)  # type: ignore[arg-type]
    assert number.status_code == 422  # para metin olmalı
    ok = await pay(charged, "10.00", reference="  EFT 123  ", note="Haziran")
    payment = ok.json()["data"]["payment"]
    assert (payment["reference"], payment["note"], payment["method"]) == (
        "EFT 123",
        "Haziran",
        "bank_transfer",
    )
    default_ref = (await pay(charged, "10.00")).json()["data"]["payment"]["reference"]
    assert default_ref == "A1-O"  # boşsa hesabın referans kodu


async def test_closed_account_and_closed_period(charged: World) -> None:
    with site_scope(charged.site_id):
        async with charged.factory() as session, session.begin():
            await session.execute(update(Period).values(status="closed"))
    closed_period = await pay(charged, "10.00")
    assert closed_period.status_code == 409
    assert (
        closed_period.json()["error"]["message"]
        == "06/2026 dönemi kapalı; bu tarihe tahsilat yazılamaz."
    )
    with site_scope(charged.site_id):
        async with charged.factory() as session, session.begin():
            await session.execute(update(Period).values(status="open"))
            await session.execute(update(LedgerAccount).values(is_closed=True))
    closed_account = await pay(charged, "10.00")
    assert closed_account.status_code == 409
    assert closed_account.json()["error"]["code"] == "account_closed"


async def test_same_key_records_payment_once(charged: World) -> None:
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await pay(charged, "300.00", headers=key)
    second = await pay(charged, "300.00", headers=key)
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert (await charged.get("/payments")).json()["total"] == 1
    assert await charged.balance() == Decimal("700.00")


async def test_allocations_are_immutable(charged: World) -> None:
    await pay(charged, "300.00")
    with site_scope(charged.site_id):
        async with charged.factory() as session:
            with pytest.raises(DBAPIError, match="değiştirilemez"):
                await session.execute(text("UPDATE payment_allocations SET amount = 1"))


# --- ekstre, borçlular, liste -----------------------------------------------------------


async def test_statement_newest_first_with_running_balance(charged: World) -> None:
    await pay(charged, "400.00")
    body = (await charged.get(f"/accounts/{charged.account_id}/statement")).json()
    entries = body["entries"]["items"]
    assert [(e["source"], e["debit"], e["credit"], e["running_balance"]) for e in entries] == [
        ("payment", "0.00", "400.00", "600.00"),
        ("charge", "1000.00", "0.00", "1000.00"),
    ]
    assert entries[0]["running_balance"] == body["account"]["balance"]
    assert (
        entries[0]["description"]
        == "18.06.2026 tahsilat (havale/EFT) — 1 borç kaydına mahsup edildi"
    )
    assert body["last_charge"]["period"] == "06/2026"
    assert body["last_charge"]["lines"][0]["description"] == "Aidat"
    paged = (
        await charged.get(
            f"/accounts/{charged.account_id}/statement", params={"page_size": 1, "page": 2}
        )
    ).json()
    assert [e["source"] for e in paged["entries"]["items"]] == ["charge"]


async def test_debtors(charged: World) -> None:
    charged.set_today(date(2026, 8, 20))
    await post_run(charged, "2026-07-01")
    body = (await charged.get("/debtors")).json()
    assert body["total"] == 1
    [row] = body["items"]
    assert (row["reference_code"], row["balance"], row["overdue_days"]) == ("A1-O", "2000.00", 66)
    assert row["person_name"] == "Ayşe YILMAZ"
    assert body["summary"] == {
        "total_balance": "2000.00",
        "debtor_count": 1,
        "over_30_days": "2000.00",
        "over_30_count": 1,
        "over_60_days": "2000.00",
        "over_60_count": 1,
        "average_balance": "2000.00",
    }
    await pay(charged, "2000.00", "2026-08-20")
    assert (await charged.get("/debtors")).json()["total"] == 0


async def test_accounts_and_payments_lists(charged: World) -> None:
    await pay(charged, "250.00")
    accounts = (await charged.get("/accounts", params={"q": "ayşe"})).json()
    assert {a["reference_code"]: a["balance"] for a in accounts["items"]} == {
        "A1-M": "0.00",
        "A1-O": "750.00",
    }
    assert (await charged.get("/accounts", params={"q": "A1-O"})).json()["total"] == 1
    assert (await charged.get("/accounts", params={"unit_id": charged.unit_id})).json()[
        "total"
    ] == 2
    payments = (
        await charged.get("/payments", params={"from": "2026-06-01", "to": "2026-06-30"})
    ).json()
    assert [(p["unit_name"], p["amount"], p["person_name"]) for p in payments["items"]] == [
        ("A-1", "250.00", "Ayşe YILMAZ")
    ]
    assert (await charged.get("/payments", params={"account_id": str(uuid.uuid4())})).json()[
        "total"
    ] == 0


# --- makbuz verisi ve erişim -----------------------------------------------------------------


async def test_receipt_data(charged: World) -> None:
    payment_id = (await pay(charged, "1200.00")).json()["data"]["payment"]["id"]
    receipt = (await charged.get(f"/payments/{payment_id}")).json()
    assert receipt["receipt_number"] is None  # K15
    assert receipt["site"]["name"] == "Aksu Konakları"
    assert (receipt["applied"], receipt["unapplied"]) == ("1000.00", "200.00")
    assert receipt["account"]["reference_code"] == "A1-O"
    assert receipt["payment"]["created_by_name"] == "Kerem YILDIRIM"
    assert (await charged.get(f"/payments/{uuid.uuid4()}")).status_code == 404


async def _headers_for(w: World, email: str, role: str, **fields: object) -> dict[str, str]:
    user = await create_user(w.factory, email, **fields.pop("user", {}))  # type: ignore[arg-type]
    await add_site_membership(w.factory, w.site_id, user.id, role, **fields)
    return await login_headers(w.api, email)


async def test_resident_sees_only_own_account(charged: World) -> None:
    payment_id = (await pay(charged, "100.00")).json()["data"]["payment"]["id"]
    own = await _headers_for(
        charged,
        "sakin@test.local",
        "Sakin",
        user={"kind": "resident"},
        person_id=uuid.UUID(charged.person_id),
    )
    statement = await charged.api.get(
        charged.url(f"/accounts/{charged.account_id}/statement"), headers=own
    )
    assert statement.status_code == 200
    assert statement.json()["account"]["person_name"] == "Ayşe YILMAZ"
    assert (
        await charged.api.get(charged.url(f"/payments/{payment_id}"), headers=own)
    ).status_code == 200
    assert (await charged.api.get(charged.url("/debtors"), headers=own)).status_code == 403

    other = await charged.post("/people", {"first_name": "Başka", "last_name": "Sakin"})
    stranger = await _headers_for(
        charged, "baska@test.local", "Sakin", user={"kind": "resident"},
        person_id=uuid.UUID(other.json()["data"]["id"]),
    )  # fmt: skip
    denied = await charged.api.get(
        charged.url(f"/accounts/{charged.account_id}/statement"), headers=stranger
    )
    assert denied.status_code == 403
    assert (
        await charged.api.get(charged.url(f"/payments/{payment_id}"), headers=stranger)
    ).status_code == 403


async def test_security_guard_cannot_see_balance(charged: World) -> None:
    """docs/05 §6: güvenlik görevlisi siteye girer ama sakin bakiyesinden 403 alır."""
    guard = await _headers_for(charged, "guvenlik@test.local", "Güvenlik")
    response = await charged.api.get(
        charged.url(f"/accounts/{charged.account_id}/statement"), headers=guard
    )
    assert response.status_code == 403


async def test_auditor_sees_finance_without_personal_data(charged: World) -> None:
    await pay(charged, "100.00")
    auditor = await _headers_for(charged, "denetci@test.local", "Denetçi")
    debtors = (await charged.api.get(charged.url("/debtors"), headers=auditor)).json()
    assert debtors["items"][0]["person_name"] is None
    assert debtors["items"][0]["balance"] == "900.00"
    payments = (await charged.api.get(charged.url("/payments"), headers=auditor)).json()
    assert payments["items"][0]["person_name"] is None
    run_id = (await charged.get("/charge-runs")).json()["items"][0]["id"]
    charges = (
        await charged.api.get(charged.url(f"/charge-runs/{run_id}/charges"), headers=auditor)
    ).json()
    assert charges["items"][0]["person_name"] is None
    record = await charged.api.post(
        charged.url("/payments"),
        json={
            "ledger_account_id": charged.account_id,
            "amount": "1.00",
            "date": "2026-06-18",
            "method": "cash",
        },
        headers=auditor,
    )
    assert record.status_code == 403


async def test_other_site_cannot_touch_account(charged: World) -> None:
    other = "yildiz-sitesi"
    statement = await charged.api.get(
        charged.url(f"/accounts/{charged.account_id}/statement", other), headers=charged.headers
    )
    assert statement.status_code == 404
    response = await charged.api.post(
        charged.url("/payments", other),
        json={
            "ledger_account_id": charged.account_id,
            "amount": "10.00",
            "date": "2026-06-18",
            "method": "cash",
        },
        headers=charged.headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "account_not_found"
    assert await charged.balance() == Decimal("1000.00")
