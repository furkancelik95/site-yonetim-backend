"""Finans motoru, ikinci yarı (P03b; Yazılım Dokümanı FIN-02..05, FIN-08, FIN-16, FIN-17):
K25 vade günü, D-6 avans mahsubu, K23 aidat kuralları, K21 tahakkuk onayı, K2 gecikme, D-10 satır
düzeltmesi (kilitli dönem dahil), B17 bütçe–gerçekleşme. Kurgu: docs/07 §3 (`finance_world`)."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from site_yonetim.api.deps import get_now
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import (
    ChargeLine,
    LateFeeAccrual,
    LedgerEntry,
    Notification,
    PaymentAllocation,
)
from site_yonetim.services import charge_schedule, finance_jobs
from tests.integration.finance_world import World, finalized_plan, item_body, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers
from tests.integration.test_finance_ops import approve, pay, rule
from tests.integration.test_period_lock import set_period


@pytest.fixture
async def other(world: World) -> dict[str, str]:
    user = await create_user(world.factory, "ikinci@test.local", full_name="Selin KAYA")
    await add_site_membership(world.factory, world.site_id, user.id, "Yönetici")
    return await login_headers(world.api, "ikinci@test.local")


async def params(w: World, effective: str, **values: Any) -> None:
    response = await w.post(
        "/finance-parameters/versions", {"effective_period": effective, "values": values}
    )
    assert response.status_code == 201, response.text


async def entries(w: World, source: str | None = None) -> list[LedgerEntry]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            query = select(LedgerEntry).where(LedgerEntry.account_id == uuid.UUID(w.account_id))
            if source:
                query = query.where(LedgerEntry.source == source)
            return list(await session.scalars(query.order_by(LedgerEntry.created_at)))


async def allocated_to(w: World, entry_id: uuid.UUID) -> Decimal:
    with site_scope(w.site_id):
        async with w.factory() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                    PaymentAllocation.ledger_entry_id == entry_id
                )
            )
    return Decimal(total or 0)


# --- K25 vade günü ------------------------------------------------------------------------


async def test_k25_vade_ayin_10u_ve_parametre(world: World) -> None:
    w = world
    await finalized_plan(w)
    preview = (await w.get("/charge-runs/preview", params={"charge_date": "2026-06-01"})).json()
    assert preview["due_date"] == "2026-06-10"
    assert (preview["source"], preview["proration"]) == ("budget_plan", "day")
    assert preview["approval_required"] is False
    await params(w, "2026-07", due_day=20)
    july = (await w.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})).json()
    assert july["due_date"] == "2026-07-20"
    schedule = (await w.get("/charge-schedule")).json()
    assert (schedule["due_day"], schedule["draft_lead_days"]) == (10, 5)


# --- D-6 avansın sonraki borca mahsubu ------------------------------------------------------


async def test_d6_fazla_odeme_sonraki_tahakkuka_mahsup_edilir(world: World) -> None:
    """FIN-08 S2: 1.500 TL tahsilat, 1.000 TL borç → 500 avans; sonraki tahakkuktan düşer."""
    w = world
    await finalized_plan(w)
    assert (await post_run(w, "2026-06-01")).status_code == 201
    paid = await pay(w, "1500.00")
    assert paid.json()["data"]["unapplied"] == "500.00"
    assert await w.balance() == Decimal("-500.00")
    assert (await post_run(w, "2026-07-01")).status_code == 201
    assert await w.balance() == Decimal("500.00")
    july = next(e for e in await entries(w, "charge") if e.date == date(2026, 7, 1))
    assert await allocated_to(w, july.id) == Decimal("500.00")  # mahsup kaydı üretildi
    # ikinci tahakkukta artık avans yok: yeni mahsup yazılmaz
    assert (await post_run(w, "2026-08-01")).status_code == 201
    august = next(e for e in await entries(w, "charge") if e.date == date(2026, 8, 1))
    assert await allocated_to(w, august.id) == Decimal("0")
    assert await w.balance() == Decimal("1500.00")


async def test_d6_ters_kaydi_alinan_odenmis_borc_avansa_doner(world: World) -> None:
    w = world
    await finalized_plan(w)
    june = (await post_run(w, "2026-06-01")).json()["data"]["id"]
    await pay(w, "1000.00")
    reversed_ = await w.post(f"/charge-runs/{june}/reverse", {"reason": "Hatalı kesim"})
    assert reversed_.status_code == 200, reversed_.text
    assert await w.balance() == Decimal("-1000.00")
    assert (await post_run(w, "2026-06-01")).status_code == 201
    assert await w.balance() == Decimal("0.00")
    new = [e for e in await entries(w, "charge") if e.debit > 0][-1]
    assert await allocated_to(w, new.id) == Decimal("1000.00")


# --- K23 aidat kuralları ------------------------------------------------------------------


def rule_body(w: World, **version: Any) -> dict[str, Any]:
    return {
        "name": "2026 aylık aidat",
        "charge_type_id": w.lookups["aidat"],
        "owner_item": False,
        "version": {
            "effective_period": "2026-07",
            "kind": "equal",
            "amount": "1500.00",
            "basis_note": "Yönetim planı md. 12",
            **version,
        },
    }


async def create_rule(w: World, **version: Any) -> dict[str, Any]:
    response = await w.post("/billing-rules", rule_body(w, **version))
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def test_k23_aidat_kurali_kaynagiyla_tahakkuk(world: World) -> None:
    w = world
    await params(w, "2026-07", charge_source="billing_rule")
    created = await create_rule(w)
    version = created["versions"][0]
    assert (version["status"], version["amount_mode"]) == ("draft", "total")
    trial = await w.post(f"/billing-rules/{created['id']}/versions/{version['id']}/trial")
    assert trial.status_code == 200, trial.text
    assert (trial.json()["period_total"], trial.json()["annual_total"]) == ("1500.00", "18000.00")
    # yürürlükte kural yokken kaynak 'aidat kuralı' → 409
    none = await w.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})
    assert none.json()["error"]["code"] == "no_billing_rules"
    submitted = await w.post(f"/billing-rules/{created['id']}/versions/{version['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["versions"][0]["status"] == "active"
    preview = (await w.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})).json()
    assert (preview["source"], preview["budget_plan"], preview["total_amount"]) == (
        "billing_rule",
        None,
        "1500.00",
    )
    posted = await post_run(w, "2026-07-01")
    assert posted.status_code == 201, posted.text
    assert posted.json()["data"]["source"] == "billing_rule"
    with site_scope(w.site_id):
        async with w.factory() as session:
            [line] = list(await session.scalars(select(ChargeLine)))
    assert (line.budget_item_id, line.billing_rule_version_id) == (
        None,
        uuid.UUID(version["id"]),
    )
    # FIN-02 S3: Ağustos'tan yürürlüğe giren yeni sürüm Temmuz'u değiştirmez
    v2 = await w.post(
        f"/billing-rules/{created['id']}/versions",
        {
            "effective_period": "2026-08",
            "kind": "equal",
            "amount": "1800.00",
            "basis_note": "Genel kurul 2026/3",
        },
    )
    assert v2.status_code == 201, v2.text
    v2_id = v2.json()["data"]["versions"][0]["id"]
    assert (
        await w.post(f"/billing-rules/{created['id']}/versions/{v2_id}/submit")
    ).status_code == 200
    july = (await w.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})).json()
    august = (await w.get("/charge-runs/preview", params={"charge_date": "2026-08-01"})).json()
    assert (july["total_amount"], august["total_amount"]) == ("1500.00", "1800.00")
    assert july["already_charged"] is True


async def test_k23_kural_dogrulama_eksik_veri_cakisma_ve_onay(
    world: World, other: dict[str, str]
) -> None:
    w = world
    invalid = await w.post("/billing-rules", rule_body(w, amount="0", basis_note=""))
    assert invalid.status_code == 422
    assert set(invalid.json()["error"]["fields"]) == {"amount", "basis_note"}
    by_area = await create_rule(w, kind="by_area")
    version = by_area["versions"][0]["id"]
    missing = await w.post(f"/billing-rules/{by_area['id']}/versions/{version}/submit")
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "missing_unit_data"
    past = await create_rule(w, effective_period="2026-05")
    past_v = past["versions"][0]["id"]
    late = await w.post(f"/billing-rules/{past['id']}/versions/{past_v}/submit")
    assert late.status_code == 422
    first = await create_rule(w)
    first_v = first["versions"][0]["id"]
    assert (
        await w.post(f"/billing-rules/{first['id']}/versions/{first_v}/submit")
    ).status_code == 200
    second = await create_rule(w)
    second_v = second["versions"][0]["id"]
    clash = await w.post(f"/billing-rules/{second['id']}/versions/{second_v}/submit")
    assert clash.json()["error"]["code"] == "scope_overlap"
    # "Aidat kuralı" onay kuralı → 202, onaylanınca yürürlükte
    await rule(w, "charge_rule")
    owner_rule = await w.post(
        "/billing-rules",
        {**rule_body(w), "name": "Demirbaş", "charge_type_id": w.lookups["demirbas"]},
    )
    data = owner_rule.json()["data"]
    pending = await w.post(
        f"/billing-rules/{data['id']}/versions/{data['versions'][0]['id']}/submit"
    )
    assert pending.status_code == 202, pending.text
    request_id = pending.json()["data"]["versions"][0]["approval_request_id"]
    await approve(w, other, request_id)
    after = (await w.get(f"/billing-rules/{data['id']}")).json()
    assert after["versions"][0]["status"] == "active"


async def test_aidat_kurali_site_izolasyonu_ve_yetki(world: World) -> None:
    w = world
    created = await create_rule(w)
    foreign = await w.api.get(
        w.url(f"/billing-rules/{created['id']}", slug="yildiz-sitesi"), headers=w.headers
    )
    assert foreign.status_code == 404
    guard = await create_user(w.factory, "guvenlik@test.local", full_name="Ali VAR")
    await add_site_membership(w.factory, w.site_id, guard.id, "Güvenlik")
    headers = await login_headers(w.api, "guvenlik@test.local")
    assert (await w.api.get(w.url("/billing-rules"), headers=headers)).status_code == 403
    assert (await w.api.get(w.url("/budgets"), headers=headers)).status_code == 403


# --- K21 / FIN-05 tahakkuk taslağı ve onayı -------------------------------------------------


async def test_k21_kural_varsa_tahakkuk_onaya_gider_onaylaninca_kesinlesir(
    world: World, other: dict[str, str]
) -> None:
    w = world
    await finalized_plan(w)
    await rule(w, "charge_run")
    sent = await post_run(w, "2026-06-01")
    assert sent.status_code == 202, sent.text
    run = sent.json()["data"]
    assert (run["status"], run["total_amount"], run["charge_count"]) == (
        "approval_pending",
        "1000.00",
        1,
    )
    assert await w.balance() == Decimal("0.00")  # onaylanmadan deftere bir şey yazılmaz
    charges = (await w.get(f"/charge-runs/{run['id']}/charges")).json()
    assert charges["items"][0]["id"] is None
    assert charges["items"][0]["amount"] == "1000.00"
    second = await post_run(w, "2026-06-01")
    assert second.json()["error"]["code"] == "draft_exists"
    await approve(w, other, run["approval_request_id"])
    after = (await w.get(f"/charge-runs/{run['id']}")).json()
    assert after["status"] == "posted"  # tahakkuk tarihi geçmiş: onayda kesinleşti
    assert await w.balance() == Decimal("1000.00")


async def test_fin05_taslak_donem_basi_5_gun_once_ve_kesinlestirme_isi(
    world: World, other: dict[str, str]
) -> None:
    """Gece işi: kesim 1 Temmuz, taslak 26 Haziran'da hazırlanıp onaya gider; onaylanınca
    1 Temmuz'da kesinleşir (FIN-05 S2: iş tekrar çalışınca yeni satır oluşmaz)."""
    w = world
    clock = [datetime(2026, 6, 20, 9, tzinfo=UTC)]
    w.app.dependency_overrides[get_now] = lambda: clock[0]
    await finalized_plan(w)
    await rule(w, "charge_run")
    schedule = await w.api.put(
        w.url("/charge-schedule"),
        json={"enabled": True, "charge_day": 1, "draft_lead_days": 5, "notify_on_run": True},
        headers=w.headers,
    )
    assert schedule.status_code == 200, schedule.text
    assert schedule.json()["data"]["approval_required"] is True

    async def run(day: date) -> list[str]:
        w.set_today(day)
        now = datetime(day.year, day.month, day.day, 3, tzinfo=UTC)
        clock[0] = now
        results = await charge_schedule.run_all(w.factory, today=day, now=now)
        return [r.outcome.status.value for r in results if r.site_id == w.site_id]

    assert await run(date(2026, 6, 25)) == []
    assert await run(date(2026, 6, 26)) == ["drafted"]
    assert await run(date(2026, 6, 27)) == []  # ikinci çalışma taslak açmaz
    runs = (await w.get("/charge-runs")).json()["items"]
    draft = next(r for r in runs if r["period"] == "07/2026")
    assert draft["status"] == "approval_pending"
    await approve(w, other, draft["approval_request_id"])
    assert (await w.get(f"/charge-runs/{draft['id']}")).json()["status"] == "approved"
    assert await w.balance() == Decimal("0.00")
    assert "posted" in await run(date(2026, 7, 1))
    assert (await w.get(f"/charge-runs/{draft['id']}")).json()["status"] == "posted"
    assert await w.balance() == Decimal("1000.00")
    await run(date(2026, 7, 2))
    assert await w.balance() == Decimal("1000.00")  # tekrar güvenli


