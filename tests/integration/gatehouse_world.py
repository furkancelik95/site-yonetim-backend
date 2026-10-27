"""Güvenlik paketi (servis istekleri 40–46) entegrasyon kurgusu.

`finance_world` üzerine: plan kargo + ziyaretçi modüllerini içerir ve ikisi açık; A-1 maliki Ayşe
sakin hesabıyla, A-2 maliki Mehmet ayrı sakin hesabıyla; Güvenlik görevlisi iki kişi (devir için).
Saat elle ilerletilir: `gw.at(...)` (tesis saati, Europe/Istanbul). Bugün: 20.06.2026 (cumartesi).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx2
import pytest

from site_yonetim.api.deps import get_now, get_today
from site_yonetim.models import Plan, Site
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

TZ = ZoneInfo("Europe/Istanbul")
TODAY = date(2026, 6, 20)


@dataclass
class GateWorld:
    w: World
    guard: dict[str, str]
    guard2: dict[str, str]
    resident: dict[str, str]  # Ayşe — A-1
    neighbour: dict[str, str]  # Mehmet — A-2
    unit2: str
    clock: list[datetime]

    @property
    def api(self) -> httpx2.AsyncClient:
        return self.w.api

    @property
    def manager(self) -> dict[str, str]:
        return self.w.headers

    def url(self, path: str, slug: str = "aksu-konaklari") -> str:
        return self.w.url(path, slug)

    def at(self, hour: int, minute: int = 0, *, day: date = TODAY, second: int = 0) -> None:
        self.clock[0] = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)
        self.w.set_today(day)

    def later(self, **delta: float) -> None:
        self.clock[0] = self.clock[0] + timedelta(**delta)
        self.w.set_today(self.clock[0].astimezone(TZ).date())

    async def get(self, path: str, headers: dict[str, str], **kw: object) -> httpx2.Response:
        return await self.api.get(self.url(path), headers=headers, **kw)  # type: ignore[arg-type]

    async def post(
        self, path: str, headers: dict[str, str], body: object = None
    ) -> httpx2.Response:
        return await self.api.post(self.url(path), headers=headers, json=body)

    async def put(self, path: str, headers: dict[str, str], body: object) -> httpx2.Response:
        return await self.api.put(self.url(path), headers=headers, json=body)

    async def patch(self, path: str, headers: dict[str, str], body: object) -> httpx2.Response:
        return await self.api.patch(self.url(path), headers=headers, json=body)

    async def member(self, email: str, role: str, name: str) -> dict[str, str]:
        """Sonradan üye ekler; giriş gerçek saatle yapılır (jeton süresi gerçek saate göre)."""
        frozen = self.w.app.dependency_overrides.pop(get_now)
        try:
            return await _user(self.w, email, role, name)
        finally:
            self.w.app.dependency_overrides[get_now] = frozen

    async def kinds(self, headers: dict[str, str]) -> list[str]:
        body = (await self.get("/notifications", headers)).json()
        return [n["kind"] for n in body["items"]]


async def _user(w: World, email: str, role: str, name: str, **fields: object) -> dict[str, str]:
    kind = {"kind": "resident"} if role == "Sakin" else {}
    user = await create_user(w.factory, email, full_name=name, **kind)
    await add_site_membership(w.factory, w.site_id, user.id, role, **fields)
    return await login_headers(w.api, email)


@pytest.fixture
async def gw(world: World) -> GateWorld:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Pro", max_units=500, allowed_modules=["finance", "packages", "visitors"])
        session.add(plan)
        await session.flush()
        for site_id in (world.site_id, world.other_site_id):
            site = await session.get(Site, site_id)
            assert site is not None
            site.plan_id = plan.id
    for slug in ("aksu-konaklari", "yildiz-sitesi"):  # B de açık: izolasyon kayıt düzeyinde
        for key in ("packages", "visitors"):
            toggled = await world.api.post(
                world.url(f"/modules/{key}/toggle", slug), headers=world.headers
            )
            assert toggled.status_code == 200, toggled.text
    unit2 = (
        await world.post("/units", {"block_id": world.lookups["block"], "number": "2"})
    ).json()["data"]["id"]
    party = await world.post(
        f"/units/{unit2}/parties",
        {
            "role": "owner",
            "start_date": "2020-01-01",
            "person": {"first_name": "Mehmet", "last_name": "Kaya"},
        },
    )
    mehmet = party.json()["data"]["party"]["person"]["id"]
    # Önce giriş (jeton gerçek saatle imzalanır), sonra saat sabitlenir.
    gate = GateWorld(
        w=world,
        guard=await _user(world, "guvenlik@test.local", "Güvenlik", "Ali GÖREVLİ"),
        guard2=await _user(world, "guvenlik2@test.local", "Güvenlik", "Veli NÖBETÇİ"),
        resident=await _user(
            world, "ayse@test.local", "Sakin", "Ayşe YILMAZ", person_id=uuid.UUID(world.person_id)
        ),
        neighbour=await _user(
            world, "mehmet@test.local", "Sakin", "Mehmet KAYA", person_id=uuid.UUID(mehmet)
        ),
        unit2=unit2,
        clock=[datetime(2026, 6, 20, 10, 0, tzinfo=TZ)],
    )
    world.app.dependency_overrides[get_now] = lambda: gate.clock[0]
    world.app.dependency_overrides[get_today] = lambda: world.today[0]
    return gate
