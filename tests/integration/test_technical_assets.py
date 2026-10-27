"""Teknik varlık, yasal muayene takvimi ve periyodik bakım — Yazılım Dokümanı HIZ-07, HIZ-06;
PM kararı K28 (mali demirbaştan ayrı liste). Gerçek PostgreSQL; bugün 20.06.2026.
"""

import asyncio
import uuid
from datetime import date
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim import cli
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Notification, Plan, Request, Site
from site_yonetim.services import technical_assets as svc
from site_yonetim.services.files import FileStore
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

FORM = "Formda düzeltilmesi gereken alanlar var."
PDF = b"%PDF-1.4 muayene raporu"
ELEVATOR = {
    "category": "elevator",
    "name": "A Blok asansörü",
    "location": "A Blok",
    "brand": "Kone",
    "serial_no": "KN-778",
    "installed_on": "2018-03-01",
    "warranty_until": "2027-03-01",
    "service_company": "Asansör Bakım Ltd.",
    "inspections": [{"template_key": "elevator_periodic", "last_inspected_on": "2025-11-15"}],
}


@pytest.fixture
async def tech(world: World, api_app: FastAPI, tmp_path: Path) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Teknik", max_units=150, allowed_modules=["finance", "requests"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/requests/toggle")).status_code == 200
    api_app.dependency_overrides[get_file_store] = lambda: FileStore(tmp_path)
    return world


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    assert response.json()["error"]["message"] == FORM
    return dict(response.json()["error"]["fields"])