async def test_taslak_yenile_reddet_iptal(world: World, other: dict[str, str]) -> None:
    w = world
    await finalized_plan(w)
    draft = await w.post("/charge-runs/drafts", {"charge_date": "2026-06-01"})
    assert draft.status_code == 201, draft.text
    run = draft.json()["data"]
    assert run["status"] == "draft"
    diff = await w.get(f"/charge-runs/{run['id']}/diff")
    assert diff.status_code == 200
    summary = (await w.get(f"/charge-runs/{run['id']}/summary")).json()
    assert summary["item_totals"][0]["distributed"] == "1000.00"
    assert (await w.post(f"/charge-runs/{run['id']}/refresh")).status_code == 200
    await rule(w, "charge_run")
    sent = await w.post(f"/charge-runs/{run['id']}/submit")
    assert sent.status_code == 202, sent.text
    request_id = sent.json()["data"]["approval_request_id"]
    cancel_pending = await w.post(f"/charge-runs/{run['id']}/cancel")
    assert cancel_pending.status_code == 409
    rejected = await w.api.post(
        w.url(f"/approvals/{request_id}/reject"), json={"comment": "Tutar hatalı"}, headers=other
    )
    assert rejected.status_code == 200, rejected.text
    assert (await w.get(f"/charge-runs/{run['id']}")).json()["status"] == "rejected"
    assert (await w.post(f"/charge-runs/{run['id']}/cancel")).status_code == 200
    assert (await w.get(f"/charge-runs/{run['id']}")).json()["status"] == "cancelled"
    # iptalden sonra dönem yeniden taslağa açılabilir
    assert (await w.post("/charge-runs/drafts", {"charge_date": "2026-06-01"})).status_code == 201


