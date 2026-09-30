"""İşletme projesi ve tahakkuk — docs/07 §3.1–3.4, 3.10 (gerçek PostgreSQL, RLS'e tabi rol).

Kurgu (07 §3): tek site, tek daire, tek kişi, bir oturan hesabı; kesinleşmiş projede tek kalem
12.000/yıl aylık eşit → aylık 1.000.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AccountBalance, ChargeRun, LedgerAccount, LedgerEntry
from site_yonetim.services import ledger
from site_yonetim.services.finance_setup import ensure_finance_setup
from tests.integration.conftest import add_site_membership, create_user, login_headers
from tests.integration.finance_world import World, finalized_plan, item_body, post_run

# --- 07 §3 --------------------------------------------------------------------------


async def test_3_1_tahakkuk_kaydedilince_borc_defterde_olusur(world: World) -> None:
    await finalized_plan(world)
    response = await post_run(world)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["data"]["charge_count"] == 1
    assert body["data"]["total_amount"] == "1000.00"
    assert body["data"]["period"] == "06/2026"
    assert body["data"]["posted_by"] == "Kerem YILDIRIM"
    assert body["message"] == "06/2026 tahakkuku kesildi: 1 bölüm, 1.000,00 TL."
    assert await world.balance() == Decimal("1000.00")

    with site_scope(world.site_id):
        async with world.factory() as session:
            entry = await session.scalar(select(LedgerEntry))
            summary = await session.scalar(select(AccountBalance))
    assert entry is not None
    assert (entry.debit, entry.due_date) == (Decimal("1000.00"), date(2026, 6, 15))
    assert entry.description == "06/2026 tahakkuku — Aidat"
    assert summary is not None
    assert summary.oldest_open_due_date == date(2026, 6, 15)


async def test_3_2_ayni_donem_ikinci_kez_kesilemez(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    again = await post_run(world)
    assert again.status_code == 409
    assert "zaten kesilmiş" in again.json()["error"]["message"]
    assert again.json()["error"]["code"] == "period_already_charged"
    assert await world.balance() == Decimal("1000.00")


async def test_3_3_ters_kayit_bakiyeyi_geri_alir(world: World) -> None:
    await finalized_plan(world)
    run_id = (await post_run(world)).json()["data"]["id"]
    reversed_ = await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Yanlış tutar"})
    assert reversed_.status_code == 200, reversed_.text
    data = reversed_.json()["data"]
    assert data["reversal_of_run_id"] == run_id
    assert data["reason"] == "Yanlış tutar"
    assert await world.balance() == Decimal("0.00")
    original = (await world.get(f"/charge-runs/{run_id}")).json()
    assert original["status"] == "reversed"  # orijinal yerinde, durumu değişti

    with site_scope(world.site_id):
        async with world.factory() as session:
            entries = (await session.scalars(select(LedgerEntry).order_by(LedgerEntry.id))).all()
    assert [(e.debit, e.credit) for e in entries] == [
        (Decimal("1000.00"), Decimal("0.00")),
        (Decimal("0.00"), Decimal("1000.00")),
    ]
    assert entries[1].reversal_of_entry_id == entries[0].id
    assert entries[1].description == "TERS KAYIT — 06/2026 tahakkuku iptali"


async def test_3_4_ayni_kosu_iki_kez_ters_kaydedilemez(world: World) -> None:
    await finalized_plan(world)
    run_id = (await post_run(world)).json()["data"]["id"]
    await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Yanlış tutar"})
    again = await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Tekrar"})
    assert again.status_code == 409
    assert again.json()["error"]["message"] == "Bu koşu zaten ters kayıtla iptal edilmiş."
    assert await world.balance() == Decimal("0.00")


async def test_reversal_run_itself_cannot_be_reversed(world: World) -> None:
    await finalized_plan(world)
    run_id = (await post_run(world)).json()["data"]["id"]
    reversal = (await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Hata"})).json()
    again = await world.post(f"/charge-runs/{reversal['data']['id']}/reverse", {"reason": "Hata"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "run_not_reversible"


async def test_3_10_ters_kayittan_sonra_siradaki_donem_ayni_ay(world: World) -> None:
    world.set_today(date(2026, 9, 10))
    await finalized_plan(world)
    run_id = (await post_run(world, "2026-09-01")).json()["data"]["id"]
    world.set_today(date(2026, 9, 30))
    await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Yanlış kalem"})

    preview = (await world.get("/charge-runs/preview")).json()
    assert preview["period"] == "09/2026"
    assert preview["charge_date"] == "2026-09-01"
    assert preview["due_date"] == "2026-09-15"
    assert preview["already_charged"] is False
    assert (await post_run(world, "2026-09-01")).status_code == 201  # yeniden kesilebilir


async def test_next_period_follows_last_valid_run(world: World) -> None:
    await finalized_plan(world)
    await post_run(world, "2026-06-01")
    preview = (await world.get("/charge-runs/preview")).json()
    assert (preview["period"], preview["charge_date"]) == ("07/2026", "2026-07-01")


# --- önizleme ---------------------------------------------------------------------


async def test_preview_writes_nothing(world: World) -> None:
    await finalized_plan(world)
    preview = await world.get("/charge-runs/preview", params={"charge_date": "2026-06-01"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["total_amount"], body["unit_count"], body["charge_count"]) == ("1000.00", 1, 1)
    assert body["item_totals"] == [
        {
            "budget_item_id": body["item_totals"][0]["budget_item_id"],
            "name": "Aidat",
            "amount": "1000.00",
            "distributed": "1000.00",
        }
    ]
    charge = body["charges"]["items"][0]
    assert charge["unit_name"] == "A-1"
    assert charge["reference_code"] == "A1-O"
    assert charge["person_name"] == "Ayşe YILMAZ"
    assert charge["lines"][0]["explanation"] == "1.000,00 TL, 1 bağımsız bölüme eşit paylaştırıldı"
    assert (await world.get("/charge-runs")).json()["total"] == 0
    assert await world.balance() == Decimal("0.00")


async def test_without_finalized_plan(world: World) -> None:
    preview = await world.get("/charge-runs/preview")
    assert preview.status_code == 409
    assert preview.json()["error"]["message"] == (
        "Tahakkuk kesmek için önce işletme projesi kesinleşmeli."
    )


async def test_nothing_to_charge(world: World) -> None:
    await finalized_plan(world, item_body(world, frequency="quarterly"))
    # Mayıs üç aylık dönem değil → kesilecek kalem yok
    response = await post_run(world, "2026-05-01")
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "Kesilecek tahakkuk yok."


async def test_frequency_calendar(world: World) -> None:
    await finalized_plan(
        world,
        item_body(world),
        item_body(world, name="Çatı onarımı", annual_amount="6000.00", frequency="one_time",
                  charge_type_id=world.lookups["demirbas"]),
        item_body(world, name="Sigorta", annual_amount="4000.00", frequency="quarterly"),
    )  # fmt: skip
    july = (await world.get("/charge-runs/preview", params={"charge_date": "2026-07-01"})).json()
    assert {t["name"] for t in july["item_totals"]} == {"Aidat", "Çatı onarımı", "Sigorta"}
    assert july["total_amount"] == "8000.00"  # 1.000 + 6.000 + 1.000 (4.000/4)
    run_id = (await post_run(world, "2026-07-01")).json()["data"]["id"]

    august = (await world.get("/charge-runs/preview", params={"charge_date": "2026-08-01"})).json()
    assert [t["name"] for t in august["item_totals"]] == ["Aidat"]
    assert sorted(august["not_due_items"]) == ["Sigorta", "Çatı onarımı"]

    # Ters kaydedilen koşudaki tek seferlik kalem yeniden kesilebilir
    await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Hata"})
    again = (await world.get("/charge-runs/preview", params={"charge_date": "2026-08-01"})).json()
    assert "Çatı onarımı" in {t["name"] for t in again["item_totals"]}


async def test_owner_item_goes_to_owner_account(world: World) -> None:
    await finalized_plan(
        world, item_body(world, name="Demirbaş", charge_type_id=world.lookups["demirbas"])
    )
    run_id = (await post_run(world)).json()["data"]["id"]
    charges = (await world.get(f"/charge-runs/{run_id}/charges")).json()
    assert charges["total"] == 1
    [charge] = charges["items"]
    assert (charge["account_kind"], charge["reference_code"]) == ("owner", "A1-M")
    assert charge["lines"][0]["allocation_kind"] == "equal"


async def test_due_date_before_charge_date_is_rejected(world: World) -> None:
    await finalized_plan(world)
    response = await world.post(
        "/charge-runs", {"charge_date": "2026-06-10", "due_date": "2026-06-01"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {
        "due_date": "Vade tarihi tahakkuk tarihinden önce olamaz."
    }


# --- Idempotency-Key ------------------------------------------------------------------


async def test_same_idempotency_key_returns_first_response(world: World) -> None:
    await finalized_plan(world)
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await post_run(world, headers=key)
    second = await post_run(world, headers=key)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert (await world.get("/charge-runs")).json()["total"] == 1
    assert await world.balance() == Decimal("1000.00")


async def test_idempotency_key_reuse_with_other_body_is_rejected(world: World) -> None:
    await finalized_plan(world)
    key = {"Idempotency-Key": "anahtar-12345"}
    await post_run(world, "2026-06-01", headers=key)
    other = await post_run(world, "2026-07-01", headers=key)
    assert other.status_code == 422
    assert other.json()["error"]["code"] == "idempotency_key_reused"


async def test_expired_idempotency_key_is_forgotten(world: World, admin_engine: Any) -> None:
    await finalized_plan(world)
    key = {"Idempotency-Key": "anahtar-eskimis-1"}
    run_id = (await post_run(world, headers=key)).json()["data"]["id"]
    async with admin_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.all_sites', 'on', true)"))
        await connection.execute(
            text("UPDATE idempotency_keys SET created_at = created_at - interval '25 hours'")
        )
    await world.post(f"/charge-runs/{run_id}/reverse", {"reason": "Hata"})
    again = await post_run(world, headers=key)  # 24 saat geçti: yeni istek sayılır
    assert again.status_code == 201
    assert again.json()["data"]["id"] != run_id


async def test_finance_setup_is_idempotent(world: World) -> None:
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            await ensure_finance_setup(session)
    assert len((await world.get("/charge-types")).json()) == 2
    assert len((await world.get("/allocation-rules")).json()) == 5
    assert len((await world.get("/expense-categories")).json()) == 2


async def test_invalid_idempotency_key(world: World) -> None:
    await finalized_plan(world)
    response = await post_run(world, headers={"Idempotency-Key": "kisa"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_idempotency_key"


# --- işletme projesi -------------------------------------------------------------------


async def test_budget_lifecycle(world: World) -> None:
    plan = (await world.post("/budget-plans", {"fiscal_year": 2026, "name": "2026 Projesi"})).json()
    assert plan["data"]["status"] == "draft"
    plan_id = plan["data"]["id"]

    empty = await world.post(f"/budget-plans/{plan_id}/notify", {"notified_on": "2026-06-01"})
    assert empty.status_code == 409
    assert empty.json()["error"]["code"] == "budget_empty"

    added = await world.post(f"/budget-plans/{plan_id}/items", item_body(world))
    assert added.json()["message"] == "'Aidat' kalemi eklendi (12.000,00 TL/yıl)."
    item_id = added.json()["data"]["items"][0]["id"]
    assert added.json()["data"]["items"][0]["period_amount"] == "1000.00"
    updated = await world.api.put(
        world.url(f"/budget-plans/{plan_id}/items/{item_id}"),
        json=item_body(world, annual_amount="24000.00"),
        headers=world.headers,
    )
    assert updated.json()["data"]["total_annual_amount"] == "24000.00"

    future = await world.post(f"/budget-plans/{plan_id}/notify", {"notified_on": "2026-06-21"})
    assert future.status_code == 422
    notified = await world.post(f"/budget-plans/{plan_id}/notify", {"notified_on": "2026-06-15"})
    assert notified.json()["data"]["objection_deadline"] == "2026-06-22"
    assert "22.06.2026" in notified.json()["message"]

    locked = await world.post(f"/budget-plans/{plan_id}/items", item_body(world))
    assert locked.status_code == 409
    assert locked.json()["error"]["code"] == "budget_not_editable"

    early = await world.post(f"/budget-plans/{plan_id}/finalize")
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "objection_period_open"

    world.set_today(date(2026, 6, 23))
    final = await world.post(f"/budget-plans/{plan_id}/finalize")
    assert final.json()["data"]["status"] == "finalized"
    current = await world.get("/budget-plans/current")
    assert current.json()["id"] == plan_id
    removed = await world.api.delete(
        world.url(f"/budget-plans/{plan_id}/items/{item_id}"), headers=world.headers
    )
    assert removed.status_code == 409


async def test_new_finalized_plan_supersedes_old(world: World) -> None:
    old = await finalized_plan(world, year=2026)
    world.set_today(date(2026, 12, 20))
    new = await finalized_plan(world, year=2027)
    plans = (await world.get("/budget-plans")).json()
    assert {p["id"]: p["status"] for p in plans["items"]} == {old: "superseded", new: "finalized"}


async def test_draft_item_can_be_removed_and_scope_is_checked(world: World) -> None:
    plan_id = (await world.post("/budget-plans", {"fiscal_year": 2026, "name": "Taslak"})).json()[
        "data"
    ]["id"]
    foreign = await world.post(
        f"/budget-plans/{plan_id}/items",
        item_body(world, scope_kind="blocks", scope_block_ids=[str(uuid.uuid4())]),
    )
    assert foreign.status_code == 422
    assert foreign.json()["error"]["fields"] == {
        "scope_block_ids": "Seçilen blok bu sitede bulunamadı."
    }
    missing = await world.post(
        f"/budget-plans/{plan_id}/items", item_body(world, scope_kind="usage")
    )
    assert missing.status_code == 422
    bad_rule = await world.post(
        f"/budget-plans/{plan_id}/items", item_body(world, allocation_rule_id=str(uuid.uuid4()))
    )
    assert bad_rule.json()["error"]["fields"] == {"allocation_rule_id": "Seçilen kayıt bulunamadı."}
    ok = await world.post(
        f"/budget-plans/{plan_id}/items",
        item_body(world, scope_kind="blocks", scope_block_ids=[world.lookups["block"]]),
    )
    item_id = ok.json()["data"]["items"][0]["id"]
    removed = await world.api.delete(
        world.url(f"/budget-plans/{plan_id}/items/{item_id}"), headers=world.headers
    )
    assert removed.json()["data"]["items"] == []
    assert (await world.get(f"/budget-plans/{uuid.uuid4()}")).status_code == 404
    assert (await world.get("/budget-plans/current")).status_code == 404


async def test_money_must_be_text(world: World) -> None:
    plan_id = (await world.post("/budget-plans", {"fiscal_year": 2026, "name": "Taslak"})).json()[
        "data"
    ]["id"]
    response = await world.post(
        f"/budget-plans/{plan_id}/items", item_body(world, annual_amount=12000)
    )
    assert response.status_code == 422
    negative = await world.post(
        f"/budget-plans/{plan_id}/items", item_body(world, annual_amount="-1.00")
    )
    assert negative.status_code == 422


async def test_lookups(world: World) -> None:
    rules = (await world.get("/allocation-rules")).json()
    heating = next(r for r in rules if r["kind"] == "composite")
    assert [(c["kind"], c["percent"]) for c in heating["components"]] == [
        ("equal", "70.00"),
        ("by_area", "30.00"),
    ]
    assert heating["note"] == "Tüketim payı sayaç okuması girilene kadar eşit dağıtılır."
    assert {c["kind"] for c in (await world.get("/expense-categories")).json()} == {
        "operating",
        "capital_improvement",
    }


# --- değişmezlik, izolasyon, yetki ------------------------------------------------------


async def test_ledger_rows_cannot_be_updated_or_deleted(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    with site_scope(world.site_id):
        async with world.factory() as session:
            with pytest.raises(DBAPIError, match="değiştirilemez"):
                await session.execute(update(LedgerEntry).values(debit=Decimal(1)))
            await session.rollback()
            for table in ("ledger_entries", "charges", "charge_lines"):
                with pytest.raises(DBAPIError, match="silinemez"):
                    await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - sabit adlar
                await session.rollback()


async def test_rebuild_balances_matches_summary(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            await session.execute(update(AccountBalance).values(balance=Decimal(0)))
            assert await ledger.rebuild_balances(session) == 2  # M ve O hesapları
    assert await world.balance() == Decimal("1000.00")


async def test_other_site_sees_nothing(world: World) -> None:
    plan_id = await finalized_plan(world)
    run_id = (await post_run(world)).json()["data"]["id"]
    other = "yildiz-sitesi"
    assert (
        await world.api.get(world.url(f"/charge-runs/{run_id}", other), headers=world.headers)
    ).status_code == 404
    assert (
        await world.api.get(world.url(f"/budget-plans/{plan_id}", other), headers=world.headers)
    ).status_code == 404
    assert (await world.api.get(world.url("/charge-runs", other), headers=world.headers)).json()[
        "total"
    ] == 0
    reverse = await world.api.post(
        world.url(f"/charge-runs/{run_id}/reverse", other),
        json={"reason": "Sızma"},
        headers=world.headers,
    )
    assert reverse.status_code == 404
    with site_scope(world.other_site_id):
        async with world.factory() as session:
            assert (await session.scalars(select(ChargeRun))).all() == []
            assert (await session.scalars(select(LedgerAccount))).all() == []


async def test_permissions(world: World) -> None:
    await finalized_plan(world)
    auditor = await create_user(world.factory, "denetci@test.local")
    await add_site_membership(world.factory, world.site_id, auditor.id, "Denetçi")
    headers = await login_headers(world.api, "denetci@test.local")

    assert (await world.api.get(world.url("/charge-runs"), headers=headers)).status_code == 200
    assert (
        await world.api.get(world.url("/budget-plans/current"), headers=headers)
    ).status_code == 200
    preview = await world.api.get(world.url("/charge-runs/preview"), headers=headers)
    post = await world.api.post(world.url("/charge-runs"), json={}, headers=headers)
    plan = await world.api.post(
        world.url("/budget-plans"), json={"fiscal_year": 2027, "name": "X"}, headers=headers
    )
    assert [preview.status_code, post.status_code, plan.status_code] == [403, 403, 403]

    outsider = await create_user(world.factory, "yabanci@test.local")
    await add_site_membership(world.factory, world.other_site_id, outsider.id, "Yönetici")
    outsider_headers = await login_headers(world.api, "yabanci@test.local")
    assert (
        await world.api.get(world.url("/charge-runs"), headers=outsider_headers)
    ).status_code == 404
