"""Finans Mod P işlemleri (servis istekleri 28–34): kasa gün sonu, ödeme planı, dönem dışı
ücret, muhasebe aktarımı, finans parametreleri, borç takibi, tahakkuk fark raporu."""

import csv
import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import CollectionAction, Notification
from site_yonetim.services import finance_jobs
from tests.integration.finance_world import World, finalized_plan, item_body, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def cash_ids(w: World) -> dict[str, str]:
    items = (await w.get("/cash-accounts")).json()["items"]
    return {a["name"]: a["id"] for a in items}


async def debt(w: World, amount: str = "1000.00", day: str = "2026-05-15") -> None:
    response = await w.post(
        f"/accounts/{w.account_id}/opening-balance",
        {"amount": amount, "direction": "debit", "date": day},
    )
    assert response.status_code == 201, response.text


async def pay(w: World, amount: str, day: str = "2026-06-20", **extra: Any) -> httpx2.Response:
    return await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": amount,
            "date": day,
            "method": "bank_transfer",
            **extra,
        },
    )


async def rule(w: World, action: str, role: str = "Yönetici") -> None:
    response = await w.post(
        "/approval-rules",
        {
            "action_type": action,
            "steps": [{"roles": [role], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": w.today[0].isoformat(),
        },
    )
    assert response.status_code == 201, response.text


@pytest.fixture
async def other(world: World) -> dict[str, str]:
    user = await create_user(world.factory, "ikinci@test.local", full_name="Selin KAYA")
    await add_site_membership(world.factory, world.site_id, user.id, "Yönetici")
    return await login_headers(world.api, "ikinci@test.local")


async def approve(w: World, headers: dict[str, str], request_id: str) -> None:
    response = await w.api.post(w.url(f"/approvals/{request_id}/approve"), json={}, headers=headers)
    assert response.status_code == 200, response.text


# --- 28 Kasa gün sonu ---------------------------------------------------------------------


async def test_kasa_gun_sonu_sayimi(world: World, other: dict[str, str]) -> None:
    w = world
    cash = await cash_ids(w)
    kasa, banka = cash["Kasa"], cash["Banka Hesabı"]
    for amount in ("2000.00", "400.00"):
        await w.post(
            "/cash-movements",
            {
                "cash_account_id": kasa,
                "date": "2026-06-19",
                "direction": "in",
                "amount": amount,
                "description": "Nakit tahsilat",
            },
        )
    days = (await w.get(f"/cash-accounts/{kasa}/days")).json()
    assert days["kind"] == "cash"
    assert days["open_days"] == ["2026-06-19"]
    url = f"/cash-accounts/{kasa}/days/close"
    missing = await w.post(url, {"date": "2026-06-19", "counted": "2350.00"})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "difference_reason"
    assert "50,00 TL eksik" in missing.json()["error"]["message"]
    closed = await w.post(
        url,
        {"date": "2026-06-19", "counted": "2350.00", "reason": "Bozuk para sayımında eksik çıktı"},
    )
    assert closed.status_code == 201, closed.text
    data = closed.json()["data"]
    assert (data["system_balance"], data["difference"], data["status"]) == (
        "2400.00",
        "-50.00",
        "closed",
    )
    again = await w.post(url, {"date": "2026-06-19", "counted": "2400.00"})
    assert again.json()["error"]["code"] == "day_closed"
    late = await w.post(
        "/cash-movements",
        {
            "cash_account_id": kasa,
            "date": "2026-06-19",
            "direction": "out",
            "amount": "1.00",
            "description": "Kırtasiye",
        },
    )
    assert late.status_code == 422
    assert late.json()["error"]["code"] == "cash_day_closed"
    bank = await w.post(
        f"/cash-accounts/{banka}/days/close", {"date": "2026-06-19", "counted": "0.00"}
    )
    assert bank.json()["error"]["code"] == "not_cash"
    future = await w.post(url, {"date": "2026-06-21", "counted": "0.00"})
    assert future.status_code == 422
    # "Kasa farkı" onay kuralı: farklı sayım onaya gider, onaylanınca kapanır
    await rule(w, "cash_difference")
    await w.post(
        "/cash-movements",
        {
            "cash_account_id": kasa,
            "date": "2026-06-20",
            "direction": "in",
            "amount": "100.00",
            "description": "Nakit tahsilat",
        },
    )
    pending = await w.post(
        url, {"date": "2026-06-20", "counted": "2440.00", "reason": "Kasada fazla para sayıldı"}
    )
    assert pending.status_code == 202, pending.text
    assert pending.json()["data"]["status"] == "approval_pending"
    await approve(w, other, pending.json()["data"]["approval_request_id"])
    closed_days = (await w.get(f"/cash-accounts/{kasa}/days")).json()["closed"]
    assert [d["status"] for d in closed_days] == ["closed", "closed"]
    # yetki: YK üyesi okur, kapatamaz
    board = await create_user(w.factory, "yk@test.local")
    await add_site_membership(w.factory, w.site_id, board.id, "Yönetim Kurulu Üyesi")
    headers = await login_headers(w.api, "yk@test.local")
    assert (
        await w.api.get(w.url(f"/cash-accounts/{kasa}/days"), headers=headers)
    ).status_code == 200
    assert (
        await w.api.post(
            w.url(url), json={"date": "2026-06-18", "counted": "0.00"}, headers=headers
        )
    ).status_code == 403


# --- 30 Ödeme planı -----------------------------------------------------------------------


async def test_odeme_plani_deftere_yazilir_ve_taksitlere_mahsup_edilir(world: World) -> None:
    w = world
    await debt(w)
    url = f"/accounts/{w.account_id}/payment-plans"
    bad = await w.post(
        url, {"amount": "1500.00", "installment_count": 1, "first_due_date": "2026-06-01"}
    )
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"amount", "installment_count", "first_due_date"}
    key = {"Idempotency-Key": str(uuid.uuid4())}
    body = {"amount": "1000.00", "installment_count": 3, "first_due_date": "2026-07-10"}
    created = await w.post(url, body, headers=key)
    assert created.status_code == 201, created.text
    assert (await w.post(url, body, headers=key)).json() == created.json()
    plan = created.json()["data"]
    assert [i["amount"] for i in plan["installments"]] == ["333.34", "333.33", "333.33"]
    assert [i["due_date"] for i in plan["installments"]] == [
        "2026-07-10",
        "2026-08-10",
        "2026-09-10",
    ]
    assert plan["status"] == "active"
    assert plan["tolerance_days"] == 10
    assert await w.balance() == Decimal("1000.00")  # bakiye değişmez, vadeler değişir
    exists = await w.post(url, body)
    assert exists.json()["error"]["code"] == "plan_exists"
    # tahsilat FIFO ile taksitlere
    assert (await pay(w, "400.00")).status_code == 201
    plans = (await w.get(url)).json()
    assert [i["status"] for i in plans[0]["installments"]] == ["paid", "partial", "open"]
    assert plans[0]["paid_total"] == "400.00"
    # bozulma: 2. taksit vade + 10 gün geçti
    w.set_today(date(2026, 8, 21))
    listed = (await w.get("/payment-plans")).json()
    assert listed["items"][0]["status"] == "broken"
    assert listed["counts"]["broken"] == 1
    results = await finance_jobs.run_all(w.factory, now=datetime.now(UTC), today=w.today[0])
    assert dict(results)[w.site_id].broken_plans == 1
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = list(await session.scalars(select(Notification.kind)))
    assert "payment_plan.broken" in kinds
    # iptal: kalan 600 ilk vadesiyle yeniden açılır, bakiye aynı
    short = await w.post(f"/payment-plans/{plan['id']}/cancel", {"reason": "yok"})
    assert short.status_code == 422
    cancelled = await w.post(
        f"/payment-plans/{plan['id']}/cancel", {"reason": "Sakin taksitleri ödemedi"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert await w.balance() == Decimal("600.00")
    statement = (await w.get(f"/accounts/{w.account_id}/statement")).json()
    newest = statement["entries"]["items"][0]
    assert newest["due_date"] == "2026-05-15"
    assert newest["debit"] == "600.00"
    again = await w.post(f"/payment-plans/{plan['id']}/cancel", {"reason": "Tekrar iptal"})
    assert again.json()["error"]["code"] == "already_cancelled"


async def test_odeme_plani_onay_ve_sakin_ucu(world: World, other: dict[str, str]) -> None:
    w = world
    await debt(w)
    await rule(w, "payment_plan")
    url = f"/accounts/{w.account_id}/payment-plans"
    pending = await w.post(
        url, {"amount": "600.00", "installment_count": 2, "first_due_date": "2026-07-01"}
    )
    assert pending.status_code == 202, pending.text
    data = pending.json()["data"]
    assert data["status"] == "approval_pending"
    assert data["approval_request_id"]
    assert await w.balance() == Decimal("1000.00")
    cancel = await w.post(f"/payment-plans/{data['id']}/cancel", {"reason": "Vazgeçildi"})
    assert cancel.json()["error"]["code"] == "plan_pending"
    await approve(w, other, data["approval_request_id"])
    active = (await w.get(url)).json()[0]
    assert active["status"] == "active"
    assert len(active["installments"]) == 2
    # sakin: yalnız kendi hesabının planı
    resident = await create_user(w.factory, "sakin@test.local", full_name="Ayşe YILMAZ")
    await add_site_membership(
        w.factory, w.site_id, resident.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    headers = await login_headers(w.api, "sakin@test.local")
    mine = await w.api.get(w.url("/resident/payment-plans"), headers=headers)
    assert mine.status_code == 200, mine.text
    assert [p["total"] for p in mine.json()] == ["600.00"]
    assert (await w.api.get(w.url("/payment-plans"), headers=headers)).status_code == 403
    # site izolasyonu: başka sitenin kullanıcısı hesabı göremez
    stranger = await create_user(w.factory, "yabanci@test.local")
    await add_site_membership(w.factory, w.other_site_id, stranger.id, "Yönetici")
    foreign = await login_headers(w.api, "yabanci@test.local")
    assert (await w.api.get(w.url(url, "yildiz-sitesi"), headers=foreign)).status_code == 404
    assert (await w.api.get(w.url("/payment-plans", "yildiz-sitesi"), headers=foreign)).json()[
        "total"
    ] == 0


# --- 31 Dönem dışı ücretler ---------------------------------------------------------------


async def test_donem_disi_ucret_aninda_ve_sonraki_tahakkukta(world: World) -> None:
    w = world
    settings = (await w.get("/settings/ad-hoc-charges")).json()
    assert settings["sources"]["reservation"] == {"mode": "immediate", "due_days": 10}
    bad = await w.api.put(
        w.url("/settings/ad-hoc-charges"),
        headers=w.headers,
        json={"sources": {"penalty": {"mode": "soon", "due_days": 5}}},
    )
    assert bad.status_code == 422
    saved = await w.api.put(
        w.url("/settings/ad-hoc-charges"),
        headers=w.headers,
        json={"sources": {"renovation": {"mode": "next_period", "due_days": 0}}},
    )
    assert saved.status_code == 200, saved.text
    body = {
        "unit_id": w.unit_id,
        "account_kind": "occupant",
        "source": "penalty",
        "description": "Gürültü cezası",
        "amount": "250.00",
    }
    immediate = await w.post("/ad-hoc-charges", body)
    assert immediate.status_code == 201, immediate.text
    data = immediate.json()["data"]
    assert data["status"] == "posted"
    assert data["due_date"] == "2026-06-30"
    assert await w.balance() == Decimal("250.00")
    empty = (await w.post("/units", {"block_id": w.lookups["block"], "number": "9"})).json()
    no_account = await w.post("/ad-hoc-charges", {**body, "unit_id": empty["data"]["id"]})
    assert no_account.status_code == 422
    assert no_account.json()["error"]["code"] == "no_account"
    cancelled = await w.post(f"/ad-hoc-charges/{data['id']}/cancel", {"reason": "Yanlış bölüm"})
    assert cancelled.status_code == 200, cancelled.text
    assert await w.balance() == Decimal("0.00")
    again = await w.post(f"/ad-hoc-charges/{data['id']}/cancel", {"reason": "Yanlış bölüm"})
    assert again.json()["error"]["code"] == "already_cancelled"
    # sonraki tahakkuk: ek kalemler
    waiting = await w.post(
        "/ad-hoc-charges", {**body, "source": "renovation", "description": "Tadilat izni"}
    )
    assert waiting.json()["data"]["status"] == "waiting_next_run"
    await finalized_plan(w)
    run = await post_run(w, "2026-06-01")
    assert run.status_code == 201, run.text
    listed = (await w.get("/ad-hoc-charges", params={"status": "posted"})).json()
    assert listed["total"] == 1
    assert listed["items"][0]["charge_run_id"] == run.json()["data"]["id"]
    statement = (await w.get(f"/accounts/{w.account_id}/statement")).json()["entries"]["items"]
    assert any(e["description"].startswith("06/2026 ek kalemler") for e in statement)
    # koşu ters kaydedilince ek kalem yeniden bekler
    reversed_run = await w.post(
        f"/charge-runs/{run.json()['data']['id']}/reverse", {"reason": "Hatalı tutar"}
    )
    assert reversed_run.status_code == 200, reversed_run.text
    assert (await w.get("/ad-hoc-charges", params={"status": "waiting_next_run"})).json()[
        "total"
    ] == 1
    assert await w.balance() == Decimal("0.00")


async def test_donem_disi_ucret_onaya_tabi(world: World, other: dict[str, str]) -> None:
    w = world
    await rule(w, "ad_hoc_charge")
    pending = await w.post(
        "/ad-hoc-charges",
        {
            "unit_id": w.unit_id,
            "account_kind": "occupant",
            "source": "service",
            "description": "Halı yıkama",
            "amount": "80.00",
        },
    )
    assert pending.status_code == 202, pending.text
    assert await w.balance() == Decimal("0.00")
    await approve(w, other, pending.json()["data"]["approval_request_id"])
    assert await w.balance() == Decimal("80.00")


# --- 32 Muhasebe aktarımı -----------------------------------------------------------------


async def test_muhasebe_aktarimi_fis_bir_kez_aktarilir(world: World) -> None:
    w = world
    await debt(w, "300.00", "2026-06-01")
    assert (await pay(w, "100.00", "2026-06-10")).status_code == 201
    assert (await pay(w, "50.00", "2026-06-11", method="other")).status_code == 201
    await w.post(
        "/ad-hoc-charges",
        {
            "unit_id": w.unit_id,
            "account_kind": "occupant",
            "source": "penalty",
            "description": "Ceza",
            "amount": "20.00",
        },
    )
    mappings = (await w.get("/accounting/mappings")).json()["items"]
    assert {m["key"] for m in mappings} >= {"charge", "payment_cash", "expense", "refund"}
    preview = (await w.get("/accounting/exports/preview", params={"period": "2026-06"})).json()
    assert len(preview["vouchers"]) == 3
    assert preview["total"] == "170.00"
    assert preview["missing_mappings"] == ["Tahsilat — diğer"]
    missing = await w.post("/accounting/exports", {"period": "2026-06"})
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "mapping_missing"
    bad = await w.api.put(
        w.url("/accounting/mappings"),
        headers=w.headers,
        json={"items": [{"key": "payment_other", "debit_account": "10A", "credit_account": "120"}]},
    )
    assert bad.status_code == 422
    assert "payment_other.debit_account" in bad.json()["error"]["fields"]
    ok = await w.api.put(
        w.url("/accounting/mappings"),
        headers=w.headers,
        json={
            "items": [{"key": "payment_other", "debit_account": "102.01", "credit_account": "120"}]
        },
    )
    assert ok.status_code == 200, ok.text
    exported = await w.post("/accounting/exports", {"period": "2026-06"})
    assert exported.status_code == 201, exported.text
    batch = exported.json()["data"]
    assert batch["voucher_count"] == 3
    assert batch["total"] == "170.00"
    again = await w.post("/accounting/exports", {"period": "2026-06"})
    assert again.json()["error"]["code"] == "nothing_to_export"
    after = (await w.get("/accounting/exports/preview", params={"period": "2026-06"})).json()
    assert after["vouchers"] == []
    assert after["already_exported"] == 3
    file = await w.api.get(w.url(f"/accounting/exports/{batch['id']}/file"), headers=w.headers)
    assert file.status_code == 200
    text = file.content.decode("utf-8")
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
    assert rows[0][0] == "Fiş tarihi"
    assert len(rows) == 4
    assert sum(Decimal(r[6].replace(",", ".")) for r in rows[1:]) == Decimal("170.00")
    xlsx = await w.api.get(
        w.url(f"/accounting/exports/{batch['id']}/file"),
        headers=w.headers,
        params={"format": "xlsx"},
    )
    assert xlsx.status_code == 200
    assert xlsx.content[:2] == b"PK"
    history = (await w.get("/accounting/exports")).json()
    assert history["total"] == 1
    # ücret iptali yeni (ters) fiş üretir
    charge = (await w.get("/ad-hoc-charges")).json()["items"][0]
    await w.post(f"/ad-hoc-charges/{charge['id']}/cancel", {"reason": "Yanlış kayıt"})
    reverse = (await w.get("/accounting/exports/preview", params={"period": "2026-06"})).json()
    assert len(reverse["vouchers"]) == 1
    voucher = reverse["vouchers"][0]
    assert (voucher["debit_account"], voucher["credit_account"]) == ("602", "120")


# --- 33 Finans parametreleri --------------------------------------------------------------


async def test_finans_parametreleri_versiyonlu(world: World, other: dict[str, str]) -> None:
    w = world
    data = (await w.get("/finance-parameters")).json()
    current = data["current"]
    assert (current["proration"], current["due_day"], current["charge_source"]) == (
        "day",
        10,
        "budget_plan",
    )
    assert current["late_fee_enabled"] is False
    assert current["allocation_order"] == "late_fee_first"
    assert current["debtor_restriction_enabled"] is False
    assert data["current_period"] == "2026-06"
    assert data["earliest_financial_period"] == "2026-07"
    url = "/finance-parameters/versions"
    early = await w.post(url, {"effective_period": "2026-06", "values": {"proration": "month"}})
    assert early.status_code == 422
    assert early.json()["error"]["code"] == "period_locked"
    nothing = await w.post(url, {"effective_period": "2026-06", "values": {"due_day": 10}})
    assert nothing.json()["error"]["code"] == "no_change"
    invalid = await w.post(
        url,
        {
            "effective_period": "2026-07",
            "values": {
                "reminder_days": [10, 3, 30],
                "due_day": 31,
                "late_fee_monthly_rate_percent": "7.50",
            },
        },
    )
    assert invalid.status_code == 422
    assert set(invalid.json()["error"]["fields"]) == {
        "reminder_days",
        "due_day",
        "late_fee_monthly_rate_percent",
    }
    now = await w.post(url, {"effective_period": "2026-06", "values": {"plan_tolerance_days": 15}})
    assert now.status_code == 201, now.text
    later = await w.post(
        url,
        {"effective_period": "2026-07", "values": {"proration": "month", "late_fee_enabled": True}},
    )
    assert later.status_code == 201, later.text
    data = (await w.get("/finance-parameters")).json()
    assert data["current"]["plan_tolerance_days"] == 15
    assert data["current"]["proration"] == "day"  # Temmuz'dan geçerli; Haziran değişmez
    assert [v["effective_period"] for v in data["scheduled"]] == ["2026-07"]
    w.set_today(date(2026, 7, 2))
    july = (await w.get("/finance-parameters")).json()["current"]
    assert july["proration"] == "month"
    assert july["plan_tolerance_days"] == 15
    # finansal etkili değişiklik onaya tabi
    await rule(w, "finance_parameter")
    # (onay gerçek saatle verilir; yürürlüğü onay ayından sonra olan sürüm kaymaz)
    pending = await w.post(url, {"effective_period": "2027-01", "values": {"due_day": 5}})
    assert pending.status_code == 202, pending.text
    version = pending.json()["data"]
    assert version["status"] == "approval_pending"
    await approve(w, other, version["approval_request_id"])
    versions = (await w.get("/finance-parameters")).json()["versions"]
    assert versions[0]["status"] == "active"
    assert versions[0]["effective_period"] == "2027-01"
    board = await create_user(w.factory, "yk@test.local")
    await add_site_membership(w.factory, w.site_id, board.id, "Yönetim Kurulu Üyesi")
    headers = await login_headers(w.api, "yk@test.local")
    forbidden = await w.api.post(
        w.url(url), headers=headers, json={"effective_period": "2026-09", "values": {"due_day": 6}}
    )
    assert forbidden.status_code == 403


# --- 29 Borç takibi -----------------------------------------------------------------------


async def test_borc_takibi_yaslandirma_kayit_ihtarname_ve_kademe(world: World) -> None:
    w = world
    await debt(w, "1000.00", "2026-06-10")
    aging = (await w.get("/receivables/aging")).json()
    debtors = (await w.get("/debtors")).json()
    assert aging["total"] == debtors["summary"]["total_balance"] == "1000.00"
    assert [b["count"] for b in aging["buckets"]] == [1, 0, 0, 0]
    assert aging["by_block"][0]["block"] == "A"
    assert aging["by_block"][0]["0_30"] == "1000.00"
    url = f"/accounts/{w.account_id}/collection-actions"
    bad = await w.post(url, {"kind": "reminder"})
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"channel"}
    sms = await w.post(url, {"kind": "reminder", "channel": "sms"})
    assert sms.status_code == 201
    assert "K5" in sms.json()["message"]
    assert sms.json()["data"]["delivered"] is False
    assert (await w.post(url, {"kind": "call", "note": "Ulaşılamadı"})).status_code == 201
    bulk = await w.post(
        "/collection-actions/bulk", {"account_ids": [w.account_id], "channel": "app"}
    )
    assert bulk.json()["data"]["count"] == 1
    letters = f"/accounts/{w.account_id}/demand-letters"
    missing = await w.post(letters, {"method": "notary", "sent_on": "2026-06-15"})
    assert set(missing.json()["error"]["fields"]) == {"tracking_no"}
    letter = await w.post(
        letters, {"method": "notary", "sent_on": "2026-06-15", "tracking_no": "Y-12345"}
    )
    assert letter.status_code == 201, letter.text
    letter_id = letter.json()["data"]["id"]
    early = await w.api.patch(
        w.url(f"/demand-letters/{letter_id}"), headers=w.headers, json={"served_on": "2026-06-14"}
    )
    assert early.status_code == 422
    served = await w.api.patch(
        w.url(f"/demand-letters/{letter_id}"), headers=w.headers, json={"served_on": "2026-06-18"}
    )
    assert served.json()["data"]["status"] == "served"
    assert (
        await w.api.patch(
            w.url(f"/demand-letters/{uuid.uuid4()}"),
            headers=w.headers,
            json={"served_on": "2026-06-18"},
        )
    ).status_code == 404
    status = (await w.get("/collection-status")).json()[w.account_id]
    assert status["reminder_count"] == 2
    assert status["letter_status"] == "served"
    timeline = (await w.get(f"/accounts/{w.account_id}/collection-timeline")).json()
    assert timeline["total"] == 4
    assert {i["kind"] for i in timeline["items"]} == {"reminder", "call", "demand_letter"}
    # kademe işi: vade +10 → bildirim + SMS kademesi, bir kez
    w.set_today(date(2026, 6, 21))
    first = await finance_jobs.run_all(w.factory, now=datetime.now(UTC), today=w.today[0])
    second = await finance_jobs.run_all(w.factory, now=datetime.now(UTC), today=w.today[0])
    assert dict(first)[w.site_id].reminders == 2
    assert dict(second)[w.site_id].reminders == 0
    with site_scope(w.site_id):
        async with w.factory() as session:
            stages = sorted(
                s for s in await session.scalars(select(CollectionAction.stage)) if s is not None
            )
    assert stages == ["app", "sms"]


async def test_odeme_planindaki_hesaba_kademe_yazilmaz(world: World) -> None:
    w = world
    await debt(w, "600.00", "2026-05-01")
    created = await w.post(
        f"/accounts/{w.account_id}/payment-plans",
        {"amount": "600.00", "installment_count": 2, "first_due_date": "2026-06-25"},
    )
    assert created.status_code == 201, created.text
    w.set_today(date(2026, 6, 30))  # 1. taksit 5 gün gecikmiş; tolerans içinde → plan aktif
    results = await finance_jobs.run_all(w.factory, now=datetime.now(UTC), today=w.today[0])
    assert dict(results)[w.site_id].reminders == 0


# --- 34 Tahakkuk fark raporu --------------------------------------------------------------


async def test_tahakkuk_fark_raporu(world: World) -> None:
    w = world
    await finalized_plan(w, item_body(w, annual_amount="12000.00"))
    first = await w.get("/charge-runs/preview/diff", params={"charge_date": "2026-06-01"})
    assert first.status_code == 200, first.text
    assert first.json()["base_period"] is None
    assert first.json()["rows"] == []
    assert (await post_run(w, "2026-06-01")).status_code == 201
    # ikinci bölüm: Temmuz'da "yeni" hesap
    unit = (await w.post("/units", {"block_id": w.lookups["block"], "number": "2"})).json()
    await w.post(
        f"/units/{unit['data']['id']}/parties",
        {
            "role": "owner",
            "start_date": "2020-01-01",
            "person": {"first_name": "Mehmet", "last_name": "Kaya"},
        },
    )
    diff = (await w.get("/charge-runs/preview/diff", params={"charge_date": "2026-07-01"})).json()
    assert diff["base_period"] == "06/2026"
    assert diff["threshold"] == 10
    assert diff["counts"] == {"increase": 0, "decrease": 1, "new": 1, "missing": 0}
    decrease = next(r for r in diff["rows"] if r["flag"] == "decrease")
    assert (decrease["previous"], decrease["current"], decrease["change_percent"]) == (
        "1000.00",
        "500.00",
        "-50.0",
    )
    # eşik parametresi değişince işaretler değişir
    await w.post(
        "/finance-parameters/versions",
        {"effective_period": "2026-07", "values": {"diff_threshold_percent": 60}},
    )
    again = (await w.get("/charge-runs/preview/diff", params={"charge_date": "2026-07-01"})).json()
    assert again["threshold"] == 60
    assert again["counts"]["decrease"] == 0
