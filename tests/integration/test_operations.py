"""Talep ve duyuru — docs/06 §2.10–2.11, docs/05 §6 (gerçek PostgreSQL, RLS'e tabi rol).

Trello "BE · Dilim 5 · Duyuru ve talep" — bitti ölçütü: duyuru yayınlanır, talep açılıp
durumu değişir.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_today
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import AccountBalance, AnnouncementDelivery, LedgerAccount, Plan, Site
from site_yonetim.services.provisioning import provision_site
from site_yonetim.services.sites import set_module_enabled
from tests.integration.helpers import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
MODULES = ["finance", "announcements", "requests"]


@dataclass
class Ops:
    api: httpx2.AsyncClient
    factory: Factory
    site_id: uuid.UUID
    other_site_id: uuid.UUID
    manager: dict[str, str]
    ids: dict[str, str] = field(default_factory=dict)
    today: list[date] = field(default_factory=lambda: [date(2026, 10, 1)])

    def url(self, path: str, slug: str = "aksu-konaklari") -> str:
        return f"/api/v1/sites/{slug}{path}"

    async def get(
        self, path: str, headers: dict[str, str] | None = None, **kw: Any
    ) -> httpx2.Response:
        return await self.api.get(self.url(path), headers=headers or self.manager, **kw)

    async def post(
        self, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return await self.api.post(self.url(path), json=body, headers=headers or self.manager)

    async def user(self, email: str, role: str, **fields: Any) -> dict[str, str]:
        kind = fields.pop("kind", None)
        user = await create_user(self.factory, email, **({"kind": kind} if kind else {}))
        await add_site_membership(self.factory, self.site_id, user.id, role, **fields)
        return await login_headers(self.api, email)


async def _party(ops: Ops, unit: str, role: str, first: str, last: str) -> str:
    response = await ops.post(
        f"/units/{unit}/parties",
        {
            "role": role,
            "start_date": "2020-01-01",
            "person": {"first_name": first, "last_name": last},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["party"]["person"]["id"])


@pytest.fixture
async def ops(
    api: httpx2.AsyncClient, api_app: FastAPI, session_factory: Factory, admin_engine: object
) -> Ops:
    async with session_factory() as session, session.begin():
        plan = Plan(name="Başlangıç", max_units=30, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        plan_id = plan.id
    site_ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            site_ids.append((await provision_site(session, name=name, plan_id=plan_id)).id)
    manager = await create_user(session_factory, "yonetici@test.local", full_name="Kerem YILDIRIM")
    for site_id in site_ids:
        await add_site_membership(session_factory, site_id, manager.id, "Yönetici")
    o = Ops(
        api,
        session_factory,
        site_ids[0],
        site_ids[1],
        await login_headers(api, "yonetici@test.local"),
    )
    api_app.dependency_overrides[get_today] = lambda: o.today[0]

    for name in ("A", "B"):
        o.ids[f"block_{name}"] = (await o.post("/blocks", {"name": name})).json()["data"]["id"]
        unit = await o.post("/units", {"block_id": o.ids[f"block_{name}"], "number": "1"})
        o.ids[f"unit_{name}"] = unit.json()["data"]["id"]
    o.ids["ayse"] = await _party(o, o.ids["unit_A"], "owner", "Ayşe", "Yılmaz")
    o.ids["elif"] = await _party(o, o.ids["unit_A"], "tenant", "Elif", "Demir")
    o.ids["mehmet"] = await _party(o, o.ids["unit_B"], "owner", "Mehmet", "Kaya")
    return o


async def resident(
    ops: Ops, person: str = "elif", email: str = "sakin@test.local"
) -> dict[str, str]:
    return await ops.user(email, "Sakin", kind="resident", person_id=uuid.UUID(ops.ids[person]))


async def open_request(
    ops: Ops, headers: dict[str, str] | None = None, **body: object
) -> httpx2.Response:
    payload: dict[str, object] = {"title": "Musluk damlatıyor", "category": "plumbing"}
    payload.update(body)
    return await ops.post("/requests", payload, headers)


# --- talep: bitti ölçütü ------------------------------------------------------------------


async def test_talep_acilir_ve_durumu_degisir(ops: Ops) -> None:
    sakin = await resident(ops)
    created = await open_request(ops, sakin, unit_id=ops.ids["unit_A"], priority="high")
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert (data["number"], data["status"], data["status_label"]) == (1, "open", "Açık")
    assert data["unit_name"] == "A-1"
    assert data["reported_by_person_id"] == ops.ids["elif"]
    assert data["reporter_name"] == "Elif DEMİR"  # kendi talebi
    assert created.json()["message"] == "#1 numaralı talep açıldı."

    tech = await ops.user("teknik@test.local", "Teknik Personel")
    request_id = data["id"]
    progress = await ops.post(f"/requests/{request_id}/status", {"status": "in_progress"}, tech)
    assert progress.json()["data"]["status_label"] == "İşlemde"
    no_resolution = await ops.post(f"/requests/{request_id}/status", {"status": "resolved"}, tech)
    assert no_resolution.status_code == 422
    assert "resolution" in no_resolution.json()["error"]["fields"]
    same = await ops.post(f"/requests/{request_id}/status", {"status": "in_progress"}, tech)
    assert same.status_code == 409
    assigned = await ops.post(f"/requests/{request_id}/assign", {"assignee": "Tesisatçı Ali"}, tech)
    assert assigned.json()["message"] == "#1 talep Tesisatçı Ali kişisine atandı."
    resolved = await ops.post(
        f"/requests/{request_id}/status",
        {"status": "resolved", "resolution": "Conta değişti"},
        tech,
    )
    body = resolved.json()["data"]
    assert (body["status"], body["resolution"]) == ("resolved", "Conta değişti")
    assert body["resolved_at"] is not None
    assert body["reporter_name"] is None  # teknik personelde people.read yok

    detail = (await ops.get(f"/requests/{request_id}", sakin)).json()
    assert [e["kind"] for e in detail["events"]] == [
        "created",
        "status_changed",
        "assigned",
        "resolved",
    ]
    assert detail["events"][-1]["description"] == "Durum: İşlemde → Çözüldü. Çözüm: Conta değişti"
    assert detail["events"][-1]["actor_name"] == "Test KULLANICI"

    reopened = await ops.post(f"/requests/{request_id}/status", {"status": "open"}, tech)
    assert reopened.json()["data"]["resolved_at"] is None


async def test_resident_rules(ops: Ops) -> None:
    sakin = await resident(ops)
    foreign_unit = await open_request(ops, sakin, unit_id=ops.ids["unit_B"])
    assert foreign_unit.status_code == 422
    assert foreign_unit.json()["error"]["code"] == "unit_not_yours"
    common = await open_request(
        ops,
        sakin,
        title="Otopark lambası",
        location="B blok otopark",
        reported_by_person_id=ops.ids["mehmet"],
    )
    assert common.json()["data"]["reported_by_person_id"] == ops.ids["elif"]  # başkası adına açamaz
    staff = await open_request(
        ops, reported_by_person_id=ops.ids["mehmet"], unit_id=ops.ids["unit_B"]
    )
    others = staff.json()["data"]["id"]

    mine = (await ops.get("/requests", sakin)).json()
    assert [r["title"] for r in mine["items"]] == ["Otopark lambası"]
    assert (await ops.get(f"/requests/{others}", sakin)).status_code == 404
    assert (await ops.post(f"/requests/{others}/comments", {"body": "?"}, sakin)).status_code == 404
    own_id = common.json()["data"]["id"]
    comment = await ops.post(f"/requests/{own_id}/comments", {"body": "  Hâlâ  yanıyor  "}, sakin)
    assert comment.status_code == 201
    assert comment.json()["data"]["events"][-1]["description"] == "Hâlâ yanıyor"
    denied = await ops.post(
        f"/requests/{own_id}/status", {"status": "closed", "resolution": "x"}, sakin
    )
    assert denied.status_code == 403

    everything = (await ops.get("/requests")).json()
    assert [r["number"] for r in everything["items"]] == [2, 1]
    assert everything["items"][0]["reporter_name"] == "Mehmet KAYA"  # yöneticide people.read var


async def test_numbers_are_unique_under_concurrency(ops: Ops) -> None:
    responses = await asyncio.gather(*(open_request(ops, title=f"Talep {i}") for i in range(6)))
    assert sorted(r.json()["data"]["number"] for r in responses) == [1, 2, 3, 4, 5, 6]


async def test_filters_and_validation(ops: Ops) -> None:
    await open_request(ops, priority="urgent", category="elevator", title="Asansör durdu")
    await open_request(ops)
    urgent = (await ops.get("/requests", params={"priority": "urgent"})).json()
    assert [r["title"] for r in urgent["items"]] == ["Asansör durdu"]
    assert (await ops.get("/requests", params={"status": "resolved"})).json()["total"] == 0
    assert (await ops.get("/requests", params={"category": "elevator"})).json()["total"] == 1
    assert (await open_request(ops, title="ab")).status_code == 422
    missing = await open_request(ops, unit_id=str(uuid.uuid4()))
    assert missing.json()["error"]["fields"] == {"unit_id": "Seçilen bölüm bulunamadı."}
    assert (await ops.get(f"/requests/{uuid.uuid4()}")).status_code == 404


async def test_request_permissions(ops: Ops) -> None:
    guard = await ops.user("guvenlik@test.local", "Güvenlik")
    created = await open_request(ops, guard, title="Kapı kartı çalışmıyor")
    assert created.status_code == 201  # requests.create var
    assert (await ops.get("/requests", guard)).status_code == 403  # requests.read yok
    auditor = await ops.user("denetci@test.local", "Denetçi")
    assert (await open_request(ops, auditor)).status_code == 403


async def test_request_events_are_immutable(ops: Ops) -> None:
    await open_request(ops)
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            with pytest.raises(DBAPIError, match="Geçmiş kaydı"):
                await session.execute(text("UPDATE request_events SET description = 'x'"))


# --- duyuru: bitti ölçütü --------------------------------------------------------------


async def publish(ops: Ops, **body: object) -> httpx2.Response:
    payload: dict[str, object] = {"title": "Su kesintisi", "body": "Yarın 10:00–14:00 arası."}
    payload.update(body)
    return await ops.post("/announcements", payload)


async def deliveries(ops: Ops, announcement_id: str) -> list[AnnouncementDelivery]:
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            rows = await session.scalars(
                select(AnnouncementDelivery).where(
                    AnnouncementDelivery.announcement_id == uuid.UUID(announcement_id)
                )
            )
            return list(rows)


async def test_duyuru_yayinlanir_sakin_okur(ops: Ops) -> None:
    response = await publish(ops, channels=["sms"], importance="important")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["recipient_count"], data["read_count"]) == (3, 0)
    assert data["published_by"] == "Kerem YILDIRIM"
    assert response.json()["message"].startswith("Duyuru yayınlandı: 3 kişi uygulamada görecek.")
    rows = await deliveries(ops, data["id"])
    assert sorted((r.channel, r.sent_at is not None) for r in rows) == [
        ("in_app", True),
        ("in_app", True),
        ("in_app", True),
        ("sms", False),
        ("sms", False),
        ("sms", False),
    ]  # fmt: skip  — gerçek gönderim yok (K5)

    sakin = await resident(ops)
    listed = (await ops.get("/announcements", sakin)).json()
    assert [(a["title"], a["read_at"], a["recipient_count"]) for a in listed["items"]] == [
        ("Su kesintisi", None, None)
    ]
    read = await ops.post(f"/announcements/{data['id']}/read", None, sakin)
    assert read.status_code == 200
    again = await ops.post(f"/announcements/{data['id']}/read", None, sakin)
    assert again.json()["read_at"] == read.json()["read_at"]  # ilk okuma anı korunur
    stats = (await ops.get(f"/announcements/{data['id']}")).json()
    assert (stats["recipient_count"], stats["read_count"]) == (3, 1)


@pytest.mark.parametrize(
    ("audience", "expected"),
    [("owners_only", {"ayse", "mehmet"}), ("tenants_only", {"elif"}), ("blocks", {"mehmet"})],
)
async def test_audiences(ops: Ops, audience: str, expected: set[str]) -> None:
    extra = {"audience_block_ids": [ops.ids["block_B"]]} if audience == "blocks" else {}
    data = (await publish(ops, audience=audience, **extra)).json()["data"]
    people = {str(r.person_id) for r in await deliveries(ops, data["id"])}
    assert people == {ops.ids[name] for name in expected}


async def test_debtors_only(ops: Ops) -> None:
    with site_scope(ops.site_id):
        async with ops.factory() as session, session.begin():
            account = await session.scalar(
                select(LedgerAccount).where(LedgerAccount.person_id == uuid.UUID(ops.ids["mehmet"]))
            )
            assert account is not None
            session.add(
                AccountBalance(
                    account_id=account.id, debit_total=Decimal(100), balance=Decimal(100)
                )
            )
    data = (await publish(ops, audience="debtors_only", title="Borç hatırlatması")).json()["data"]
    assert {str(r.person_id) for r in await deliveries(ops, data["id"])} == {ops.ids["mehmet"]}


async def test_resident_sees_only_targeted_and_current(ops: Ops) -> None:
    sakin = await resident(ops)
    other_block = (
        await publish(ops, audience="blocks", audience_block_ids=[ops.ids["block_B"]])
    ).json()["data"]
    expiring = (await publish(ops, title="Bugün biter", expires_on="2026-10-01")).json()["data"]
    pinned = (await publish(ops, title="Sabit", is_pinned=True)).json()["data"]
    assert [a["title"] for a in (await ops.get("/announcements", sakin)).json()["items"]] == [
        "Sabit", "Bugün biter"
    ]  # fmt: skip
    assert (await ops.get(f"/announcements/{other_block['id']}", sakin)).status_code == 404
    assert (
        await ops.post(f"/announcements/{other_block['id']}/read", None, sakin)
    ).status_code == 404

    ops.today[0] = date(2026, 10, 2)
    assert [a["id"] for a in (await ops.get("/announcements", sakin)).json()["items"]] == [
        pinned["id"]
    ]
    manager_view = (await ops.get("/announcements")).json()
    assert manager_view["total"] == 3  # yayınlayan süresi geçenleri de görür
    assert manager_view["items"][0]["id"] == pinned["id"]
    assert expiring["id"] in {a["id"] for a in manager_view["items"]}

    guard = await ops.user("guvenlik@test.local", "Güvenlik")
    assert (await ops.get("/announcements", guard)).json()["total"] == 2  # yürürlükteki hepsi


async def test_publish_validation_and_permissions(ops: Ops) -> None:
    past = await publish(ops, expires_on="2026-09-30")
    assert past.json()["error"]["fields"] == {"expires_on": "Bitiş tarihi geçmiş bir tarih olamaz."}
    no_blocks = await publish(ops, audience="blocks")
    assert no_blocks.json()["error"]["code"] == "audience_blocks_required"
    foreign = await publish(ops, audience="blocks", audience_block_ids=[str(uuid.uuid4())])
    assert foreign.json()["error"]["code"] == "block_not_found"
    tech = await ops.user("teknik@test.local", "Teknik Personel")
    denied = await ops.api.post(
        ops.url("/announcements"), json={"title": "abc", "body": "x"}, headers=tech
    )
    assert denied.status_code == 403
    auditor = await ops.user("denetci@test.local", "Denetçi")
    assert (await ops.get("/announcements", auditor)).status_code == 403
    staff_read = await ops.post(f"/announcements/{(await publish(ops)).json()['data']['id']}/read")
    assert staff_read.status_code == 404  # personel kişi değil, teslim yok


# --- modül kapısı ve izolasyon -----------------------------------------------------------


async def test_closed_module_is_404(ops: Ops) -> None:
    async with ops.factory() as session:
        site = await session.get(Site, ops.site_id)
    assert site is not None
    with site_scope(ops.site_id):
        async with ops.factory() as session, session.begin():
            await set_module_enabled(session, site, ModuleKey.REQUESTS, enable=False)
            await set_module_enabled(session, site, ModuleKey.ANNOUNCEMENTS, enable=False)
    assert (await ops.get("/requests")).status_code == 404
    assert (await publish(ops)).status_code == 404


async def test_other_site_sees_nothing(ops: Ops) -> None:
    request_id = (await open_request(ops)).json()["data"]["id"]
    announcement_id = (await publish(ops, title="Aksu duyurusu")).json()["data"]["id"]
    other = "yildiz-sitesi"
    assert (
        await ops.api.get(ops.url(f"/requests/{request_id}", other), headers=ops.manager)
    ).status_code == 404
    assert (await ops.api.get(ops.url("/requests", other), headers=ops.manager)).json()[
        "total"
    ] == 0
    assert (
        await ops.api.get(ops.url(f"/announcements/{announcement_id}", other), headers=ops.manager)
    ).status_code == 404
    titles = (await ops.api.get(ops.url("/announcements", other), headers=ops.manager)).json()[
        "items"
    ]
    assert titles == []
    closing = {"status": "closed", "resolution": "x"}
    status = await ops.api.post(
        ops.url(f"/requests/{request_id}/status", other), json=closing, headers=ops.manager
    )
    assert status.status_code == 404
    # B'de numara 1'den başlar
    created = await ops.api.post(
        ops.url("/requests", other), json={"title": "Yıldız talebi"}, headers=ops.manager
    )
    assert created.json()["data"]["number"] == 1