def code(response: httpx2.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def staff(w: World, email: str, role: str) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return user.id, await login_headers(w.api, email)


async def record(
    w: World, schedule_id: str, result: str, *, report: bytes | None = PDF, **form: str
) -> httpx2.Response:
    files = {"report": ("rapor.pdf", report, "application/pdf")} if report is not None else None
    return await w.api.post(
        w.url(f"/asset-inspection-schedules/{schedule_id}/records"),
        data={"inspected_on": "2026-06-18", "result": result, **form},
        files=files,
        headers=w.headers,
    )


async def test_varlik_karti_ve_muayene_takvimi_s1(tech: World) -> None:
    w = tech
    templates = (await w.get("/asset-inspection-templates")).json()
    assert next(t for t in templates if t["key"] == "elevator_periodic")["period_months"] == 12
    assert fields(
        await w.post(
            "/technical-assets",
            {"category": "uçak", "name": "", "installed_on": "2026-01-01",
             "warranty_until": "2025-01-01", "contract_id": str(uuid.uuid4()),
             "inspections": [{"name": "x"}]},
        )
    ) == {
        "name": "Varlığın adını yazın (ör. A Blok asansörü).",
        "category": "Kategori seçin.",
        "warranty_until": "Garanti bitişi kurulumdan önce olamaz.",
        "contract_id": "Sözleşme bulunamadı.",
        "inspections.0.name": "Muayenenin adını yazın.",
        "inspections.0.period_months": "Periyot 1–120 ay olmalı.",
        "inspections.0.last_inspected_on": "Son muayene ya da sonraki muayene tarihini girin.",
    }  # fmt: skip
    created = await w.post("/technical-assets", ELEVATOR)
    assert created.status_code == 201, created.text
    asset = created.json()["data"]
    assert (asset["code"], asset["status"], asset["under_warranty"]) == ("TV-0001", "working", True)
    # S1: periyot 1 yıl, son muayene 15.11.2025 → sonraki 15.11.2026
    upcoming = asset["next_inspection"]
    assert (upcoming["next_due_on"], upcoming["days_left"], upcoming["warning"]) == (
        "2026-11-15",
        148,
        None,
    )
    # Mali demirbaş listesi ayrı (K28)
    assert (await w.get("/assets")).json() == []
    second = (
        await w.post("/technical-assets", {"category": "generator", "name": "Jeneratör"})
    ).json()
    assert second["data"]["code"] == "TV-0002"
    assert [
        a["code"]
        for a in (await w.get("/technical-assets", params={"category": "generator"})).json()
    ] == ["TV-0002"]

    # 60/30/7 uyarısı: 15.09'da yok, 16.09'da ilk uyarı (bir kez), 08.11'de 7 gün
    assert await svc.run_daily(w.factory, today=date(2026, 9, 15)) == []
    first = await svc.run_daily(w.factory, today=date(2026, 9, 16))
    again = await svc.run_daily(w.factory, today=date(2026, 9, 17))
    week = await svc.run_daily(w.factory, today=date(2026, 11, 8))
    assert [(r.warnings, r.runs) for r in first + again + week] == [(1, 0), (0, 0), (1, 0)]
    with site_scope(w.site_id):
        async with w.factory() as session:
            titles = list(
                await session.scalars(select(Notification.title).order_by(Notification.created_at))
            )
    assert titles == [
        "Muayeneye 60 gün kaldı: A Blok asansörü — Asansör periyodik kontrolü",
        "Muayeneye 7 gün kaldı: A Blok asansörü — Asansör periyodik kontrolü",
    ]
    calendar = (await w.get("/asset-inspections/calendar")).json()
    assert [(c["asset_code"], c["schedule"]["name"]) for c in calendar] == [
        ("TV-0001", "Asansör periyodik kontrolü")
    ]
    assert (await w.get("/asset-inspections/calendar", params={"within_days": 30})).json() == []

    # Takvim ekle / düzenle / kapat
    added = await w.post(
        f"/technical-assets/{second['data']['id']}/inspection-schedules",
        {"template_key": "generator_load_test", "next_due_on": "2026-07-01"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["data"]["warning"] == "30"
    edited = await w.api.patch(
        w.url(f"/asset-inspection-schedules/{added.json()['data']['id']}"),
        json={"period_months": 6, "last_inspected_on": "2026-06-01", "is_active": False},
        headers=w.headers,
    )
    assert edited.json()["data"]["next_due_on"] == "2026-12-01"
    assert edited.json()["data"]["warning"] is None
    assert "last_inspected_on" in fields(
        await w.post(
            f"/technical-assets/{second['data']['id']}/inspection-schedules",
            {"name": "Topraklama", "period_months": 12, "last_inspected_on": "2026-07-01"},
        )
    )


async def test_muayene_sonucu_rapor_zorunlu_olumsuz_is_emri(tech: World) -> None:
    w = tech
    asset = (await w.post("/technical-assets", ELEVATOR)).json()["data"]
    schedule = asset["next_inspection"]["id"]
    assert fields(await record(w, schedule, "maybe", report=None, cost="12,5")) == {
        "cost": "Bedeli 1234.56 biçiminde girin."
    }
    assert fields(await record(w, schedule, "maybe", report=None)) == {
        "result": "Sonucu seçin (olumlu / olumsuz).",
        "report": "Muayene raporunu yükleyin (zorunlu).",
    }
    assert "report" in fields(await record(w, schedule, "pass", report=b"merhaba"))
    failed = await record(
        w, schedule, "fail", inspector="Makine Mühendisleri Odası", cost="1500.00"
    )
    assert failed.status_code == 201, failed.text
    data = failed.json()["data"]
    assert data["request_id"] is not None
    assert "Düzeltme iş emri açıldı" in failed.json()["message"]
    detail = (await w.get(f"/technical-assets/{asset['id']}")).json()
    assert detail["asset"]["status"] == "nonconforming"
    assert detail["inspections"][0]["next_due_on"] == "2027-06-18"
    assert detail["records"][0]["cost"] == "1500.00"
    with site_scope(w.site_id):
        async with w.factory() as session:
            request = await session.get(Request, uuid.UUID(data["request_id"]))
            assert request is not None
            assert (request.category, request.priority) == ("elevator", "high")
            with pytest.raises(DBAPIError):  # muayene kaydı değişmez
                await session.execute(text("UPDATE asset_inspection_records SET note = 'x'"))
    report = await w.get(f"/subject-documents/{data['report_document_id']}/download")
    assert report.status_code == 200
    assert report.content == PDF
    passed = await record(w, schedule, "pass", report=b"%PDF-1.4 ikinci rapor")
    assert passed.status_code == 201, passed.text
    assert (await w.get(f"/technical-assets/{asset['id']}")).json()["asset"]["status"] == "working"

    # Varlık belgesi (garanti belgesi vb.)
    doc = await w.api.post(
        w.url("/subject-documents"),
        data={
            "subject_type": "technical_assets",
            "subject_id": asset["id"],
            "title": "Garanti belgesi",
        },
        files={"file": ("garanti.pdf", b"%PDF-1.4 garanti", "application/pdf")},
        headers=w.headers,
    )
    assert doc.status_code == 201, doc.text
    assert doc.json()["data"]["title"] == "TV-0001 A Blok asansörü: Garanti belgesi"
    # Muayene raporu genel yükleme ucundan eklenemez
    assert "subject_type" in fields(
        await w.api.post(
            w.url("/subject-documents"),
            data={"subject_type": "asset_inspection_records", "subject_id": asset["id"]},
            files={"file": ("x.pdf", PDF, "application/pdf")},
            headers=w.headers,
        )
    )

    # Zaman çizelgesi ve yıllık maliyet
    timeline = (await w.get(f"/technical-assets/{asset['id']}/timeline")).json()
    kinds = [e["kind"] for e in timeline["events"]]
    assert {"created", "inspection_fail", "inspection_pass", "document"} <= set(kinds)
    fail_event = next(e for e in timeline["events"] if e["kind"] == "inspection_fail")
    assert fail_event["request_number"] == 1
    assert timeline["annual_costs"] == [{"year": 2026, "total": "1500.00"}]


async def test_periyodik_bakim_is_emri_ve_kapanis(tech: World) -> None:
    w = tech
    asset = (
        await w.post("/technical-assets", {"category": "hydrophore", "name": "Hidrofor"})
    ).json()["data"]
    assert fields(
        await w.post("/maintenance-plans", {"period_unit": "day", "period_count": 0})
    ) == {
        "asset_id": "Varlık seçin.",
        "title": "Bakım işini yazın (ör. Aylık asansör bakımı).",
        "period_unit": "Periyot birimini seçin (hafta / ay).",
        "period_count": "Periyot 1–60 olmalı.",
        "next_due_on": "İlk bakım tarihini seçin.",
    }
    plan = (
        await w.post(
            "/maintenance-plans",
            {
                "asset_id": asset["id"],
                "title": "Aylık hidrofor bakımı",
                "period_unit": "month",
                "period_count": 1,
                "next_due_on": "2026-06-20",
                "responsible": "Pompa Servis",
            },
        )
    ).json()["data"]
    assert plan["days_left"] == 0
    # Gece işi planlanan tarihte iş emri açar; ikinci çalışmada açmaz
    first = await svc.run_daily(w.factory, today=date(2026, 6, 20))
    again = await svc.run_daily(w.factory, today=date(2026, 6, 21))
    assert [(r.warnings, r.runs) for r in first + again] == [(0, 1), (0, 0)]
    runs = (await w.get("/maintenance-runs", params={"status": "open"})).json()
    assert len(runs) == 1
    assert (runs[0]["due_on"], runs[0]["request_number"]) == ("2026-06-20", 1)
    assert code(await w.post(f"/maintenance-plans/{plan['id']}/runs")) == (409, "run_exists")
    run = runs[0]["id"]
    assert fields(await w.post(f"/maintenance-runs/{run}/complete", {"issue_found": True})) == {
        "issue_description": "Bulunan sorunu kısaca anlatın."
    }
    assert "note" in fields(
        await w.post(f"/maintenance-runs/{run}/complete", {"status": "skipped"})
    )
    done = await w.post(
        f"/maintenance-runs/{run}/complete",
        {"done_on": "2026-06-20", "cost": "850.00", "issue_found": True,
         "issue_description": "Basınç şalteri arızalı"},
    )  # fmt: skip
    assert done.status_code == 200, done.text
    assert done.json()["message"] == (
        "Bakım kaydedildi; sonraki bakım 20.07.2026. Arıza iş emri açıldı (#2)."
    )
    assert code(await w.post(f"/maintenance-runs/{run}/complete", {})) == (409, "run_closed")
    detail = (await w.get(f"/technical-assets/{asset['id']}")).json()
    assert detail["asset"]["status"] == "faulty"
    assert detail["plans"][0]["next_due_on"] == "2026-07-20"
    assert detail["plans"][0]["open_run_id"] is None
    # El ile şimdi aç (gece işini beklemeden) ve "yapılmadı" kapat
    opened = await w.post(f"/maintenance-plans/{plan['id']}/runs")
    assert opened.status_code == 201
    skipped = await w.post(
        f"/maintenance-runs/{opened.json()['data']['id']}/complete",
        {"status": "skipped", "note": "Firma gelmedi"},
    )
    assert skipped.json()["data"]["status"] == "skipped"
    timeline = (await w.get(f"/technical-assets/{asset['id']}/timeline")).json()
    assert timeline["annual_costs"] == [{"year": 2026, "total": "850.00"}]
    assert "maintenance_done" in [e["kind"] for e in timeline["events"]]
    off = await w.api.patch(
        w.url(f"/maintenance-plans/{plan['id']}"), json={"is_active": False}, headers=w.headers
    )
    assert off.json()["data"]["is_active"] is False
    assert code(await w.post(f"/maintenance-plans/{plan['id']}/runs")) == (409, "plan_inactive")


async def test_teknik_varlik_yetki_ve_izolasyon(tech: World) -> None:
    w = tech
    asset = (await w.post("/technical-assets", ELEVATOR)).json()["data"]
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/technical-assets"), headers=board)).status_code == 200
    assert (
        await w.api.post(w.url("/technical-assets"), json=ELEVATOR, headers=board)
    ).status_code == 403
    _, technician = await staff(w, "teknik@test.local", "Teknik Personel")
    edited = await w.api.patch(
        w.url(f"/technical-assets/{asset['id']}"), json={"status": "faulty"}, headers=technician
    )
    assert edited.json()["data"]["status"] == "faulty"
    assert edited.json()["data"]["code"] == "TV-0001"
    _, guard = await staff(w, "guvenlik@test.local", "Güvenlik")
    assert (await w.api.get(w.url("/technical-assets"), headers=guard)).status_code == 403
    other = w.url("/technical-assets", "yildiz-sitesi")
    assert (await w.api.get(other, headers=w.headers)).json() == []
    assert (
        await w.api.get(
            w.url(f"/technical-assets/{asset['id']}", "yildiz-sitesi"), headers=w.headers
        )
    ).status_code == 404
    made = await w.api.post(other, json={"category": "camera", "name": "Kamera"}, headers=w.headers)
    assert made.json()["data"]["code"] == "TV-0001"  # numara site içinde
    # Talep modülü kapalıysa iş emri kaydı açılır ama talep açılmaz
    assert (await w.post("/modules/requests/toggle")).status_code == 200
    schedule = asset["next_inspection"]["id"]
    failed = await record(w, schedule, "fail")
    assert failed.status_code == 201
    assert failed.json()["data"]["request_id"] is None


async def test_komut_satiri(
    tech: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    await tech.post("/technical-assets", ELEVATOR)
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-operations-jobs"]) == 0
        assert await asyncio.to_thread(cli.main, ["run-key-reminders"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "sitede varlık/bakım" in out
    assert "sitede anahtar iade kontrolü çalıştı" in out
