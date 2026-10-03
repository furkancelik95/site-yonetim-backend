"""Sakin uçları — docs/06 §2.14, docs/05 §6 (gerçek PostgreSQL).

Trello "BE · Dilim 8" — bitti ölçütü: sakin kendi bölümünü, borcunu, duyurularını, taleplerini
ve site giderlerini görür. Kurgu `finance_world`: A-1 maliki Ayşe (M + O hesapları), aylık 1.000.
"""

import uuid
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import Plan, Site
from site_yonetim.services.files import FileStore
from site_yonetim.services.sites import set_module_enabled
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def _resident(w: World, email: str, person_id: str) -> dict[str, str]:
    user = await create_user(w.factory, email, kind="resident")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(person_id)
    )
    return await login_headers(w.api, email)


async def _modules_on(w: World) -> None:
    async with w.factory() as session, session.begin():
        plan = Plan(
            name="Standart", max_units=150, allowed_modules=["finance", "announcements", "requests"]
        )
        session.add(plan)
        await session.flush()
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.plan_id = plan.id
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            site = await session.get(Site, w.site_id)
            assert site is not None
            for key in (ModuleKey.ANNOUNCEMENTS, ModuleKey.REQUESTS):
                await set_module_enabled(session, site, key, enable=True)


@pytest.fixture
async def sakin(world: World, api_app: FastAPI, tmp_path: Path) -> tuple[World, dict[str, str]]:
    api_app.dependency_overrides[get_file_store] = lambda: FileStore(tmp_path)
    await finalized_plan(world)
    await post_run(world)  # Haziran 1.000 → A1-O
    return world, await _resident(world, "ayse@test.local", world.person_id)


async def get(w: World, headers: dict[str, str], path: str, **kw: object) -> httpx2.Response:
    return await w.api.get(w.url(f"/resident{path}"), headers=headers, **kw)  # type: ignore[arg-type]


async def test_sakin_ana_sayfasi(sakin: tuple[World, dict[str, str]]) -> None:
    w, headers = sakin
    body = (await get(w, headers, "/home")).json()
    assert body["units"] == [{"unit_id": w.unit_id, "unit_name": "A-1", "role": "owner"}]
    assert {a["reference_code"]: a["balance"] for a in body["accounts"]} == {
        "A1-M": "0.00",
        "A1-O": "1000.00",
    }
    assert body["total_balance"] == "1000.00"
    assert body["accounts"][0]["person_name"] == "Ayşe YILMAZ"
    assert [e["description"] for e in body["recent_entries"]] == ["06/2026 tahakkuku — Aidat"]
    # plansız sitede duyuru/talep modülü kapalı
    assert (body["announcements"], body["open_requests"]) == (None, None)


async def test_sakin_odeme_bilgisini_gorur(sakin: tuple[World, dict[str, str]]) -> None:
    w, headers = sakin
    assert (await get(w, headers, "/home")).json()["payment_info"] is None  # IBAN girilmemiş
    async with w.factory() as session, session.begin():
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.iban, site.bank_name = "TR330006100519786457841326", "Örnek Bank"
    assert (await get(w, headers, "/home")).json()["payment_info"] == {
        "bank_name": "Örnek Bank",
        "iban": "TR330006100519786457841326",
        "account_holder": "Aksu Konakları",
    }


async def test_only_people_can_use_resident_screens(sakin: tuple[World, dict[str, str]]) -> None:
    w, _ = sakin
    response = await get(w, w.headers, "/home")  # yönetici: kişiye bağlı değil
    assert response.status_code == 403
    assert response.json()["error"]["message"] == "Bu ekran yalnız sakinler içindir."
    other_site = await w.api.get(w.url("/resident/home", "yildiz-sitesi"), headers=sakin[1])
    assert other_site.status_code == 404


