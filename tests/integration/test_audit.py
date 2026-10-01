"""Denetim kaydı — docs/09 §6 (gerçek PostgreSQL, RLS'e tabi rol).

Bitti ölçütü: iş olayları (tahsilat, gider, ayar, modül, aktarım, indirme) kim/ne zaman/ne
değişti bilgisiyle otomatik yazılır; kayıt değiştirilemez; yalnız `audit.read` görür; başka
sitenin kaydı görünmez.
"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.api.deps import BUSINESS_TZ
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.api.v1.imports import get_import_store
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, Plan, Site
from site_yonetim.services.files import FileStore
from site_yonetim.services.imports import ImportStore
from tests.integration.finance_world import World, finalized_plan, item_body, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers
from tests.integration.test_imports import XLSX, unit_row, xlsx


async def audit(w: World, slug: str = "aksu-konaklari", **params: Any) -> dict[str, Any]:
    response = await w.api.get(w.url("/audit", slug), headers=w.headers, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def items(w: World, **params: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = (await audit(w, page_size=100, **params))["items"]
    return found


async def pay(w: World, amount: str = "400.00") -> str:
    response = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": amount,
            "date": "2026-06-18",
            "method": "cash",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["payment"]["id"])


async def test_tahsilat_kim_ne_zaman_kaydedilir(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    payment_id = await pay(world)

    [row] = await items(world, entity="payments")
    assert row["action"] == "create"
    assert row["entity_id"] == payment_id
    assert row["actor_name"] == "Kerem YILDIRIM"
    assert row["user_id"] is not None
    assert row["ip"]
    after = row["after"]
    assert isinstance(after, dict)
    assert (after["amount"], after["method"], after["date"]) == ("400.00", "cash", "2026-06-18")
    assert row["before"] is None
    # koşu tek satırdır; ürettiği borç satırları (türetilmiş, değişmez) ayrıca yazılmaz
    assert len(await items(world, entity="charge_runs")) == 1
    assert await items(world, entity="charges") == []
    assert await items(world, entity="ledger_entries") == []


async def test_degisiklik_once_sonra_farkiyla_yazilir(world: World) -> None:
    plan = (await world.post("/budget-plans", {"fiscal_year": 2026, "name": "Proje"})).json()
    plan_id = plan["data"]["id"]
    added = await world.post(f"/budget-plans/{plan_id}/items", item_body(world))
    item_id = added.json()["data"]["items"][0]["id"]
    updated = await world.api.put(
        world.url(f"/budget-plans/{plan_id}/items/{item_id}"),
        json=item_body(world, annual_amount="18000.00"),
        headers=world.headers,
    )
    assert updated.status_code == 200, updated.text

    [change] = await items(world, entity="budget_items", action="update")
    assert change["entity_id"] == item_id
    assert change["before"] == {"annual_amount": "12000.00"}
    assert change["after"] == {"annual_amount": "18000.00"}
    # değişmeyen alan yazılmaz; yeni üstte
    listed = await items(world, entity_id=item_id)
    assert [r["action"] for r in listed] == ["update", "create"]


async def test_modul_ac_kapa_kaydedilir(world: World) -> None:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Standart", max_units=150, allowed_modules=["finance", "requests"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/requests/toggle")).status_code == 200
    assert (await world.post("/modules/requests/toggle")).status_code == 200

    rows = await items(world, entity="site_modules", action="update")
    assert [(r["before"], r["after"]) for r in rows] == [
        ({"enabled": True}, {"enabled": False}),  # yeni üstte: kapatma
        ({"enabled": False}, {"enabled": True}),
    ]
    assert all(r["actor_name"] == "Kerem YILDIRIM" for r in rows)


async def test_aktarim_ve_belge_indirme_kaydedilir(world: World, tmp_path: Path) -> None:
    import_store = ImportStore(tmp_path / "imports")
    file_store = FileStore(tmp_path / "files")
    world.app.dependency_overrides[get_import_store] = lambda: import_store
    world.app.dependency_overrides[get_file_store] = lambda: file_store

    uploaded = await world.api.post(
        world.url("/imports/units"),
        files={"file": ("d.xlsx", xlsx(unit_row("B", "1"), unit_row("B", "2")), XLSX)},
        headers=world.headers,
    )
    import_id = uploaded.json()["import_id"]
    confirmed = await world.post(f"/imports/units/{import_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    [imported] = await items(world, action="import")
    assert (imported["entity"], imported["entity_id"]) == ("units", import_id)
    assert isinstance(imported["after"], dict)
    assert imported["after"]["created_units"] == 2
    # aktarımın açtığı bölümler tek tek değil, tek olay olarak yazılır
    assert await items(world, entity="units", action="create") == []

    category = world.lookups["category"]
    expense = await world.api.post(
        world.url("/expenses"),
        data={
            "expense_category_id": category,
            "description": "Asansör bakımı",
            "amount": "100.00",
            "date": "2026-06-20",
            "paid": "false",
        },
        files={"document": ("fatura.pdf", b"%PDF-1.4 fatura", "application/pdf")},
        headers=world.headers,
    )
    assert expense.status_code == 201, expense.text
    file_id = expense.json()["data"]["stored_file_id"]
    assert (await world.get(f"/files/{file_id}")).status_code == 200

    [download] = await items(world, action="download")
    assert (download["entity"], download["entity_id"]) == ("stored_files", file_id)
    assert download["after"] == {"file_name": "fatura.pdf"}
    [created] = await items(world, entity="expenses")
    assert isinstance(created["after"], dict)
    assert created["after"]["amount"] == "100.00"


async def test_kayit_degistirilemez_silinemez(world: World) -> None:
    await finalized_plan(world)
    with site_scope(world.site_id):
        async with world.factory() as session:
            assert await session.scalar(select(AuditLog.id).limit(1)) is not None
            for statement in ("UPDATE audit_log SET actor_name = 'x'", "DELETE FROM audit_log"):
                with pytest.raises(DBAPIError, match="Geçmiş kaydı değiştirilemez"):
                    await session.execute(text(statement))
                await session.rollback()


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    await pay(world)
    for email, role, status in (
        ("denetci@test.local", "Denetçi", 200),
        ("muhasebe@test.local", "Muhasebe", 403),
        ("sakin@test.local", "Sakin", 403),
    ):
        user = await create_user(world.factory, email)
        await add_site_membership(world.factory, world.site_id, user.id, role)
        headers = await login_headers(world.api, email)
        response = await world.api.get(world.url("/audit"), headers=headers)
        assert response.status_code == status, (role, response.text)

    outsider = await create_user(world.factory, "yabanci@test.local")
    await add_site_membership(world.factory, world.other_site_id, outsider.id, "Yönetici")
    headers = await login_headers(world.api, "yabanci@test.local")
    assert (await world.api.get(world.url("/audit"), headers=headers)).status_code == 404

    # iki sitede yönetici: Yıldız'ın kaydında Aksu'nun tahsilatı yok
    other = await audit(world, "yildiz-sitesi", entity="payments")
    assert other["total"] == 0


async def test_filtreler_ve_sayfalama(world: World) -> None:
    await finalized_plan(world)
    await post_run(world)
    for amount in ("100.00", "200.00", "300.00"):
        await pay(world, amount)

    page = await audit(world, entity="payments", page_size=2)
    assert (page["total"], len(page["items"])) == (3, 2)
    amounts = [r["after"]["amount"] for r in page["items"]]
    assert amounts == ["300.00", "200.00"]

    at = (await items(world))[0]["at"]
    assert isinstance(at, str)
    day = datetime.fromisoformat(at).astimezone(BUSINESS_TZ).date().isoformat()  # iş günü
    assert (await audit(world, entity="payments", **{"from": day, "to": day}))["total"] == 3
    assert (await audit(world, entity="payments", to="2000-01-01"))["total"] == 0
    assert (await audit(world, user_id=str(uuid.uuid4())))["total"] == 0
    bad = await world.api.get(world.url("/audit"), headers=world.headers, params={"action": "x"})
    assert bad.status_code == 422
