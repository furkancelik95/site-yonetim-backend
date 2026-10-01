"""Site panosu ve modül yönetimi — docs/06 §2.4, §2.13 (gerçek PostgreSQL).

Trello "BE · Dilim 7" — bitti ölçütü: pano tek istekte özet döner, modül aç/kapa uçtan yapılır.
Kurgu `finance_world` (tek daire, aylık 1.000, bugün 20.06.2026).
"""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Charge, ChargeRun, Payment, Plan, Site, SiteFinanceSummary
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


@pytest.fixture
async def charged(world: World) -> World:
    await finalized_plan(world)
    await post_run(world)  # Haziran 1.000, vade 15.06
    return world


async def pay(w: World, amount: str, day: str = "2026-06-18") -> None:
    response = await w.post(
        "/payments",
        {"ledger_account_id": w.account_id, "amount": amount, "date": day, "method": "cash"},
    )
    assert response.status_code == 201, response.text


async def test_pano_tek_istekte_ozet_doner(charged: World) -> None:
    await pay(charged, "400.00")
    body = (await charged.get("/dashboard")).json()
    assert body["finance"] == {
        "total_charged": "1000.00",
        "total_collected": "400.00",
        "month_charged": "1000.00",
        "month_collected": "400.00",
        "open_balance": "600.00",
        "debtor_count": 1,
        "over_30_days": "0.00",
        "over_60_days": "0.00",
        "average_debt": "600.00",
    }
    [debtor] = body["top_debtors"]
    assert (debtor["unit_name"], debtor["balance"], debtor["overdue_days"]) == ("A-1", "600.00", 5)
    assert debtor["person_name"] == "Ayşe YILMAZ"
    assert body["cash_balance"] == "0.00"
    assert body["expenses"] == []
    # plansız sitede yalnız finans açık: talep/duyuru bölümü yok
    assert (body["requests"], body["announcements"]) == (None, None)


async def test_reversal_and_new_month_update_summary(charged: World) -> None:
    run_id = (await charged.get("/charge-runs")).json()["items"][0]["id"]
    await charged.post(f"/charge-runs/{run_id}/reverse", {"reason": "Hata"})
    assert (await charged.get("/dashboard")).json()["finance"]["total_charged"] == "0.00"
    await post_run(charged, "2026-06-01")
    await post_run(charged, "2026-05-01")
    await pay(charged, "250.00", "2026-05-20")
    finance = (await charged.get("/dashboard")).json()["finance"]
    assert (finance["total_charged"], finance["month_charged"]) == ("2000.00", "1000.00")
    assert (finance["total_collected"], finance["month_collected"]) == ("250.00", "0.00")


async def test_summary_matches_ledger(charged: World) -> None:
    await pay(charged, "300.00")
    await post_run(charged, "2026-07-01")
    with site_scope(charged.site_id):
        async with charged.factory() as session:
            summary = (
                await session.execute(
                    select(
                        func.sum(SiteFinanceSummary.charged), func.sum(SiteFinanceSummary.collected)
                    )
                )
            ).one()
            charges = await session.scalar(
                select(func.sum(Charge.amount))
                .join(ChargeRun, ChargeRun.id == Charge.charge_run_id)
                .where(ChargeRun.status == "posted", ChargeRun.reversal_of_run_id.is_(None))
            )
            payments = await session.scalar(select(func.sum(Payment.amount)))
    assert summary == (charges, payments) == (Decimal("2000.00"), Decimal("300.00"))


async def test_sections_follow_permissions(charged: World) -> None:
    await pay(charged, "100.00")
    auditor = await create_user(charged.factory, "denetci@test.local")
    await add_site_membership(charged.factory, charged.site_id, auditor.id, "Denetçi")
    headers = await login_headers(charged.api, "denetci@test.local")
    body = (await charged.api.get(charged.url("/dashboard"), headers=headers)).json()
    assert body["top_debtors"][0]["person_name"] is None  # kişisel veri yok
    assert body["expenses"] == []  # expenses.read var
    assert body["cash_balance"] == "0.00"  # finance.cash.read var
    tech = await create_user(charged.factory, "teknik@test.local")
    await add_site_membership(charged.factory, charged.site_id, tech.id, "Teknik Personel")
    tech_headers = await login_headers(charged.api, "teknik@test.local")
    assert (
        await charged.api.get(charged.url("/dashboard"), headers=tech_headers)
    ).status_code == 403


async def test_other_site_dashboard_is_empty(charged: World) -> None:
    body = (
        await charged.api.get(charged.url("/dashboard", "yildiz-sitesi"), headers=charged.headers)
    ).json()
    assert (body["finance"]["total_charged"], body["top_debtors"]) == ("0.00", [])


# --- modüller ---------------------------------------------------------------------------


async def _give_plan(w: World, modules: list[str]) -> None:
    async with w.factory() as session, session.begin():
        plan = Plan(name="Standart", max_units=150, allowed_modules=modules)
        session.add(plan)
        await session.flush()
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.plan_id = plan.id


async def test_modul_ac_kapa_uctan_yapilir(world: World) -> None:
    listed = {m["key"]: m for m in (await world.get("/modules")).json()}
    assert listed["finance"] == {
        "key": "finance", "enabled": True, "in_plan": True, "available": True, "is_core": True
    }  # fmt: skip
    assert listed["requests"]["in_plan"] is False

    not_in_plan = await world.post("/modules/requests/toggle")
    assert not_in_plan.status_code == 409
    assert not_in_plan.json()["error"]["code"] == "module_not_in_plan"

    await _give_plan(world, ["finance", "requests", "announcements"])
    opened = await world.post("/modules/requests/toggle")
    assert opened.status_code == 200, opened.text
    assert opened.json()["data"]["available"] is True
    assert opened.json()["message"] == "'requests' modülü açıldı."
    assert (await world.get("/dashboard")).json()["requests"]["total"] == 0
    assert (await world.get("/requests")).status_code == 200

    closed = await world.post("/modules/requests/toggle")
    assert closed.json()["message"] == "'requests' modülü kapatıldı (veriler silinmedi)."
    assert (await world.get("/requests")).status_code == 404

    core = await world.post("/modules/finance/toggle")
    assert core.json()["error"]["code"] == "core_module_cannot_be_disabled"
    assert (await world.post("/modules/bilinmeyen/toggle")).status_code == 404


async def test_module_permissions(world: World) -> None:
    accountant = await create_user(world.factory, "muhasebe@test.local")
    await add_site_membership(world.factory, world.site_id, accountant.id, "Muhasebe")
    headers = await login_headers(world.api, "muhasebe@test.local")
    assert (await world.api.get(world.url("/modules"), headers=headers)).status_code == 403
    assert (
        await world.api.post(world.url("/modules/requests/toggle"), headers=headers)
    ).status_code == 403