async def test_statement_is_own_only(sakin: tuple[World, dict[str, str]]) -> None:
    w, headers = sakin
    body = (await get(w, headers, "/statement")).json()
    assert body["account"]["reference_code"] == "A1-O"  # varsayılan oturan hesabı
    assert body["entries"]["items"][0]["running_balance"] == "1000.00"
    assert body["last_charge"]["period"] == "06/2026"
    owner_account = next(
        a["id"] for a in (await get(w, headers, "/home")).json()["accounts"] if a["kind"] == "owner"
    )
    assert (
        await get(w, headers, "/statement", params={"account_id": owner_account})
    ).status_code == 200
    # başka kişinin hesabı yok sayılır
    other = await w.post("/people", {"first_name": "Başka", "last_name": "Kişi"})
    stranger = await _resident(w, "baska@test.local", other.json()["data"]["id"])
    assert (
        await get(w, stranger, "/statement", params={"account_id": w.account_id})
    ).status_code == 404
    assert (await get(w, stranger, "/statement")).status_code == 404  # hiç hesabı yok


async def test_announcements_and_requests(sakin: tuple[World, dict[str, str]]) -> None:
    w, headers = sakin
    assert (await get(w, headers, "/announcements")).status_code == 404  # modül kapalı
    assert (await get(w, headers, "/requests")).status_code == 404
    await _modules_on(w)
    published = await w.post("/announcements", {"title": "Su kesintisi", "body": "Yarın."})
    assert published.status_code == 201, published.text
    listed = (await get(w, headers, "/announcements")).json()
    assert [a["title"] for a in listed["items"]] == ["Su kesintisi"]

    created = await w.api.post(
        w.url("/resident/requests"),
        json={
            "title": "Musluk damlatıyor",
            "unit_id": w.unit_id,
            "reported_by_person_id": str(uuid.uuid4()),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["reported_by_person_id"] == w.person_id  # her zaman kendi adına
    staff = await w.post("/requests", {"title": "Yönetici talebi"})
    assert staff.status_code == 201
    mine = (await get(w, headers, "/requests")).json()
    assert [r["title"] for r in mine["items"]] == ["Musluk damlatıyor"]

    home = (await get(w, headers, "/home")).json()
    assert [a["title"] for a in home["announcements"]] == ["Su kesintisi"]
    assert [(r["number"], r["status_label"]) for r in home["open_requests"]] == [(1, "Açık")]

    foreign_unit = await w.post("/units", {"block_id": w.lookups["block"], "number": "2"})
    denied = await w.api.post(
        w.url("/resident/requests"),
        json={"title": "Komşu dairesi", "unit_id": foreign_unit.json()["data"]["id"]},
        headers=headers,
    )
    assert denied.json()["error"]["code"] == "unit_not_yours"


async def test_site_expenses_with_invoice(sakin: tuple[World, dict[str, str]]) -> None:
    w, headers = sakin
    category = w.lookups["category"]

    async def expense(amount: str, files: dict[str, object] | None = None) -> str:
        response = await w.api.post(
            w.url("/expenses"),
            data={"expense_category_id": category, "description": "Asansör bakımı",
                  "amount": amount, "date": "2026-06-10"},
            files=files,  # type: ignore[arg-type]
            headers=w.headers,
        )  # fmt: skip
        assert response.status_code == 201, response.text
        return str(response.json()["data"]["id"])

    await expense(
        "500.00", files={"document": ("fatura.pdf", b"%PDF-1.4 fatura", "application/pdf")}
    )
    reversed_id = await expense(
        "200.00", files={"document": ("iptal.pdf", b"%PDF-1.4 iptal", "application/pdf")}
    )
    await w.post(f"/expenses/{reversed_id}/reverse", {"reason": "Mükerrer"})

    body = (await get(w, headers, "/expenses", params={"year": 2026})).json()
    assert (body["total"], body["total_amount"]) == (1, "500.00")  # geri alınan ve düzeltme yok
    [item] = body["items"]
    assert (item["description"], item["category"], item["is_paid"]) == (
        "Asansör bakımı",
        "İşletme Giderleri",
        False,
    )
    invoice = await get(w, headers, f"/files/{item['document_id']}")
    assert invoice.status_code == 200
    assert invoice.content == b"%PDF-1.4 fatura"
    reversed_doc = (await w.get(f"/expenses/{reversed_id}")).json()["stored_file_id"]
    assert (await get(w, headers, f"/files/{reversed_doc}")).status_code == 404
    assert (await get(w, headers, f"/files/{uuid.uuid4()}")).status_code == 404
