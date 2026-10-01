"""Portföy ve platform özeti — docs/06 §2.2–2.3, docs/05 §5, docs/07 §8.6 (gerçek PostgreSQL).

Kurgu `finance_world`: yönetici iki sitede (Aksu, Yıldız) üye; Aksu'da A-1, aylık 1.000.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Organization, Plan, Request, Site
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def test_portfoy_site_basina_ozet(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    await world.post(
        "/payments",
        {
            "ledger_account_id": world.account_id,
            "amount": "400.00",
            "date": "2026-06-18",
            "method": "cash",
        },
    )
    body = (await world.api.get("/api/v1/portfolio", headers=world.headers)).json()
    aksu, yildiz = body["sites"]
    assert (aksu["name"], aksu["units"], aksu["role"]) == ("Aksu Konakları", 1, "Yönetici")
    assert aksu["finance"] == {
        "charged": "1000.00",
        "collected": "400.00",
        "collection_rate": "40.00",
        "open_balance": "600.00",
        "debtor_count": 1,
    }
    assert aksu["requests"] == {"open": 0, "overdue": 0}
    assert (yildiz["units"], yildiz["finance"]["charged"]) == (0, "0.00")
    assert (body["total_units"], body["total_charged"], body["total_collected"]) == (
        1,
        "1000.00",
        "400.00",
    )
    assert (body["collection_rate"], body["total_open_balance"]) == ("40.00", "600.00")


async def test_fields_follow_each_sites_permissions(world: World) -> None:
    user = await create_user(world.factory, "karma@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Denetçi")
    await add_site_membership(world.factory, world.other_site_id, user.id, "Teknik Personel")
    headers = await login_headers(world.api, "karma@test.local")
    aksu, yildiz = (await world.api.get("/api/v1/portfolio", headers=headers)).json()["sites"]
    assert aksu["finance"] is not None and aksu["requests"] is None  # noqa: PT018 - Denetçi
    assert yildiz["finance"] is None and yildiz["requests"] is not None  # noqa: PT018 - Teknik


async def test_overdue_requests_are_counted(world: World) -> None:
    created = await world.post("/requests", {"title": "Asansör arızası"})
    if created.status_code == 404:  # plansız sitede talep modülü kapalı: kaydı doğrudan aç
        async with world.factory() as session, session.begin():
            plan = Plan(name="Standart", max_units=150, allowed_modules=["finance", "requests"])
            session.add(plan)
            await session.flush()
            site = await session.get(Site, world.site_id)
            assert site is not None
            site.plan_id = plan.id
        await world.post("/modules/requests/toggle")
        created = await world.post("/requests", {"title": "Asansör arızası"})
    assert created.status_code == 201, created.text
    await world.post("/requests", {"title": "Bahçe sulama"})
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            await session.execute(
                update(Request)
                .where(Request.title == "Asansör arızası")
                .values(due_at=datetime.now(UTC) - timedelta(days=1))
            )
    aksu = (await world.api.get("/api/v1/portfolio", headers=world.headers)).json()["sites"][0]
    assert aksu["requests"] == {"open": 2, "overdue": 1}


async def test_single_site_user_has_no_portfolio(world: World) -> None:
    user = await create_user(world.factory, "tek@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Yönetici")
    headers = await login_headers(world.api, "tek@test.local")
    assert (await world.api.get("/api/v1/portfolio", headers=headers)).status_code == 404


# --- platform özeti -----------------------------------------------------------------


async def test_platform_overview(world: World) -> None:
    async with world.factory() as session, session.begin():
        small = Plan(name="Mini", max_units=0, allowed_modules=["finance"])
        session.add(small)
        await session.flush()
        organization = Organization(name="Kent Yönetim A.Ş.", plan_id=small.id)
        session.add(organization)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.organization_id, site.plan_id = organization.id, small.id
    admin = await create_user(world.factory, "platform@test.local", is_platform_admin=True)
    headers = await login_headers(world.api, "platform@test.local")
    assert admin.is_platform_admin

    body = (await world.api.get("/api/v1/platform/overview", headers=headers)).json()
    assert (body["customer_count"], body["site_count"], body["unit_count"]) == (1, 2, 1)
    [over] = body["over_cap_sites"]
    assert (over["name"], over["units"], over["max_units"], over["over_cap"]) == (
        "Aksu Konakları",
        1,
        0,
        True,
    )
    [customer] = body["customers"]["items"]
    assert (customer["name"], customer["site_count"], customer["units"], customer["plan_name"]) == (
        "Kent Yönetim A.Ş.", 1, 1, "Mini"
    )  # fmt: skip
    assert [s["name"] for s in body["sites"]["items"]] == ["Aksu Konakları", "Yıldız Sitesi"]
    # kullanım ölçüsü dışında veri yok: borç/sakin alanı bulunmaz
    assert "balance" not in str(body) and "person" not in str(body)  # noqa: PT018


async def test_8_6_platform_yoneticisi_olmayan_overview_404(world: World) -> None:
    assert (
        await world.api.get("/api/v1/platform/overview", headers=world.headers)
    ).status_code == 404
    platform = await create_user(world.factory, "p2@test.local", is_platform_admin=True)
    headers = await login_headers(world.api, "p2@test.local")
    assert platform.id != uuid.UUID(int=0)
    assert (await world.api.get("/api/v1/portfolio", headers=headers)).status_code == 404
