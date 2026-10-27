"""Nöbet defteri ve devriye — servis istekleri 45, 46 (gerçek PostgreSQL). Kurgu: `gatehouse_world`
(bugün 20.06.2026, saat elle ilerletilir)."""

from datetime import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import ShiftLogEntry, Site
from site_yonetim.services import patrol as patrol_svc
from site_yonetim.services import security_jobs
from tests.integration.gatehouse_world import TZ, GateWorld

# --- 45: nöbet defteri --------------------------------------------------------------------


async def test_nobet_defteri_ve_vardiya_devri(gw: GateWorld) -> None:
    short = await gw.post("/shift-log", gw.guard, {"kind": "note", "text": "kısa"})
    assert short.status_code == 422
    assert short.json()["error"]["fields"] == {"text": "En az 5 karakter yazın."}
    bad_kind = await gw.post("/shift-log", gw.guard, {"kind": "x", "text": "Yeterince uzun"})
    assert set(bad_kind.json()["error"]["fields"]) == {"kind"}

    note = (
        await gw.post("/shift-log", gw.guard, {"kind": "note", "text": "Otopark lambası yanmıyor"})
    ).json()
    assert note["message"].startswith("Nöbet defterine yazıldı")
    handover = await gw.post(
        "/shift-log", gw.guard, {"kind": "handover", "text": "Gece sakin geçti, B kapısı kilitli."}
    )
    assert handover.status_code == 201
    entry = handover.json()["data"]
    assert (entry["by"], entry["by_me"], entry["acknowledged_by"]) == ("Ali GÖREVLİ", True, None)

    own = await gw.post(f"/shift-log/{entry['id']}/acknowledge", gw.guard)
    assert own.status_code == 409
    assert own.json()["error"]["code"] == "same_person"
    gw.later(hours=8)
    ack = await gw.post(f"/shift-log/{entry['id']}/acknowledge", gw.guard2)
    assert ack.status_code == 200, ack.text
    assert ack.json()["data"]["acknowledged_by"] == "Veli NÖBETÇİ"
    again = await gw.post(f"/shift-log/{entry['id']}/acknowledge", gw.manager)
    assert again.json()["error"]["code"] == "already_acknowledged"
    not_handover = await gw.post(f"/shift-log/{note['data']['id']}/acknowledge", gw.guard2)
    assert not_handover.status_code == 404

    added = await gw.post(
        f"/shift-log/{note['data']['id']}/addenda", gw.guard2, {"text": "Lamba değişti"}
    )
    assert [a["text"] for a in added.json()["data"]["addenda"]] == ["Lamba değişti"]
    assert (
        await gw.post(f"/shift-log/{note['data']['id']}/addenda", gw.guard2, {"text": ""})
    ).status_code == 422

    day = (await gw.get("/shift-log", gw.manager, params={"date": "2026-06-20"})).json()
    assert [e["kind"] for e in day] == ["handover", "note"]
    assert day[1]["by_me"] is False
    assert (await gw.get("/shift-log", gw.manager, params={"date": "2026-06-21"})).json() == []

    # değişmez: veritabanı güncellemeyi reddeder
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            with pytest.raises(DBAPIError, match="değiştirilemez"):
                await session.execute(update(ShiftLogEntry).values(text="Sonradan düzeltilmiş"))

    assert (await gw.get("/shift-log", gw.resident)).status_code == 403
    accountant = await gw.member("muhasebe@test.local", "Muhasebe", "Mu HASEBE")
    assert (await gw.get("/shift-log", accountant)).status_code == 403
    other = await gw.api.get(gw.url("/shift-log", "yildiz-sitesi"), headers=gw.manager)
    assert other.json() == []


# --- 46: devriye --------------------------------------------------------------------------


async def _points(gw: GateWorld, *names: str) -> list[dict[str, object]]:
    out = []
    for name in names:
        created = await gw.post(
            "/patrol-points", gw.manager, {"name": name, "tag": "qr", "area": "A"}
        )
        assert created.status_code == 201, created.text
        out.append(created.json()["data"])
    return out


async def test_devriye_kurulumu_dogrulama_ve_yetki(gw: GateWorld) -> None:
    p1, p2 = await _points(gw, "Otopark girişi", "Havuz")
    assert len(str(p1["code"])) == 6 and p1["code"] != p2["code"]  # noqa: PT018
    dup = await gw.post("/patrol-points", gw.manager, {"name": "OTOPARK GİRİŞİ", "tag": "nfc"})
    assert dup.json()["error"]["fields"] == {"name": "Bu adla nokta var."}
    assert (
        await gw.post("/patrol-points", gw.guard, {"name": "X", "tag": "qr"})
    ).status_code == 403
    assert len((await gw.get("/patrol-points", gw.guard)).json()) == 2  # görevli okur

    bad = await gw.post(
        "/patrol-plans",
        gw.manager,
        {"name": "", "point_ids": [], "start_times": ["25:00"], "allowed_minutes": 2},
    )
    assert set(bad.json()["error"]["fields"]) == {
        "name",
        "point_ids",
        "start_times",
        "allowed_minutes",
    }
    plan = await gw.post(
        "/patrol-plans",
        gw.manager,
        {
            "name": "Gece turu",
            "point_ids": [p2["id"], p1["id"]],
            "start_times": ["22:00", "01:00", "22:00"],
            "allowed_minutes": 30,
            "assignee": "Güvenlik",
        },
    )
    assert plan.status_code == 201, plan.text
    assert plan.json()["data"]["start_times"] == ["01:00", "22:00"]
    edited = await gw.patch(
        f"/patrol-plans/{plan.json()['data']['id']}",
        gw.manager,
        {**plan.json()["data"], "is_active": False},
    )
    assert edited.json()["data"]["is_active"] is False
    off = await gw.patch(
        f"/patrol-points/{p1['id']}",
        gw.manager,
        {"name": "Otopark girişi", "tag": "qr", "is_active": False},
    )
    assert (off.json()["data"]["is_active"], off.json()["data"]["code"]) == (False, p1["code"])
    other = await gw.api.get(gw.url("/patrol-points", "yildiz-sitesi"), headers=gw.manager)
    assert other.json() == []
    cross = await gw.api.patch(
        gw.url(f"/patrol-points/{p1['id']}", "yildiz-sitesi"),
        headers=gw.manager,
        json={"name": "X", "tag": "qr"},
    )
    assert cross.status_code == 404
    assert (await gw.get("/patrol-schedule", gw.resident)).status_code == 403