# --- K2 gecikme tazminatı -----------------------------------------------------------------


async def test_k2_gecikme_varsayilan_kapali(world: World) -> None:
    w = world
    await finalized_plan(w)
    await post_run(w, "2026-06-01")
    w.set_today(date(2026, 8, 1))
    await finance_jobs.run_all(w.factory, now=datetime(2026, 8, 1, 3, tzinfo=UTC), today=w.today[0])
    assert await entries(w, "late_fee") == []
    assert (await pay(w, "100.00", "2026-08-01")).status_code == 201
    assert await entries(w, "late_fee") == []


async def test_k2_gecikme_acikken_ay_sonu_ve_odeme_aninda(world: World) -> None:
    """Vade 10 Temmuz, tolerans 5: 31 Temmuz'a kadar 16 gün → 1000 × 0,05/30 × 16 = 26,67;
    10 Ağustos'a kadar toplam 26 gün → 43,33, yazılmış 26,67 düşülür → 16,66; tahsilat önce
    gecikmeyi kapatır (K24 varsayılanı, TBK m.100)."""
    w = world
    await finalized_plan(w)
    await params(w, "2026-07", late_fee_enabled=True)
    assert (await post_run(w, "2026-07-01")).status_code == 201
    day = date(2026, 8, 1)
    w.set_today(day)
    now = datetime(2026, 8, 1, 3, tzinfo=UTC)
    await finance_jobs.run_all(w.factory, now=now, today=day)
    [month_end] = await entries(w, "late_fee")
    assert (month_end.debit, month_end.date, month_end.due_date) == (
        Decimal("26.67"),
        date(2026, 7, 31),
        date(2026, 7, 10),
    )
    await finance_jobs.run_all(w.factory, now=now, today=day)  # aynı gün ikinci çalışma (S3)
    assert len(await entries(w, "late_fee")) == 1
    w.set_today(date(2026, 8, 10))
    paid = await pay(w, "1000.00", "2026-08-10")
    assert paid.status_code == 201, paid.text
    fees = await entries(w, "late_fee")
    assert [f.debit for f in fees] == [Decimal("26.67"), Decimal("16.66")]
    for fee in fees:
        assert await allocated_to(w, fee.id) == fee.debit  # önce gecikme (TBK m.100)
    assert await w.balance() == Decimal("43.33")
    with site_scope(w.site_id):
        async with w.factory() as session:
            accruals = await session.scalar(select(func.count()).select_from(LateFeeAccrual))
    assert accruals == 2


