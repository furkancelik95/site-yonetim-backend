"""Sözleşme performansı — servis istekleri 68–70 (Yazılım Dokümanı 15.3, SAT-06; gerçek PostgreSQL).

Kurgu `finance_world` (bugün 20.06.2026): Kerem YILDIRIM iki sitede Yönetici; Selin KAYA ikinci
Yönetici. Kabul senaryosu S1: 20.000 TL/ay temizlik sözleşmesi, denetim hedefi 85, gerçekleşen
72, "hedefin %10'undan fazla altında → %5" → 1.000,00 TL kesinti onaya düşer.
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim import cli
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import (
    AuditLog,
    ChecklistInspection,
    ChecklistTemplate,
    ContractAmount,
    Notification,
    PatrolPlan,
    PatrolPoint,
    PatrolRun,
    PatrolScan,
    Plan,
    Request,
    RequestCategory,
    Site,
    TaskCompletion,
    TaskTemplate,
)
from site_yonetim.services import contract_performance as svc
from site_yonetim.services.files import FileStore
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers
from tests.integration.test_technical_assets import ELEVATOR

CLEANING = {
    "vendor": "Parlak Temizlik Ltd.",
    "subject": "Ortak alan temizliği",
    "category": "cleaning",
    "start_date": "2025-09-01",
    "end_date": "2027-08-31",
    "amount": "20000.00",
    "period": "monthly",
    "notice_days": 30,
}


@pytest.fixture
async def other(world: World) -> dict[str, str]:
    user = await create_user(world.factory, "ikinci@test.local", full_name="Selin KAYA")
    await add_site_membership(world.factory, world.site_id, user.id, "Yönetici")
    return await login_headers(world.api, "ikinci@test.local")


async def staff(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def contract(w: World, **changes: Any) -> str:
    response = await w.post("/contracts", {**CLEANING, **changes})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


async def put(w: World, cid: str, body: dict[str, Any], **kw: Any) -> httpx2.Response:
    return await w.api.put(
        w.url(f"/contracts/{cid}/performance"), json=body, headers=kw.get("headers", w.headers)
    )


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    return dict(response.json()["error"]["fields"])


S1_RULES: dict[str, Any] = {
    "kpis": [
        {"id": "k1", "name": "Temizlik denetimi", "source": "inspection", "target": 85,
         "weight": 60},
        {"id": "k2", "name": "Sakin memnuniyeti", "source": "manual", "target": 90, "weight": 40},
    ],
    "penalties": [{"kpi_id": "k1", "below_percent": 10, "cut_kind": "percent", "cut_value": "5"}],
    "escalation": {"kind": "none"},
}  # fmt: skip


async def inspections(w: World, scores: list[int], day: date) -> None:
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            template = ChecklistTemplate(name=f"Temizlik {day}", name_key=f"t{day}", items=[])
            session.add(template)
            await session.flush()
            for i, score in enumerate(scores):
                session.add(
                    ChecklistInspection(
                        template_id=template.id, template_name="Temizlik",
                        location=f"Blok {i}", answers=[], score=score, failed=0,
                        by_name="Denetçi",
                        created_at=datetime(day.year, day.month, day.day, 9 + i, tzinfo=UTC),
                    )
                )  # fmt: skip


async def rule(w: World, action: str) -> None:
    response = await w.post(
        "/approval-rules",
        {
            "action_type": action,
            "steps": [{"roles": ["Yönetici"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": w.today[0].isoformat(),
        },
    )
    assert response.status_code == 201, response.text


# --- 68 Tanımlar ---------------------------------------------------------------------------


async def test_performans_tanimlari_dogrulama_ve_kayit(world: World) -> None:
    w = world
    cid = await contract(w)
    empty = (await w.get(f"/contracts/{cid}/performance")).json()
    assert (empty["kpis"], empty["penalties"], empty["vendor"]) == ([], [], None)
    assert empty["escalation"] == {"kind": "none", "fixed_rate": None, "period": "yearly",
                                   "cap": None}  # fmt: skip
    assert empty["monthly_amount"] == "20000.00"
    assert empty["escalation_preview"]["status"] == "none"

    bad = await put(
        w,
        cid,
        {
            "kpis": [
                {"id": "a", "name": "x", "source": "nope", "target": 0, "weight": 50},
                {"id": "b", "name": "Devriye", "source": "patrol", "target": 95, "weight": 30},
            ],
            "penalties": [
                {"kpi_id": "zz", "below_percent": 100, "cut_kind": "percent", "cut_value": "150"}
            ],
            "escalation": {"kind": "fixed", "fixed_rate": 0, "cap": -1},
        },
    )
    assert fields(bad) == {
        "kpis.0.name": "KPI adı yazın (2–60 karakter).",
        "kpis.0.source": "Kaynak seçin.",
        "kpis.0.target": "Hedef 1–100 (%) olmalı.",
        "kpis": "Ağırlıkların toplamı 100 olmalı.",
        "penalties.0.kpi_id": "KPI seçin.",
        "penalties.0.below_percent": "Eşik %0–99 olmalı.",
        "penalties.0.cut_value": "Kesinti %1–100 olmalı.",
        "escalation.fixed_rate": "Oran %0,01–200 olmalı.",
        "escalation.cap": "Tavan boş ya da %0,01–200 olmalı.",
    }
    no_amount = await contract(w, vendor="Bedelsiz Firma", amount=None, period=None)
    assert fields(await put(w, no_amount, {**S1_RULES, "escalation": {"kind": "tufe"}})) == {
        "penalties": "Yüzde kesinti için sözleşmede aylık ya da yıllık bedel olmalı.",
        "escalation.kind": "Artış için sözleşmede bedel olmalı.",
    }

    vendor_id = str(uuid.uuid4())
    saved = await put(w, cid, {**S1_RULES, "vendor_id": vendor_id})
    assert saved.status_code == 200, saved.text
    assert saved.json()["message"] == "Sözleşme performans kuralları kaydedildi."
    data = saved.json()["data"]
    assert data["vendor_id"] == vendor_id
    assert [(k["name"], k["source"], k["target"], k["weight"]) for k in data["kpis"]] == [
        ("Temizlik denetimi", "inspection", "85.00", 60),
        ("Sakin memnuniyeti", "manual", "90.00", 40),
    ]
    k1 = data["kpis"][0]["id"]
    assert [(p["kpi_id"], p["below_percent"], p["cut_kind"], p["cut_value"]) for p in
            data["penalties"]] == [(k1, "10.00", "percent", "5.00")]  # fmt: skip

    # KPI kimliği korunur; kaldırılan KPI'nın ceza kuralı da kalkar.
    kept = await put(
        w,
        cid,
        {"kpis": [{**data["kpis"][1], "weight": 100}], "penalties": data["penalties"][:0]},
    )
    assert [k["id"] for k in kept.json()["data"]["kpis"]] == [data["kpis"][1]["id"]]
    assert kept.json()["data"]["penalties"] == []
    assert kept.json()["data"]["vendor_id"] == vendor_id  # verilmeyen alan korunur

    auditor = await staff(w, "denetci@test.local", "Denetçi")
    assert (
        await w.api.get(w.url(f"/contracts/{cid}/performance"), headers=auditor)
    ).status_code == 200
    assert (await put(w, cid, S1_RULES, headers=auditor)).status_code == 403
    other_site = await w.api.get(
        w.url(f"/contracts/{cid}/performance", "yildiz-sitesi"), headers=w.headers
    )
    assert other_site.status_code == 404
    with site_scope(w.site_id):
        async with w.factory() as session:
            entities = set(await session.scalars(select(AuditLog.entity)))
    assert {"contract_kpis", "contract_penalty_rules", "contract_amounts"} <= entities


# --- 69 Karne ve ceza -------------------------------------------------------------------------


async def test_karne_kabul_senaryosu_s1_ve_ceza_onayi(world: World, other: dict[str, str]) -> None:
    w = world
    cid = await contract(w)
    assert (await put(w, cid, S1_RULES)).status_code == 200
    await inspections(w, [70, 72, 74], date(2026, 5, 10))
    url = f"/contracts/{cid}/scorecards/2026-05"

    draft = (await w.get(url)).json()
    assert (draft["id"], draft["status"]) == (None, "draft")
    assert [(r["name"], r["actual"], r["score"], r["detail"], r["editable"]) for r in
            draft["rows"]] == [
        ("Temizlik denetimi", "72", 85, "3 denetim ortalaması", False),
        ("Sakin memnuniyeti", None, None, "Elle girilir", True),
    ]  # fmt: skip
    assert draft["total"] is None
    assert draft["penalty"]["status"] == "none"
    assert draft["penalty"]["amount"] == "1000.00"
    assert draft["penalty"]["reasons"] == [
        "Temizlik denetimi: gerçekleşen 72, hedef 85 (sınır 76,5) → %5 kesinti"
    ]
    assert (draft["monthly_amount"], draft["expected_invoice"]) == ("20000.00", "20000.00")

    k1, k2 = (r["kpi_id"] for r in draft["rows"])
    patch = await w.api.patch(w.url(url), json={"values": {k1: 99, k2: 101}}, headers=w.headers)
    assert fields(patch) == {
        f"values.{k1}": "Bu KPI kaynaktan hesaplanır; elle girilemez.",
        f"values.{k2}": "0–100.",
    }
    missing = await w.post(f"{url}/finalize")
    assert missing.status_code == 422
    error = missing.json()["error"]
    assert (error["code"], error["message"]) == (
        "missing_values", "Gerçekleşeni eksik KPI: Sakin memnuniyeti.",
    )  # fmt: skip
    saved = await w.api.patch(
        w.url(url), json={"values": {k2: 90}, "comment": " İyi iş "}, headers=w.headers
    )
    assert saved.status_code == 200, saved.text
    card = saved.json()["data"]
    assert (card["total"], card["comment"]) == (91, "İyi iş")  # (85×60 + 100×40) / 100

    open_period = await w.post(f"/contracts/{cid}/scorecards/2026-06/finalize")
    assert (open_period.status_code, open_period.json()["error"]["code"]) == (409, "period_open")
    future = await w.get(f"/contracts/{cid}/scorecards/2026-07")
    assert fields(future) == {"period": "Gelecek dönem seçilemez."}

    finalized = await w.post(f"{url}/finalize")
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["message"] == (
        "Karne kesinleşti; 1.000,00 TL ceza kesintisi onaya düştü."
    )
    card = finalized.json()["data"]
    assert (card["status"], card["finalized_by"], card["penalty"]["status"]) == (
        "finalized", "Kerem YILDIRIM", "pending",
    )  # fmt: skip
    assert (await w.post(f"{url}/finalize")).json()["error"]["code"] == "finalized"
    again = await w.api.patch(w.url(url), json={"comment": "x"}, headers=w.headers)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "finalized")

    own = await w.post(f"{url}/penalty", {"decision": "approve"})
    assert (own.status_code, own.json()["error"]["code"]) == (403, "self_approval")
    no_note = await w.api.post(w.url(f"{url}/penalty"), json={"decision": "reject"}, headers=other)
    assert fields(no_note) == {"note": "Ret gerekçesi yazın (en az 5 karakter)."}
    approved = await w.api.post(
        w.url(f"{url}/penalty"), json={"decision": "approve", "note": ""}, headers=other
    )
    assert approved.status_code == 200, approved.text
    card = approved.json()["data"]
    assert (card["penalty"]["status"], card["penalty"]["decided_by"]) == ("approved", "Selin KAYA")
    assert card["expected_invoice"] == "19000.00"
    assert "faturanızı 19.000,00 TL olarak" in card["penalty"]["notice"]
    twice = await w.api.post(w.url(f"{url}/penalty"), json={"decision": "approve"}, headers=other)
    assert (twice.status_code, twice.json()["error"]["code"]) == (409, "invalid_status")

    perf = (await w.get(f"/contracts/{cid}/performance")).json()
    assert perf["last_scorecards"] == [
        {"period": "2026-05", "total": 91, "status": "finalized", "penalty": "1000.00",
         "penalty_status": "approved"}
    ]  # fmt: skip
    listed = (await w.get(f"/contracts/{cid}/scorecards")).json()
    assert (listed["total"], listed["items"][0]["period"]) == (1, "2026-05")
    # Kesinleşen karne bir ay sonra da aynı kalır (kaynak değişse bile).
    await inspections(w, [100], date(2026, 5, 20))
    assert (await w.get(url)).json()["rows"][0]["actual"] == "72"


async def test_ceza_kesintisi_onay_kuralina_duser(world: World, other: dict[str, str]) -> None:
    w = world
    cid = await contract(w)
    await put(w, cid, S1_RULES)
    await inspections(w, [72], date(2026, 5, 3))
    url = f"/contracts/{cid}/scorecards/2026-05"
    me = (await w.api.get("/api/v1/me", headers=w.headers)).json()
    await w.api.patch(
        w.url(url), json={"values": {(await w.get(url)).json()["rows"][1]["kpi_id"]: 80}},
        headers=w.headers,
    )  # fmt: skip
    await rule(w, "penalty_deduction")
    finalized = await w.post(f"{url}/finalize")
    assert finalized.status_code == 200, finalized.text
    assert "onaya gönderildi (talep #" in finalized.json()["message"]
    request_id = finalized.json()["data"]["penalty"]["approval_request_id"]
    assert request_id
    request = (await w.api.get(w.url(f"/approvals/{request_id}"), headers=other)).json()
    assert request["action_type"] == "penalty_deduction"
    assert request["requested_by"] == me["full_name"]
    # Kesinti uç noktasından karar ONY'ye yazılır.
    decided = await w.api.post(
        w.url(f"{url}/penalty"), json={"decision": "reject", "note": "Firma itiraz etti"},
        headers=other,
    )  # fmt: skip
    assert decided.status_code == 200, decided.text
    penalty = decided.json()["data"]["penalty"]
    assert (penalty["status"], penalty["decided_by"], penalty["note"]) == (
        "rejected", "Selin KAYA", "Firma itiraz etti",
    )  # fmt: skip
    assert decided.json()["data"]["expected_invoice"] == "20000.00"


async def test_ceza_onaya_approvals_ucundan_ve_donem_kilidi(
    world: World, other: dict[str, str]
) -> None:
    w = world
    cid = await contract(w)
    await put(w, cid, {**S1_RULES, "kpis": [{**S1_RULES["kpis"][0], "weight": 100}]})
    await inspections(w, [60], date(2026, 5, 3))
    await rule(w, "penalty_deduction")
    finalized = await w.post(f"/contracts/{cid}/scorecards/2026-05/finalize")
    request_id = finalized.json()["data"]["penalty"]["approval_request_id"]
    ok = await w.api.post(w.url(f"/approvals/{request_id}/approve"), json={}, headers=other)
    assert ok.status_code == 200, ok.text
    card = (await w.get(f"/contracts/{cid}/scorecards/2026-05")).json()
    assert card["penalty"]["status"] == "approved"
    assert card["expected_invoice"] == "19000.00"


async def test_kaynaklar_kapali_modulde_elle_ve_izolasyon(world: World) -> None:
    w = world
    cid = await contract(w)
    body = {"kpis": [{"id": "s", "name": "SLA", "source": "sla", "target": 90, "weight": 100}]}
    assert (await put(w, cid, body)).status_code == 200
    # Planı olmayan sitede talep modülü kapalı: SLA KPI'ı elle girilir.
    row = (await w.get(f"/contracts/{cid}/scorecards/2026-05")).json()["rows"][0]
    assert (row["actual"], row["editable"], row["detail"]) == (
        None, True, "Talep modülü kapalı — elle girin",
    )  # fmt: skip
    outside = await w.get(f"/contracts/{cid}/scorecards/2025-01")
    assert fields(outside) == {"period": "Dönem sözleşme süresi dışında."}
    bad = await w.get(f"/contracts/{cid}/scorecards/2026-13")
    assert fields(bad) == {"period": "Dönem seçin (YYYY-AA)."}
    stranger = await w.api.get(
        w.url(f"/contracts/{cid}/scorecards/2026-05", "yildiz-sitesi"), headers=w.headers
    )
    assert stranger.status_code == 404


# --- 70 Endeksli artış ve bedel geçmişi -----------------------------------------------------


@pytest.fixture
async def platform(world: World) -> dict[str, str]:
    await create_user(
        world.factory, "platform@test.local", full_name="Platform YÖNETİCİ", is_platform_admin=True
    )
    return await login_headers(world.api, "platform@test.local")


async def test_endeksli_artis_bedel_gecmisi(world: World, platform: dict[str, str]) -> None:
    w = world
    cid = await contract(w, start_date="2025-07-15")
    rules = {"escalation": {"kind": "tufe", "period": "yearly", "cap": 30}}
    assert (await put(w, cid, rules)).status_code == 200
    preview = (await w.get(f"/contracts/{cid}/performance")).json()["escalation_preview"]
    assert (preview["status"], preview["date"], preview["days_left"], preview["index_period"]) == (
        "missing_index", "2026-07-15", 25, "2026-06",
    )  # fmt: skip
    blocked = await w.post(f"/contracts/{cid}/escalation", {"decision": "approve"})
    assert (blocked.status_code, blocked.json()["error"]["message"]) == (
        409, "Endeks değeri girilmemiş; platform yöneticisine bildirildi.",
    )  # fmt: skip

    assert (await w.api.get("/api/v1/platform/index-values", headers=w.headers)).status_code == 404
    missing = await w.api.get("/api/v1/platform/index-values/missing", headers=platform)
    assert missing.json() == [{"period": "2026-06", "contracts": 1, "sites": 1}]
    future = await w.api.put(
        "/api/v1/platform/index-values/2026-07", json={"tufe": 1, "ufe": 2}, headers=platform
    )
    assert fields(future) == {"period": "Dönem geçmiş ya da bu ay olmalı (YYYY-AA)."}
    first = await w.api.put(
        "/api/v1/platform/index-values/2026-06", json={"tufe": 40, "ufe": 20}, headers=platform
    )
    assert first.status_code == 200, first.text
    fixed = await w.api.put(
        "/api/v1/platform/index-values/2026-06", json={"tufe": 33.5, "ufe": 28}, headers=platform
    )
    assert fixed.json()["data"]["tufe"] == "33.5"
    listed = (await w.api.get("/api/v1/platform/index-values", headers=platform)).json()
    assert (listed["total"], listed["items"][0]["tufe"], listed["items"][0]["entered_by"]) == (
        1, "33.50", "Platform YÖNETİCİ",
    )  # fmt: skip
    assert (await w.api.get("/api/v1/platform/index-values/missing", headers=platform)).json() == []

    preview = (await w.get(f"/contracts/{cid}/performance")).json()["escalation_preview"]
    assert (preview["status"], preview["rate"], preview["capped_rate"]) == ("due", "33.50", "30.00")
    assert (preview["old_amount"], preview["new_amount"], preview["annual_effect"]) == (
        "20000.00", "26000.00", "72000.00",
    )  # fmt: skip
    no_note = await w.post(f"/contracts/{cid}/escalation", {"decision": "reject"})
    assert fields(no_note) == {"note": "Tedarikçiyle görüşme notunu yazın."}
    approved = await w.post(f"/contracts/{cid}/escalation", {"decision": "approve"})
    assert approved.status_code == 200, approved.text
    assert (
        approved.json()["message"]
        == "Yeni bedel 26.000,00 TL, 15.07.2026 tarihinden itibaren geçerli."
    )
    assert approved.json()["data"]["status"] == "approved"
    again = await w.post(f"/contracts/{cid}/escalation", {"decision": "approve"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "not_due")

    detail = (await w.get(f"/contracts/{cid}")).json()
    assert (detail["amount"], detail["latest_amount"]) == ("20000.00", "26000.00")
    assert [(h["amount"], h["effective_from"], h["source"]) for h in detail["amount_history"]] == [
        ("20000.00", "2025-07-15", "initial"),
        ("26000.00", "2026-07-15", "escalation"),
    ]
    # Artış tarihinden sonra bedel yenidir; öncesinin hesabı eski bedelle.
    w.set_today(date(2026, 8, 5))
    listed_contract = next(c for c in (await w.get("/contracts")).json() if c["id"] == cid)
    assert listed_contract["amount"] == "26000.00"
    perf = (await w.get(f"/contracts/{cid}/performance")).json()
    assert perf["monthly_amount"] == "26000.00"
    assert perf["escalations"][0]["status"] == "approved"
    # Bedel geçmişi değişmez.
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            with pytest.raises(DBAPIError):
                await session.execute(text("UPDATE contract_amounts SET amount = 1"))


async def test_artis_reddi_onay_kurali_ve_sabit_oran(world: World, other: dict[str, str]) -> None:
    w = world
    cid = await contract(w, start_date="2024-07-01", period="yearly", amount="120000.00")
    await put(w, cid, {"escalation": {"kind": "fixed", "fixed_rate": 25, "period": "yearly"}})
    p = (await w.get(f"/contracts/{cid}/performance")).json()["escalation_preview"]
    assert (p["status"], p["date"], p["new_amount"], p["annual_effect"]) == (
        "due", "2026-07-01", "150000.00", "30000.00",
    )  # fmt: skip
    await rule(w, "contract_escalation")
    pending = await w.post(f"/contracts/{cid}/escalation", {"decision": "approve"})
    assert pending.status_code == 202, pending.text
    assert pending.json()["data"]["status"] == "pending"
    request_id = pending.json()["data"]["record"]["approval_request_id"]
    ok = await w.api.post(w.url(f"/approvals/{request_id}/approve"), json={}, headers=other)
    assert ok.status_code == 200, ok.text
    detail = (await w.get(f"/contracts/{cid}")).json()
    assert detail["latest_amount"] == "150000.00"

    cid2 = await contract(w, vendor="Güvenlik A.Ş.", start_date="2024-07-01")
    await put(w, cid2, {"escalation": {"kind": "fixed", "fixed_rate": 10}})
    rejected = await w.post(
        f"/contracts/{cid2}/escalation", {"decision": "reject", "note": "Firma %5'e razı oldu"}
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["message"] == "Bedel değişmedi; görüşme notu kaydedildi."
    assert rejected.json()["data"]["status"] == "rejected"
    assert (await w.get(f"/contracts/{cid2}")).json()["latest_amount"] == "20000.00"

    w.set_today(date(2026, 5, 1))
    cid3 = await contract(w, vendor="Bahçe Ltd.", start_date="2025-07-15")
    await put(w, cid3, {"escalation": {"kind": "fixed", "fixed_rate": 10}})
    early = await w.post(f"/contracts/{cid3}/escalation", {"decision": "approve"})
    assert (early.status_code, early.json()["error"]["message"]) == (
        409, "Onay bekleyen artış yok (artış tarihinden 30 gün önce açılır).",
    )  # fmt: skip


async def test_bedel_duzenlemesi_gecmise_yazilir(world: World) -> None:
    w = world
    cid = await contract(w)
    response = await w.api.patch(
        w.url(f"/contracts/{cid}"), json={"note": "Not"}, headers=w.headers
    )
    assert response.status_code == 200
    edited = await w.api.patch(
        w.url(f"/contracts/{cid}"), json={"amount": "22000.00"}, headers=w.headers
    )
    assert edited.json()["data"]["amount"] == "22000.00"
    history = (await w.get(f"/contracts/{cid}")).json()["amount_history"]
    assert [(h["amount"], h["effective_from"], h["source"]) for h in history] == [
        ("20000.00", "2025-09-01", "initial"),
        ("22000.00", "2026-06-20", "edit"),
    ]
    # Mayıs karnesi mayıs başındaki bedelle hesaplanır.
    await put(w, cid, {"kpis": [{"name": "Elle", "source": "manual", "target": 90, "weight": 100}]})
    card = (await w.get(f"/contracts/{cid}/scorecards/2026-05")).json()
    assert card["monthly_amount"] == "20000.00"


# --- Zamanlanmış iş ----------------------------------------------------------------------------


async def test_gunluk_is_taslak_artis_ve_yenileme(world: World) -> None:
    w = world
    cid = await contract(w, start_date="2025-07-10", end_date="2026-08-10")
    await put(w, cid, {**S1_RULES, "escalation": {"kind": "fixed", "fixed_rate": 10}})
    now = datetime(2026, 6, 20, 6, tzinfo=UTC)
    for expected in ((1, 1, 1), (0, 0, 0)):  # ikinci çalışma bir şey yazmaz
        with site_scope(w.site_id):
            async with w.factory() as session, session.begin():
                site = await session.get(Site, w.site_id)
                assert site is not None
                r = await svc.run_site(session, site, today=date(2026, 6, 20), now=now)
        assert (r.drafts, r.escalations, r.renewals) == expected
    card = (await w.get(f"/contracts/{cid}/scorecards/2026-05")).json()
    assert card["id"] is not None
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = set(await session.scalars(select(Notification.kind)))
            history = list(await session.scalars(select(ContractAmount.source)))
    assert {"contract.scorecard_ready", "contract.escalation_due", "contract.renewal"} <= kinds
    assert history == ["initial"]
    results = await svc.run_all(w.factory, today=date(2026, 6, 20), now=now)
    assert {r.site_id for r in results} == {w.site_id}


# --- KPI kaynakları: devriye, görev, SLA, teknik varlık muayenesi ------------------------


@pytest.fixture
async def sources(world: World, api_app: FastAPI, tmp_path: Path) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Kaynak", max_units=150, allowed_modules=["finance", "requests"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/requests/toggle")).status_code == 200
    api_app.dependency_overrides[get_file_store] = lambda: FileStore(tmp_path)
    return world


async def seed_sources(w: World) -> None:
    me = (await w.api.get("/api/v1/me", headers=w.headers)).json()
    may = datetime(2026, 5, 10, 9, tzinfo=UTC)
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            points = [
                PatrolPoint(name=f"N{i}", name_key=f"n{i}", tag="qr", code=f"10000{i}")
                for i in range(4)
            ]
            session.add_all(points)
            await session.flush()
            ids = [p.id for p in points]
            plan = PatrolPlan(name="Gece", point_ids=ids, start_times=["22:00"],
                              allowed_minutes=30)  # fmt: skip
            session.add(plan)
            await session.flush()
            run = PatrolRun(
                plan_id=plan.id, plan_name="Gece", point_ids=ids, ordered=True,
                allowed_minutes=30, run_date=date(2026, 5, 10), started_at=may,
                started_by="Bekçi", started_by_user_id=uuid.UUID(me["user_id"]),
            )  # fmt: skip
            session.add(run)
            await session.flush()
            session.add_all(
                [PatrolScan(run_id=run.id, point_id=i, scanned_at=may, scanned_by="Bekçi")
                 for i in ids[:3]]
            )  # fmt: skip
            task = TaskTemplate(
                name="Lobi temizliği", frequency="daily", weekdays=[], time_from=time(8),
                time_to=time(10), assignee="Temizlik", created_at=datetime(2026, 4, 1, tzinfo=UTC),
            )  # fmt: skip
            session.add(task)
            await session.flush()
            session.add_all(
                [TaskCompletion(template_id=task.id, task_date=date(2026, 5, d), status="done",
                                by_name="Temizlik") for d in range(1, 26)]
            )  # fmt: skip
            category = await session.scalar(select(RequestCategory.key).limit(1))
            assert category is not None
            for n, late in enumerate((False, False, False, True), start=1):
                session.add(
                    Request(
                        number=n, title=f"Arıza {n}", category=category, status="resolved",
                        resolution="Giderildi", created_at=may,
                        due_at=datetime(2026, 5, 11, tzinfo=UTC),
                        resolved_at=datetime(2026, 5, 12 if late else 10, 12, tzinfo=UTC),
                    )
                )  # fmt: skip


async def test_kpi_kaynaklari(sources: World) -> None:
    w = sources
    cid = await contract(w)
    await seed_sources(w)
    body = {
        "kpis": [
            {"id": "p", "name": "Devriye", "source": "patrol", "target": 95, "weight": 25},
            {"id": "t", "name": "Görev", "source": "tasks", "target": 90, "weight": 25},
            {"id": "s", "name": "SLA", "source": "sla", "target": 90, "weight": 25},
            {"id": "a", "name": "Muayene", "source": "asset_inspection", "target": 100,
             "weight": 25},
        ]
    }  # fmt: skip
    saved = await put(w, cid, body)
    assert saved.status_code == 200, saved.text
    rows = (await w.get(f"/contracts/{cid}/scorecards/2026-05")).json()["rows"]
    assert [(r["actual"], r["detail"]) for r in rows] == [
        ("75", "1 tur, 3/4 nokta"),
        ("81", "25/31 görev"),
        ("75", "3/4 talep SLA içinde"),
        (None, "Bu ay sözleşmeye bağlı varlık muayenesi yok — elle girin"),
    ]
    asset = await w.post("/technical-assets", {**ELEVATOR, "contract_id": cid})
    assert asset.status_code == 201, asset.text
    schedule = asset.json()["data"]["next_inspection"]["id"]
    recorded = await w.api.post(
        w.url(f"/asset-inspection-schedules/{schedule}/records"),
        data={"inspected_on": "2026-06-18", "result": "pass", "inspector": "Muayene A.Ş."},
        files={"report": ("rapor.pdf", b"%PDF-1.4 muayene", "application/pdf")},
        headers=w.headers,
    )
    assert recorded.status_code == 201, recorded.text
    june = (await w.get(f"/contracts/{cid}/scorecards/2026-06")).json()["rows"][3]
    assert (june["actual"], june["detail"], june["editable"]) == (
        "100",
        "1/1 muayene olumlu",
        False,
    )


async def test_komut_satiri(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    await contract(world)
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-contract-jobs"]) == 0
    finally:
        get_settings.cache_clear()
    assert "1 sitede sözleşme işi çalıştı." in capsys.readouterr().out