async def test_devriye_yurutme_sapma_raporu_ve_takip(gw: GateWorld) -> None:
    p1, p2, p3, outside = await _points(gw, "Nokta 1", "Nokta 2", "Nokta 3", "Dış nokta")
    plan = (
        await gw.post(
            "/patrol-plans",
            gw.manager,
            {
                "name": "Sabah turu",
                "point_ids": [p1["id"], p2["id"], p3["id"]],
                "ordered": True,
                "start_times": ["10:00", "22:00"],
                "allowed_minutes": 30,
            },
        )
    ).json()["data"]

    gw.at(10, 5)
    slots = (await gw.get("/patrol-schedule", gw.guard)).json()
    assert [(s["slot"], s["status"]) for s in slots] == [("10:00", "due"), ("22:00", "upcoming")]
    gw.at(10, 20)
    slots = (await gw.get("/patrol-schedule", gw.guard)).json()
    assert slots[0]["status"] == "not_started"

    # takip işi: başlamayan tura yöneticiye bir kez bildirim
    async def check() -> tuple[int, int]:
        with site_scope(gw.w.site_id):
            async with gw.w.factory() as session, session.begin():
                site = await session.get(Site, gw.w.site_id)
                assert site is not None
                return await patrol_svc.check_site(session, site, now=gw.clock[0])

    assert await check() == (1, 0)
    assert await check() == (0, 0)
    assert "patrol.not_started" in await gw.kinds(gw.manager)

    started = await gw.post("/patrol-runs", gw.guard, {"plan_id": plan["id"]})
    assert started.status_code == 201, started.text
    run = started.json()["data"]
    assert (run["slot"], run["report"]["late_start_minutes"], run["report"]["total_points"]) == (
        "10:00",
        20,
        3,
    )
    second = await gw.post("/patrol-runs", gw.guard2, {"plan_id": plan["id"]})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "run_open"

    gw.later(minutes=3)
    first = await gw.post(
        f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": p2["code"], "note": "Kapı açık"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["message"] == "Nokta 2 okutuldu — sıra dışı. (1/3)"
    unknown = await gw.post(f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": "12"})
    assert unknown.json()["error"]["fields"] == {"code": "Bu kodla nokta yok."}
    foreign = await gw.post(f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": outside["code"]})
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (409, "not_in_plan")
    dup = await gw.post(f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": p2["code"]})
    assert dup.json()["error"]["message"] == "Nokta 2 zaten okutuldu."
    in_order = await gw.post(f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": p1["code"]})
    assert in_order.json()["data"]["scans"][-1]["out_of_order"] is False

    gw.later(minutes=10)
    finished = await gw.post(f"/patrol-runs/{run['id']}/finish", gw.guard)
    assert finished.status_code == 200, finished.text
    report = finished.json()["data"]["report"]
    assert report == {
        "missed": ["Nokta 3"],
        "out_of_order": ["Nokta 2"],
        "duration_minutes": 13,
        "overtime": False,
        "late_start_minutes": 20,
        "total_points": 3,
        "scanned_points": 2,
        "ok": False,
    }
    assert "1 nokta okutulmadı" in finished.json()["message"]
    assert "patrol.finished" in await gw.kinds(gw.manager)
    after = await gw.post(f"/patrol-runs/{run['id']}/scans", gw.guard, {"code": p3["code"]})
    assert after.json()["error"]["code"] == "run_finished"
    again = await gw.post(f"/patrol-runs/{run['id']}/finish", gw.guard)
    assert again.status_code == 409

    slots = (await gw.get("/patrol-schedule", gw.manager, params={"date": "2026-06-20"})).json()
    assert slots[0]["status"] == "done_with_deviation"
    runs = (await gw.get("/patrol-runs", gw.manager)).json()
    assert [(r["plan_name"], r["started_by"]) for r in runs] == [("Sabah turu", "Ali GÖREVLİ")]

    # gece turu: zamanında başlar ama süreyi aşar → `overdue` ve bildirim (bir kez)
    gw.at(22, 1)
    night = (await gw.post("/patrol-runs", gw.guard, {"plan_id": plan["id"]})).json()["data"]
    assert night["slot"] == "22:00"
    gw.at(22, 40)
    assert (await gw.get("/patrol-schedule", gw.guard)).json()[1]["status"] == "overdue"
    results = await security_jobs.run_patrol_checks(
        gw.w.factory, now=datetime(2026, 6, 20, 22, 40, tzinfo=TZ)
    )
    assert (gw.w.site_id, 0, 1) in results
    assert "patrol.overdue" in await gw.kinds(gw.manager)
    done = await gw.post(f"/patrol-runs/{night['id']}/finish", gw.guard)
    assert done.json()["data"]["report"]["overtime"] is True