async def test_k2_odeme_plani_suresince_gecikme_islemez(world: World) -> None:
    w = world
    await finalized_plan(w)
    await params(w, "2026-07", late_fee_enabled=True)
    await post_run(w, "2026-07-01")
    w.set_today(date(2026, 7, 20))
    plan = await w.post(
        f"/accounts/{w.account_id}/payment-plans",
        {"amount": "1000.00", "installment_count": 2, "first_due_date": "2026-08-15"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert plan.status_code in (201, 202), plan.text
    w.set_today(date(2026, 8, 1))
    await finance_jobs.run_all(
        w.factory, now=datetime(2026, 8, 1, 3, tzinfo=UTC), today=date(2026, 8, 1)
    )
    fees = await entries(w, "late_fee")
    # 10 Temmuz vadesi, plan 20 Temmuz'da kapattı: yalnız 16–20 Temmuz (5 gün) = 8,33
    assert [f.debit for f in fees] == [Decimal("8.33")]


# --- D-10 satır düzeltmesi ------------------------------------------------------------------


async def first_line(w: World, run_id: str) -> dict[str, Any]:
    charges = (await w.get(f"/charge-runs/{run_id}/charges")).json()
    line: dict[str, Any] = charges["items"][0]["lines"][0]
    return line


async def test_d10_satir_duzeltmesi_ayni_doneme(world: World) -> None:
    w = world
    await finalized_plan(w)
    run = (await post_run(w, "2026-06-01")).json()["data"]["id"]
    line = await first_line(w, run)
    short = await w.post(
        f"/charge-runs/lines/{line['id']}/adjustments", {"new_amount": "800.00", "reason": ""}
    )
    assert short.status_code == 422
    adjusted = await w.post(
        f"/charge-runs/lines/{line['id']}/adjustments",
        {"new_amount": "800.00", "reason": "Muafiyet unutulmuş"},
    )
    assert adjusted.status_code == 201, adjusted.text
    data = adjusted.json()["data"]
    assert (data["status"], data["old_amount"], data["prior_period"]) == (
        "applied",
        "1000.00",
        False,
    )
    assert await w.balance() == Decimal("800.00")
    charges = (await w.get(f"/charge-runs/{run}/charges")).json()["items"][0]
    assert (charges["amount"], charges["current_amount"]) == ("1000.00", "800.00")
    assert charges["lines"][0]["current_amount"] == "800.00"
    charge_entries = await entries(w, "charge")
    assert [e.date for e in charge_entries] == [date(2026, 6, 1)] * 3  # aynı dönem
    # orijinal koşu ters kaydedilebilir: güncel borç (800) ters kaydedilir
    reversed_ = await w.post(f"/charge-runs/{run}/reverse", {"reason": "Yeniden kesilecek"})
    assert reversed_.status_code == 200, reversed_.text
    assert await w.balance() == Decimal("0.00")
    assert len((await w.get(f"/charge-runs/{run}/adjustments")).json()) == 1


async def test_d10_kilitli_donemde_onceki_doneme_ait_ve_onay(
    world: World, other: dict[str, str]
) -> None:
    """FIN-05: orijinal dönem kilitli → ters ve yeni kayıt açık döneme "önceki döneme ait"."""
    w = world
    await finalized_plan(w)
    run = (await post_run(w, "2026-05-01")).json()["data"]["id"]
    await pay(w, "1000.00")
    await set_period(w, 2026, 5, "locked")
    line = await first_line(w, run)
    await rule(w, "charge_adjustment")
    sent = await w.post(
        f"/charge-runs/lines/{line['id']}/adjustments",
        {"new_amount": "700.00", "reason": "Arsa payı hatası"},
    )
    assert sent.status_code == 202, sent.text
    assert await w.balance() == Decimal("0.00")
    await approve(w, other, sent.json()["data"]["approval_request_id"])
    [adjustment] = (await w.get(f"/charge-runs/{run}/adjustments")).json()
    assert (adjustment["status"], adjustment["prior_period"]) == ("applied", True)
    applied_on = date.fromisoformat(adjustment["applied_on"])  # onay günü (açık dönem)
    assert applied_on > date(2026, 5, 31)
    new = [e for e in await entries(w, "charge") if e.date == applied_on]
    assert len(new) == 2
    assert all("önceki döneme ait" in e.description for e in new)
    assert await w.balance() == Decimal("-300.00")  # ödenen 1.000; doğru borç 700 → 300 avans
    new_debt = next(e for e in new if e.debit > 0)
    assert await allocated_to(w, new_debt.id) == Decimal("700.00")


# --- B17 bütçe–gerçekleşme ------------------------------------------------------------------


async def expense(w: World, amount: str, day: str) -> None:
    form = {
        "expense_category_id": w.lookups["category"],
        "description": "Bahçe bakımı",
        "amount": amount,
        "date": day,
    }
    response = await w.api.post(w.url("/expenses"), data=form, headers=w.headers)
    assert response.status_code == 201, response.text


async def test_b17_butce_gerceklesme_ve_esik_uyarisi(world: World) -> None:
    w = world
    plan = await finalized_plan(w)
    created = await w.post(
        "/budgets",
        {
            "fiscal_year": 2026,
            "name": "2026 bütçesi",
            "budget_plan_id": plan,
            "income_target": "12000",
        },
    )
    assert created.status_code == 201, created.text
    budget = created.json()["data"]
    [line] = budget["lines"]
    assert line["months"] == ["1000.00"] * 12
    assert budget["total"] == "12000.00"
    submitted = await w.post(f"/budgets/{budget['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "active"
    await expense(w, "5000.00", "2026-05-10")
    await expense(w, "5900.00", "2026-06-05")
    actuals = (await w.get(f"/budgets/{budget['id']}/actuals", params={"month": 6})).json()
    [row] = actuals["rows"]
    assert (row["cumulative_budget"], row["cumulative_actual"], row["annual_budget"]) == (
        "6000.00",
        "10900.00",
        "12000.00",
    )
    assert (row["usage_percent"], row["level"], row["remaining"]) == ("90.8", "warn", "1100.00")

    async def notifications() -> int:
        with site_scope(w.site_id):
            async with w.factory() as session:
                count = await session.scalar(
                    select(func.count()).where(Notification.kind == "budget.threshold")
                )
        return count or 0

    now = datetime(2026, 6, 20, 3, tzinfo=UTC)
    await finance_jobs.run_all(w.factory, now=now, today=w.today[0])
    first = await notifications()
    assert first >= 1
    await finance_jobs.run_all(w.factory, now=now, today=w.today[0])
    assert await notifications() == first  # eşik başına bir kez
    await expense(w, "1200.00", "2026-06-10")
    await finance_jobs.run_all(w.factory, now=now, today=w.today[0])
    assert await notifications() > first  # %100
    # revizyon yeni sürüm; aidat kuralı deneme hesabı gelir hedefini gösterir
    revised = await w.post(f"/budgets/{budget['id']}/revise")
    assert revised.status_code == 201
    assert revised.json()["data"]["version"] == 2
    trial = await w.post(
        "/billing-rules/trial", {**rule_body(w), "version": rule_body(w)["version"]}
    )
    assert trial.json()["budget_income_target"] == "12000.00"


async def test_d16_butce_onayi_ve_isletme_projesi_kesinlestirme(
    world: World, other: dict[str, str]
) -> None:
    w = world
    await rule(w, "budget")
    plan = (await w.post("/budget-plans", {"fiscal_year": 2026, "name": "2026 Projesi"})).json()
    plan_id = plan["data"]["id"]
    await w.post(f"/budget-plans/{plan_id}/items", item_body(w))
    await w.post(f"/budget-plans/{plan_id}/notify", {"notified_on": "2026-01-02"})
    sent = await w.post(f"/budget-plans/{plan_id}/finalize")
    assert sent.status_code == 202, sent.text
    request_id = sent.json()["data"]["finalize_request_id"]
    again = await w.post(f"/budget-plans/{plan_id}/finalize")
    assert again.json()["error"]["code"] == "finalize_pending"
    await approve(w, other, request_id)
    assert (await w.get(f"/budget-plans/{plan_id}")).json()["status"] == "finalized"
    budget = (await w.post("/budgets", {"fiscal_year": 2026, "name": "Bütçe"})).json()["data"]
    empty = await w.post(f"/budgets/{budget['id']}/submit")
    assert empty.json()["error"]["code"] == "budget_empty"
    put = await w.api.put(
        w.url(f"/budgets/{budget['id']}"),
        json={"lines": [{"expense_category_id": w.lookups["category"], "months": ["100"] * 12}]},
        headers=w.headers,
    )
    assert put.status_code == 200, put.text
    pending = await w.post(f"/budgets/{budget['id']}/submit")
    assert pending.status_code == 202
    await approve(w, other, pending.json()["data"]["approval_request_id"])
    assert (await w.get(f"/budgets/{budget['id']}")).json()["status"] == "active"
    foreign = await w.api.get(
        w.url(f"/budgets/{budget['id']}", slug="yildiz-sitesi"), headers=w.headers
    )
    assert foreign.status_code == 404
