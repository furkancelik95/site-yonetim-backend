# ruff: noqa: F811 — `ops` fikstürü test_operations modülünden alınır
"""Hizmetler ve talep akışı — HIZ-01..05, servis istekleri 47–52, K18/K26 (gerçek PostgreSQL).

Akış (K18), sakin onayı ve puan, iç not (K26), kategoriler ve SLA (D-26), ana olay, ücretli
hizmet, tadilat/taşınma; yetki, site izolasyonu ve zamanlanmış işler.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim import cli
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import LedgerEntry, Notification, Request, Site
from site_yonetim.services import service_desk
from site_yonetim.services.sites import set_module_enabled
from tests.integration.conftest import DatabaseUrls
from tests.integration.test_operations import (  # noqa: F401 — fikstür
    Ops,
    open_request,
    ops,
    resident,
)


async def put(o: Ops, path: str, body: object, headers: dict[str, str] | None = None) -> Any:
    return await o.api.put(o.url(path), json=body, headers=headers or o.manager)


async def patch(o: Ops, path: str, body: object, headers: dict[str, str] | None = None) -> Any:
    return await o.api.patch(o.url(path), json=body, headers=headers or o.manager)


async def status(
    o: Ops, rid: str, new: str, resolution: str | None = None, headers: dict[str, str] | None = None
) -> httpx2.Response:
    return await o.post(f"/requests/{rid}/status", {"status": new, "resolution": resolution},
                        headers)  # fmt: skip


async def to_resolved(o: Ops, rid: str) -> None:
    assert (
        await o.post(f"/requests/{rid}/assign", {"assignee": "Tesisatçı Ali"})
    ).status_code == 200
    assert (await status(o, rid, "in_progress")).status_code == 200
    assert (await status(o, rid, "resolved", "Conta değişti")).status_code == 200


async def notifications(o: Ops, kind: str) -> list[Notification]:
    with site_scope(o.site_id):
        async with o.factory() as session:
            rows = await session.scalars(select(Notification).where(Notification.kind == kind))
            return list(rows)


async def run_jobs(o: Ops, now: datetime) -> service_desk.JobResult:
    with site_scope(o.site_id):
        async with o.factory() as session, session.begin():
            site = await session.get(Site, o.site_id)
            assert site is not None
            return await service_desk.run_jobs(session, site, now=now)


# --- Akış (K18) ve kapanış onayı ----------------------------------------------------------


async def test_dokumandaki_akis_ve_sakin_onayi(ops: Ops) -> None:
    sakin = await resident(ops)
    created = await open_request(ops, sakin, unit_id=ops.ids["unit_A"], priority="urgent")
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    rid = data["id"]
    # Sakinde öncelik kategoriden gelir (aciliyet beyanı bilgi amaçlı); tesisat 7/24.
    assert (data["priority"], data["category_name"]) == ("normal", "Su / tesisat")
    assert data["sla"]["hour_type"] == "24x7"
    assert data["sla"]["target_hours"] == 72
    assert data["allowed_transitions"] == []  # sakinde durum değiştirme yok
    assert data["can_cancel"] is True

    skipped = await status(ops, rid, "in_progress")
    assert (skipped.status_code, skipped.json()["error"]["code"]) == (409, "invalid_transition")
    assert (await status(ops, rid, "assigned")).json()["error"]["code"] == "assignee_required"
    assigned = await ops.post(f"/requests/{rid}/assign", {"assignee": "Tesisatçı Ali"})
    body = assigned.json()["data"]
    assert (body["status"], body["allowed_transitions"]) == (
        "assigned",
        ["in_progress", "cancelled"],
    )
    await status(ops, rid, "in_progress")
    no_text = await status(ops, rid, "resolved")
    assert no_text.status_code == 422
    assert "resolution" in no_text.json()["error"]["fields"]
    resolved = (await status(ops, rid, "resolved", "Conta değişti")).json()["data"]
    assert resolved["auto_close_at"] is not None
    assert resolved["allowed_transitions"] == ["in_progress"]  # sakin onayı beklenir
    closing = await status(ops, rid, "closed")
    assert closing.json()["error"]["code"] == "awaiting_confirmation"

    score = await ops.post(f"/resident/requests/{rid}/confirm", {"solved": True}, sakin)
    assert score.json()["error"]["fields"] == {"score": "1–5 arası puan verin."}
    confirmed = await ops.post(
        f"/resident/requests/{rid}/confirm", {"solved": True, "score": 2, "comment": "Geç geldi"},
        sakin,
    )  # fmt: skip
    assert confirmed.status_code == 200, confirmed.text
    final = confirmed.json()["data"]
    assert (final["status"], final["feedback"]["score"]) == ("closed", 2)
    assert final["closed_at"] is not None
    assert [e["kind"] for e in final["events"]][-1] == "closed"
    [low] = await notifications(ops, "request.low_rating")  # puan ≤ 2 → yöneticiye
    assert low.title == "Düşük memnuniyet puanı: 2/5"
    again = await ops.post(f"/resident/requests/{rid}/confirm", {"solved": True, "score": 5}, sakin)
    assert again.json()["error"]["code"] == "not_resolved"
    reopen = await status(ops, rid, "in_progress")
    assert reopen.json()["error"]["code"] == "request_finished"
    # Sakine "talep alındı" ve durum bildirimleri gitti.
    assert {n.title for n in await notifications(ops, "request.status")} >= {
        "Talebiniz alındı: #1",
        "Talebiniz: Çözüldü",
    }


async def test_sakin_devam_ediyor_der_ve_iptal_eder(ops: Ops) -> None:
    sakin = await resident(ops)
    rid = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    await to_resolved(ops, rid)
    missing = await ops.post(f"/resident/requests/{rid}/confirm", {"solved": False}, sakin)
    assert missing.json()["error"]["fields"] == {"comment": "Sorunun neden devam ettiğini yazın."}
    reopened = await ops.post(
        f"/resident/requests/{rid}/confirm", {"solved": False, "comment": "Hâlâ damlatıyor"}, sakin
    )
    data = reopened.json()["data"]
    assert (data["status"], data["resolved_at"], data["auto_close_at"]) == ("assigned", None, None)
    assert data["feedback"]["solved"] is False
    assert len(await notifications(ops, "request.reopened")) >= 1
    # İş başladıysa sakin iptal edemez; başlamadıysa eder.
    await status(ops, rid, "in_progress")
    late = await ops.post(f"/resident/requests/{rid}/cancel", {"reason": None}, sakin)
    assert (late.status_code, late.json()["error"]["code"]) == (409, "not_cancellable")
    fresh = (await open_request(ops, sakin, title="Kapı zili")).json()["data"]["id"]
    cancelled = await ops.post(f"/resident/requests/{fresh}/cancel", {"reason": "Düzeldi"}, sakin)
    assert cancelled.json()["data"]["status"] == "cancelled"
    others = (await open_request(ops, title="Başkasının")).json()["data"]["id"]
    assert (await ops.post(f"/resident/requests/{others}/cancel", {}, sakin)).status_code == 404
    # Personel iptali gerekçe ister.
    staff = (await open_request(ops, title="Personel talebi")).json()["data"]["id"]
    assert (await status(ops, staff, "cancelled")).status_code == 422
    assert (await status(ops, staff, "cancelled", "Mükerrer")).json()["data"][
        "status"
    ] == "cancelled"


async def test_personelin_actigi_talebi_personel_kapatir(ops: Ops) -> None:
    rid = (await open_request(ops, title="Çatı oluğu tıkalı")).json()["data"]["id"]
    await to_resolved(ops, rid)
    closed = await status(ops, rid, "closed")
    assert closed.json()["data"]["status"] == "closed"


# --- İç not (K26) --------------------------------------------------------------------------


async def test_ic_not_sakine_hic_donmez(ops: Ops) -> None:
    sakin = await resident(ops)
    rid = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    empty = await ops.post(f"/requests/{rid}/internal-notes", {"body": " "})
    assert empty.json()["error"]["fields"] == {"body": "Not yazın."}
    note = await ops.post(f"/requests/{rid}/internal-notes", {"body": "GİZLİ-NOT: sakin agresif"})
    assert note.status_code == 201, note.text
    assert note.json()["message"] == "İç not eklendi; sakin görmez."
    listed = (await ops.get(f"/requests/{rid}/internal-notes")).json()
    assert [n["body"] for n in listed] == ["GİZLİ-NOT: sakin agresif"]
    assert (await ops.get(f"/requests/{rid}/internal-notes", sakin)).status_code == 403
    assert (
        await ops.post(f"/requests/{rid}/internal-notes", {"body": "x x"}, sakin)
    ).status_code == 403
    # Sakin kanalının hiçbir yerinde not metni yok.
    for path in (f"/requests/{rid}", "/resident/requests", "/resident/home"):
        assert "GİZLİ-NOT" not in (await ops.get(path, sakin)).text
    assert "GİZLİ-NOT" not in (await ops.get(f"/requests/{rid}")).text  # talep yanıtında da yok
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            with pytest.raises(DBAPIError, match="Geçmiş kaydı"):
                await session.execute(text("UPDATE request_internal_notes SET body = 'x'"))


# --- Kategoriler ve SLA (D-26, servis isteği 47) -------------------------------------------


async def test_kategoriler_ve_sla_ayarlari(ops: Ops) -> None:
    listed = (await ops.get("/request-categories")).json()
    keys = [c["key"] for c in listed]
    assert {"plumbing", "elevator", "flood", "gas_smell", "other"} <= set(keys)
    flood = next(c for c in listed if c["key"] == "flood")
    assert (flood["hour_type"], flood["default_priority"]) == ("24x7", "urgent")

    created = await ops.post(
        "/request-categories",
        {"name": "Çatı akıntısı", "hour_type": "24x7",
         "resolve_hours": {"low": 100, "normal": 48, "high": 12, "urgent": 2},
         "default_priority": "high", "department_id": None},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    category = created.json()["data"]
    assert category["key"] == "cati_akintisi"
    duplicate = await ops.post("/request-categories", {"name": "çatı AKINTISI"})
    assert duplicate.json()["error"]["fields"] == {"name": "Bu adla bir kategori zaten var."}
    bad = await ops.post(
        "/request-categories", {"name": "Kötü", "resolve_hours": {"low": 0, "normal": 1,
                                                                    "high": 1, "urgent": 1}}
    )  # fmt: skip
    assert bad.json()["error"]["fields"] == {"resolve_hours.low": "1–2160 saat."}

    request = (await open_request(ops, category="cati_akintisi", title="Tavan damlıyor")).json()
    data = request["data"]
    assert (data["priority"], data["sla"]["target_hours"]) == ("high", 12)
    due = datetime.fromisoformat(data["due_at"])
    assert due - datetime.fromisoformat(data["created_at"]) == timedelta(hours=12)

    settings = (await ops.get("/settings/requests")).json()
    assert settings["auto_close_days"] == 3
    assert settings["calendar"][6]["closed"] is True
    wrong = await put(ops, "/settings/requests", {**settings, "calendar": [
        {"closed": True, "open": "09:00", "close": "18:00"}] * 7, "auto_close_days": 0,
        "categories": {"elevator": {"hour_type": "24x7", "resolve_hours": {
            "low": 1, "normal": 1, "high": 1, "urgent": 3000}}}})  # fmt: skip
    assert wrong.json()["error"]["fields"] == {
        "categories.elevator.urgent": "1–2160 saat.",
        "calendar": "En az bir çalışma günü olmalı.",
        "auto_close_days": "1–30 gün.",
    }
    ok = await put(ops, "/settings/requests", {**settings, "auto_close_days": 5,
                   "categories": {"cati_akintisi": {"hour_type": "business", "resolve_hours": {
                       "low": 100, "normal": 48, "high": 6, "urgent": 2}}}})  # fmt: skip
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["categories"]["cati_akintisi"]["resolve_hours"]["high"] == 6
    # Açık talep açıldığı kuralla devam eder.
    again = (await ops.get(f"/requests/{data['id']}")).json()
    assert (again["sla"]["target_hours"], again["sla"]["hour_type"]) == (12, "24x7")

    off = await patch(ops, f"/request-categories/{category['id']}",
                      {"name": "Çatı akıntısı", "is_active": False})  # fmt: skip
    assert off.json()["data"]["is_active"] is False
    closed = await open_request(ops, category="cati_akintisi")
    assert closed.json()["error"]["fields"] == {
        "category": "Kategori bulunamadı ya da kullanımdan kaldırıldı."
    }
    sakin = await resident(ops)
    assert (await ops.get("/request-categories", sakin)).status_code == 200  # sakin formu
    tech = await ops.user("teknik@test.local", "Teknik Personel")
    assert (await put(ops, "/settings/requests", settings, tech)).status_code == 403
    assert (await ops.post("/request-categories", {"name": "Deneme"}, tech)).status_code == 403


async def test_sla_ve_otomatik_kapanis_isi(ops: Ops) -> None:
    rid = (await open_request(ops, category="elevator", priority="urgent")).json()["data"]["id"]
    sakin = await resident(ops)
    mine = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    await to_resolved(ops, mine)
    now = datetime.now(UTC)
    early = await run_jobs(ops, now)
    assert (early.closed, early.warned, early.breached) == (0, 0, 0)
    warn = await run_jobs(ops, now + timedelta(hours=3, minutes=20))  # 4 saatin %80'i geçti
    assert (warn.warned, warn.breached) == (1, 0)
    late = await run_jobs(ops, now + timedelta(hours=5))
    assert late.breached == 1
    assert len(await notifications(ops, "request.sla_warning")) >= 1
    assert len(await notifications(ops, "request.sla_breached")) == 1  # yalnız yönetici
    breached = (await ops.get("/requests", params={"sla_breached": True})).json()
    assert rid not in {r["id"] for r in breached["items"]}  # gerçek saatte süresi dolmadı
    closed = await run_jobs(ops, now + timedelta(days=3, hours=1))
    assert closed.closed == 1
    data = (await ops.get(f"/requests/{mine}")).json()
    assert data["status"] == "closed"
    assert "3 gün içinde yanıt vermediği" in data["events"][-1]["description"]


# --- Hazır yanıtlar (48) ------------------------------------------------------------------


async def test_hazir_yanitlar(ops: Ops) -> None:
    listed = (await ops.get("/reply-templates")).json()
    assert [t["title"] for t in listed] == ["Arıza giderildi", "Talep alındı"]  # site açılışı
    bad = await ops.post("/reply-templates", {"title": "", "body": "kısa", "category": "yok"})
    assert set(bad.json()["error"]["fields"]) == {"title", "body", "category"}
    created = await ops.post(
        "/reply-templates",
        {"title": "Asansör", "category": "elevator", "body": "Asansör firması yolda {{talep.no}}."},
    )
    assert created.status_code == 201
    tid = created.json()["data"]["id"]
    off = await patch(
        ops, f"/reply-templates/{tid}", {**created.json()["data"], "is_active": False}
    )
    assert off.json()["data"]["is_active"] is False
    sakin = await resident(ops)
    assert (await ops.get("/reply-templates", sakin)).status_code == 403


# --- Ana olay (49) ------------------------------------------------------------------------


async def test_ana_olay_birlestirme_ve_cozum(ops: Ops) -> None:
    sakin = await resident(ops)
    mehmet = await resident(ops, "mehmet", "mehmet@test.local")
    first = (await open_request(ops, sakin, title="Asansör durdu", category="elevator",
                                location="A blok asansör")).json()["data"]["id"]  # fmt: skip
    second = (await open_request(ops, mehmet, title="Asansör çalışmıyor", category="elevator",
                                 location="A blok asansör")).json()["data"]["id"]  # fmt: skip
    other = (await open_request(ops, title="Lamba", category="electrical",
                                location="A blok asansör")).json()["data"]["id"]  # fmt: skip
    suggest = (await ops.get(f"/requests/{first}/merge-suggestions")).json()
    assert [s["id"] for s in suggest["similar"]] == [second]  # kategori farklı olan gelmez
    assert suggest["window_hours"] == 24
    assert suggest["linked_outage"] is None

    bad = await ops.post("/outages", {"title": "", "category": "elevator",
                                      "public_description": "kısa"})  # fmt: skip
    assert set(bad.json()["error"]["fields"]) == {"title", "public_description"}
    created = await ops.post(
        "/outages",
        {"title": "A blok asansör arızası", "category": "elevator", "location": "A blok",
         "public_description": "Asansör firması çağrıldı; akşama kadar çalışır.",
         "request_ids": [first, second]},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    outage = created.json()["data"]
    assert set(outage["request_ids"]) == {first, second}
    third = (await open_request(ops, title="Asansör sesi", category="elevator",
                                location="A blok asansör")).json()["data"]["id"]  # fmt: skip
    suggest = (await ops.get(f"/requests/{third}/merge-suggestions")).json()
    assert [o["id"] for o in suggest["open_outages"]] == [outage["id"]]
    again = await ops.post(f"/outages/{outage['id']}/links", {"request_id": first})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_linked")
    linked = await ops.post(f"/outages/{outage['id']}/links", {"request_id": third})
    assert len(linked.json()["data"]["request_ids"]) == 3
    unlinked = await ops.post(f"/outages/{outage['id']}/unlink", {"request_id": third})
    assert len(unlinked.json()["data"]["request_ids"]) == 2

    # Sakin ana olayın genel açıklamasını görür, diğer sakinin talebini görmez.
    mine = (await ops.get("/resident/requests", sakin)).json()["items"]
    assert [r["outage"]["title"] for r in mine] == ["A blok asansör arızası"]
    assert "Asansör çalışmıyor" not in (await ops.get("/resident/requests", sakin)).text

    resolved = await ops.post(f"/outages/{outage['id']}/resolve", {"resolution": "Motor değişti"})
    assert resolved.json()["message"] == (
        'Ana olay çözüldü; 2 bağlı talep "çözüldü" oldu ve sakinlere bildirildi.'
    )
    for rid in (first, second):
        data = (await ops.get(f"/requests/{rid}")).json()
        assert (data["status"], data["resolution"]) == ("resolved", "Motor değişti")
    assert (await ops.get(f"/requests/{other}")).json()["status"] == "open"
    late = await ops.post(f"/outages/{outage['id']}/links", {"request_id": third})
    assert late.json()["error"]["code"] == "outage_resolved"
    page = (await ops.get("/outages")).json()
    assert page["total"] == 1
    assert page["items"][0]["status"] == "resolved"
    resolved_notes = [n for n in await notifications(ops, "request.status")
                      if n.title == "Talebiniz: Çözüldü"]  # fmt: skip
    assert len(resolved_notes) == 2  # her sakine


# --- Ücretli hizmet (51) ------------------------------------------------------------------


async def test_ucretli_hizmet_teklif_onay_ve_ek_tutar(ops: Ops) -> None:
    sakin = await resident(ops)
    common = (await open_request(ops, sakin, title="Otopark")).json()["data"]["id"]
    lines = [{"description": "Batarya", "qty": "1", "unit_price": "650.00"},
             {"description": "İşçilik", "qty": "0.5", "unit_price": "400.00"}]  # fmt: skip
    no_unit = await ops.post(f"/requests/{common}/quotes", {"lines": lines})
    assert no_unit.json()["error"]["code"] == "not_unit_request"
    rid = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    empty = await ops.post(f"/requests/{rid}/quotes", {"lines": []})
    assert empty.json()["error"]["fields"] == {"lines": "En az bir kalem girin."}
    quote = await ops.post(f"/requests/{rid}/quotes", {"lines": lines, "note": "Mutfak"})
    assert quote.status_code == 201, quote.text
    q = quote.json()["data"]
    assert (q["total"], q["status"]) == ("850.00", "pending")
    assert (await ops.post(f"/requests/{rid}/quotes", {"lines": lines})).json()["error"][
        "code"
    ] == "quote_open"
    seen = (await ops.get("/resident/requests", sakin)).json()["items"]
    assert (
        next(r for r in seen if r["id"] == rid)["quote"]["status_label"] == "Sakin onayı bekliyor"
    )
    early = await ops.post(f"/quotes/{q['id']}/complete", {"actual_total": "900.00"})
    assert early.json()["error"]["code"] == "invalid_status"
    stranger = await resident(ops, "mehmet", "mehmet@test.local")
    denied = await ops.post(
        f"/resident/quotes/{q['id']}/decision", {"decision": "accept"}, stranger
    )
    assert denied.status_code == 404
    accepted = await ops.post(f"/resident/quotes/{q['id']}/decision", {"decision": "accept"}, sakin)
    assert accepted.json()["data"]["status"] == "accepted"
    # Servis isteği 51 kabul: 850 teklif, 1.500 gerçekleşen, sakin ek tutarı reddeder → 850.
    extra = await ops.post(f"/quotes/{q['id']}/complete", {"actual_total": "1500.00"})
    assert extra.json()["data"]["status"] == "extra_pending"
    rejected = await ops.post(f"/resident/quotes/{q['id']}/decision", {"decision": "reject"}, sakin)
    final = rejected.json()["data"]
    assert (final["status"], final["billed_total"]) == ("billed", "850.00")
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            [entry] = await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "service")
            )
    assert entry.debit == Decimal("850.00")
    assert entry.source_id == uuid.UUID(q["id"])
    twice = await ops.post(f"/resident/quotes/{q['id']}/decision", {"decision": "accept"}, sakin)
    assert twice.json()["error"]["code"] == "invalid_status"


async def test_sakin_teklifi_reddederse_ucret_olusmaz(ops: Ops) -> None:
    sakin = await resident(ops)
    rid = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    q = (await ops.post(f"/requests/{rid}/quotes", {"lines": [
        {"description": "İş", "qty": "1", "unit_price": "100.00"}]})).json()["data"]  # fmt: skip
    rejected = await ops.post(f"/resident/quotes/{q['id']}/decision", {"decision": "reject"}, sakin)
    assert rejected.json()["data"]["status"] == "rejected"
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            assert not list(
                await session.scalars(select(LedgerEntry).where(LedgerEntry.source == "service"))
            )
    # Reddedilen tekliften sonra yeni teklif verilebilir.
    assert (await ops.post(f"/requests/{rid}/quotes", {"lines": [
        {"description": "İş", "qty": "1", "unit_price": "80.00"}]})).status_code == 201  # fmt: skip


async def test_tarife_onay_kuraliyla(ops: Ops) -> None:
    created = await ops.post("/service-tariffs", {"name": "İşçilik", "unit": "saat",
                                                  "price": "400.00"})  # fmt: skip
    assert (created.status_code, created.json()["data"]["status"]) == (201, "saved")
    tid = created.json()["data"]["tariff"]["id"]
    bad = await ops.post("/service-tariffs", {"name": "", "unit": "", "price": "0.00"})
    assert set(bad.json()["error"]["fields"]) == {"name", "unit", "price"}
    board = await ops.user("yk@test.local", "Yönetim Kurulu Üyesi")
    rule = await ops.post("/approval-rules", {
        "action_type": "paid_service_tariff",
        "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
        "remind_after_days": 2, "escalate_after_days": 5, "effective_from": "2026-10-01",
    })  # fmt: skip
    assert rule.status_code == 201, rule.text
    pending = await patch(ops, f"/service-tariffs/{tid}", {"name": "İşçilik", "unit": "saat",
                                                           "price": "450.00"})  # fmt: skip
    assert pending.status_code == 202, pending.text
    assert pending.json()["data"]["status"] == "approval_pending"
    still = (await ops.get("/service-tariffs")).json()
    assert [t["price"] for t in still] == ["400.00"]
    approval = pending.json()["data"]["approval_request_id"]
    decided = await ops.post(f"/approvals/{approval}/approve", {}, board)
    assert decided.json()["data"]["status"] == "executed", decided.text
    assert [t["price"] for t in (await ops.get("/service-tariffs")).json()] == ["450.00"]


# --- Tadilat ve taşınma (52) --------------------------------------------------------------


async def test_tadilat_basvuru_onay_depozito_ve_iade(ops: Ops) -> None:
    sakin = await resident(ops, "ayse", "ayse@test.local")  # A-1 maliki
    rules = await put(ops, "/settings/permits", {
        "weekday_hours": "09:00-18:00", "saturday_hours": "", "sunday_allowed": False,
        "deposit_amount": "1000.00", "checklist": ["Moloz çıkarıldı", " "]})  # fmt: skip
    assert rules.status_code == 200, rules.text
    assert (rules.json()["data"]["saturday_hours"], rules.json()["data"]["checklist"]) == (
        None, ["Moloz çıkarıldı"],
    )  # fmt: skip
    bad_rules = await put(ops, "/settings/permits", {"weekday_hours": "18:00-09:00"})
    assert bad_rules.json()["error"]["fields"] == {"weekday_hours": "SS:DD-SS:DD biçiminde."}
    past = await ops.post("/resident/permits", {"type": "renovation", "date_from": "2026-09-01",
                          "date_to": "2026-09-02", "description": "Banyo"}, sakin)  # fmt: skip
    assert set(past.json()["error"]["fields"]) == {"date_from"}
    foreign = await ops.post("/resident/permits", {"unit_id": ops.ids["unit_B"],
                             "type": "move_in", "date_from": "2026-10-05",
                             "description": "Nakliye sabah"}, sakin)  # fmt: skip
    assert foreign.json()["error"]["code"] == "unit_not_yours"
    applied = await ops.post("/resident/permits", {
        "type": "renovation", "date_from": "2026-10-05", "date_to": "2026-10-12",
        "contractor": "Usta Yapı", "description": "Banyo yenileme"}, sakin)  # fmt: skip
    assert applied.status_code == 201, applied.text
    pid = applied.json()["data"]["id"]
    mine = (await ops.get("/resident/permits", sakin)).json()
    assert (mine["rules"]["deposit_amount"], len(mine["items"])) == ("1000.00", 1)
    pending = (await ops.get("/permits", params={"status": "pending"})).json()
    assert pending["total"] == 1

    approved = await ops.post(f"/permits/{pid}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["permit"]["deposit"] == "1000.00"
    twice = await ops.post(f"/permits/{pid}/approve")
    assert twice.json()["error"]["code"] == "invalid_status"
    no_list = await ops.post(f"/permits/{pid}/complete", {"checklist": [], "refund_deposit": True})
    assert no_list.json()["error"]["fields"] == {"checklist": "Kontrol listesini doldurun."}
    done = await ops.post(
        f"/permits/{pid}/complete",
        {"checklist": [{"item": "Moloz çıkarıldı", "ok": True}], "refund_deposit": True},
    )
    assert done.json()["data"]["status"] == "completed"
    assert done.json()["data"]["deposit_refunded"] is True
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            entries = list(await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "deposit")
                .order_by(LedgerEntry.created_at)))  # fmt: skip
    assert [(e.debit, e.credit) for e in entries] == [
        (Decimal("1000.00"), Decimal("0.00")), (Decimal("0.00"), Decimal("1000.00")),
    ]  # fmt: skip
    assert entries[1].reversal_of_entry_id == entries[0].id
    assert len(await notifications(ops, "permit.status")) >= 1


async def test_tasinma_ret_ve_onay_kurali(ops: Ops) -> None:
    sakin = await resident(ops)
    body = {"type": "move_in", "date_from": "2026-10-03", "description": "Nakliye 09:00"}
    first = (await ops.post("/resident/permits", body, sakin)).json()["data"]["id"]
    no_reason = await ops.post(f"/permits/{first}/reject", {"reason": "yok"})
    assert no_reason.json()["error"]["fields"] == {"reason": "Ret gerekçesi yazın."}
    rejected = await ops.post(f"/permits/{first}/reject", {"reason": "Asansör bakımda"})
    assert (rejected.json()["data"]["status"], rejected.json()["data"]["reject_reason"]) == (
        "rejected", "Asansör bakımda",
    )  # fmt: skip

    board = await ops.user("yk@test.local", "Yönetim Kurulu Üyesi")
    await ops.post("/approval-rules", {
        "action_type": "renovation_permit",
        "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
        "remind_after_days": 2, "escalate_after_days": 5, "effective_from": "2026-10-01",
    })  # fmt: skip
    second = (await ops.post("/resident/permits", body, sakin)).json()["data"]["id"]
    sent = await ops.post(f"/permits/{second}/approve")
    assert sent.status_code == 202, sent.text
    data = sent.json()["data"]
    assert (data["status"], data["permit"]["status"]) == ("approval_pending", "pending")
    again = await ops.post(f"/permits/{second}/approve")
    assert again.json()["error"]["code"] == "approval_pending"
    decided = await ops.post(f"/approvals/{data['approval_request_id']}/approve", {}, board)
    assert decided.json()["data"]["status"] == "executed", decided.text
    listed = (await ops.get("/permits", params={"status": "approved"})).json()["items"]
    assert [p["id"] for p in listed] == [second]
    assert listed[0]["approval_status"] == "executed"
    done = await ops.post(f"/permits/{second}/complete", {"checklist": []})
    assert done.json()["data"]["status"] == "completed"  # giriş taşınması: liste gerekmez


# --- Yetki, modül, izolasyon --------------------------------------------------------------


async def test_yetki_modul_ve_site_izolasyonu(ops: Ops) -> None:
    rid = (await open_request(ops, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    outage = (
        await ops.post(
            "/outages",
            {
                "title": "Su kesintisi",
                "category": "plumbing",
                "public_description": "Ana boru patladı, ekip çalışıyor.",
            },
        )
    ).json()["data"]
    guard = await ops.user("guvenlik@test.local", "Güvenlik")
    assert (await ops.get("/permits", guard)).status_code == 403
    assert (await ops.post("/outages", {}, guard)).status_code in (403, 422)
    assert (await ops.post(f"/outages/{outage['id']}/resolve", {"resolution": "Tamir"},
                           guard)).status_code == 403  # fmt: skip
    # Başka sitenin yöneticisi bu kayıtları göremez.
    other = await create_other_manager(ops)
    yildiz = "yildiz-sitesi"
    assert (await ops.api.get(ops.url(f"/requests/{rid}/internal-notes", yildiz),
                              headers=other)).status_code == 404  # fmt: skip
    resolve = await ops.api.post(ops.url(f"/outages/{outage['id']}/resolve", yildiz),
                                 json={"resolution": "Tamir edildi"}, headers=other)  # fmt: skip
    assert resolve.status_code == 404
    listed = (await ops.api.get(ops.url("/outages", yildiz), headers=other)).json()
    assert listed["total"] == 0
    cats = (await ops.api.get(ops.url("/request-categories", yildiz), headers=other)).json()
    assert len(cats) >= 10  # kendi varsayılanları
    # Modül kapalıysa uçlar 404.
    async with ops.factory() as session, session.begin():
        site = await session.get(Site, ops.site_id)
        assert site is not None
        with site_scope(site.id):
            await set_module_enabled(session, site, ModuleKey.REQUESTS, enable=False)
    assert (await ops.get("/outages")).status_code == 404
    assert (await ops.get("/settings/requests")).status_code == 404
    with site_scope(ops.site_id):
        async with ops.factory() as session:
            assert await session.get(Request, uuid.UUID(rid)) is not None


async def create_other_manager(ops: Ops) -> dict[str, str]:
    from tests.integration.helpers import add_site_membership, create_user, login_headers

    user = await create_user(ops.factory, "diger@test.local")
    await add_site_membership(ops.factory, ops.other_site_id, user.id, "Yönetici")
    return await login_headers(ops.api, "diger@test.local")


async def test_komut_satiri_talep_isleri(
    ops: Ops,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    sakin = await resident(ops)
    rid = (await open_request(ops, sakin, unit_id=ops.ids["unit_A"])).json()["data"]["id"]
    await to_resolved(ops, rid)
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-request-jobs"]) == 0
    finally:
        get_settings.cache_clear()
    assert "sitede talep işleri çalıştı" in capsys.readouterr().out
