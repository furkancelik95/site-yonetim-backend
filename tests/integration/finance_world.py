"""Finans entegrasyon testlerinin ortak kurgusu (docs/07 §3): tek site, tek daire, tek kişi.

`world` fixture'ı `tests/integration/conftest.py` üzerinden tüm entegrasyon testlerine açıktır.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_today
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AccountBalance
from site_yonetim.services.provisioning import provision_site
from tests.integration.helpers import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]


@dataclass
class World:
    api: httpx2.AsyncClient
    app: FastAPI
    factory: Factory
    site_id: uuid.UUID
    other_site_id: uuid.UUID
    headers: dict[str, str]
    lookups: dict[str, str] = field(default_factory=dict)
    account_id: str = ""
    person_id: str = ""
    unit_id: str = ""
    today: list[date] = field(default_factory=lambda: [date(2026, 6, 20)])

    def url(self, path: str, slug: str = "aksu-konaklari") -> str:
        return f"/api/v1/sites/{slug}{path}"

    async def get(self, path: str, **kwargs: Any) -> httpx2.Response:
        return await self.api.get(self.url(path), headers=self.headers, **kwargs)

    async def post(self, path: str, body: object = None, **kwargs: Any) -> httpx2.Response:
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return await self.api.post(self.url(path), json=body, headers=headers, **kwargs)

    def set_today(self, day: date) -> None:
        self.today[0] = day

    async def balance(self) -> Decimal:
        with site_scope(self.site_id):
            async with self.factory() as session:
                value = await session.scalar(
                    select(AccountBalance.balance).where(
                        AccountBalance.account_id == uuid.UUID(self.account_id)
                    )
                )
        return value if value is not None else Decimal("0.00")


@pytest.fixture
async def world(
    api: httpx2.AsyncClient, api_app: FastAPI, session_factory: Factory, admin_engine: object
) -> World:
    site_ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            site_ids.append((await provision_site(session, name=name)).id)
    user = await create_user(session_factory, "yonetici@test.local", full_name="Kerem YILDIRIM")
    for site_id in site_ids:
        await add_site_membership(session_factory, site_id, user.id, "Yönetici")
    w = World(
        api,
        api_app,
        session_factory,
        site_ids[0],
        site_ids[1],
        await login_headers(api, "yonetici@test.local"),
    )
    api_app.dependency_overrides[get_today] = lambda: w.today[0]

    block = (await w.post("/blocks", {"name": "A"})).json()["data"]["id"]
    unit = (await w.post("/units", {"block_id": block, "number": "1"})).json()["data"]["id"]
    party = await w.post(
        f"/units/{unit}/parties",
        {
            "role": "owner",
            "start_date": "2020-01-01",
            "person": {"first_name": "Ayşe", "last_name": "Yılmaz"},
        },
    )
    accounts = party.json()["data"]["opened_accounts"]
    w.account_id = next(a["id"] for a in accounts if a["kind"] == "occupant")
    w.person_id = party.json()["data"]["party"]["person"]["id"]
    w.unit_id = unit
    w.lookups = {
        "category": (await w.get("/expense-categories")).json()[0]["id"],
        "aidat": next(
            t["id"] for t in (await w.get("/charge-types")).json() if t["name"] == "Aidat"
        ),
        "demirbas": next(
            t["id"] for t in (await w.get("/charge-types")).json() if t["payer_rule"] == "owner"
        ),
        "equal": next(
            r["id"] for r in (await w.get("/allocation-rules")).json() if r["kind"] == "equal"
        ),
        "block": block,
    }
    return w


def item_body(w: World, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Aidat",
        "expense_category_id": w.lookups["category"],
        "charge_type_id": w.lookups["aidat"],
        "allocation_rule_id": w.lookups["equal"],
        "annual_amount": "12000.00",
        "frequency": "monthly",
    }
    body.update(overrides)
    return body


async def finalized_plan(w: World, *items: dict[str, object], year: int = 2026) -> str:
    plan = (await w.post("/budget-plans", {"fiscal_year": year, "name": f"{year} Projesi"})).json()
    plan_id = plan["data"]["id"]
    for body in items or (item_body(w),):
        response = await w.post(f"/budget-plans/{plan_id}/items", body)
        assert response.status_code == 201, response.text
    today = w.today[0]
    notified = await w.post(
        f"/budget-plans/{plan_id}/notify", {"notified_on": f"{today.year}-01-02"}
    )
    assert notified.status_code == 200, notified.text
    finalized = await w.post(f"/budget-plans/{plan_id}/finalize")
    assert finalized.status_code == 200, finalized.text
    return str(plan_id)


async def post_run(w: World, charge_date: str = "2026-06-01", **kwargs: Any) -> httpx2.Response:
    return await w.post("/charge-runs", {"charge_date": charge_date}, **kwargs)
