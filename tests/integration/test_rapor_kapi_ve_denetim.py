"""Test raporu (10.11.2026) — kapı tarafı: kargo bildirimi (#6, GUV-05 adım 3), teslim kodu
deneme sınırı ve denetim kaydı (#16, #3), güvenliğin kaydettiği ziyaretçinin sakin geçmişinde
görünmesi, denetçide denetim kaydı maskelemesi (#14), uzun bekleyen kargo hatırlatmasının 200
kargoyla sınırlı kalmaması."""

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, Package
from site_yonetim.services import security_jobs
from tests.integration.gatehouse_world import TZ, GateWorld


async def _audit(gw: GateWorld, entity: str) -> list[AuditLog]:
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            rows = await session.scalars(
                select(AuditLog).where(AuditLog.entity == entity).order_by(AuditLog.created_at)
            )
            return list(rows)


async def _package(gw: GateWorld, carrier: str = "Aras") -> dict[str, Any]:
    created = await gw.post("/packages", gw.guard, {"unit_id": gw.w.unit_id, "carrier": carrier})
    assert created.status_code == 201, created.text
    data: dict[str, Any] = created.json()["data"]
    return data


async def _code(gw: GateWorld, package_id: str) -> str:
    mine = (await gw.get("/resident/packages", gw.resident)).json()
    return str(next(p["pickup_code"] for p in mine if p["id"] == package_id))


async def test_kargo_gelince_sakine_kodsuz_bildirim_ve_denetim(gw: GateWorld) -> None:
    package = await _package(gw)
    code = await _code(gw, package["id"])
    items = (await gw.get("/notifications", gw.resident)).json()["items"]
    [received] = [n for n in items if n["kind"] == "package.received"]
    assert received["title"] == "Kargonuz geldi"
    assert code not in (received["body"] or "")  # kod bildirim/SMS metnine girmez
    assert "package.received" not in await gw.kinds(gw.neighbour)  # başka bölüm
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            row = await session.get(Package, package["id"])
            assert row is not None
            assert (row.notification_sent, row.notification_channel) == (True, "in_app")
    [created] = await _audit(gw, "packages")
    assert created.action == "create"
    assert created.after == {"unit_id": gw.w.unit_id, "carrier": "Aras", "status": "waiting"}
    assert created.actor_name == "Ali GÖREVLİ"


async def test_teslim_kodu_bes_hatada_on_bes_dakika_kilitlenir(gw: GateWorld) -> None:
    package = await _package(gw)
    other = await _package(gw, "MNG")
    code = await _code(gw, package["id"])
    wrong = "1000" if code != "1000" else "1001"
    deliver = f"/packages/{package['id']}/deliver"
    body = {"pickup_code": wrong, "delivered_to": "Ayşe Yılmaz"}
    for _ in range(4):
        response = await gw.post(deliver, gw.guard, body)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "wrong_pickup_code"
    locked = await gw.post(deliver, gw.guard, body)  # 5. hatalı deneme kilitler
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "pickup_code_locked"
    assert locked.headers["Retry-After"] == "900"
    # Kilitliyken doğru kod da reddedilir; kilit kargo başınadır.
    right = {"pickup_code": code, "delivered_to": "Ayşe Yılmaz"}
    assert (await gw.post(deliver, gw.guard, right)).status_code == 429
    other_code = await _code(gw, other["id"])
    other_ok = await gw.post(
        f"/packages/{other['id']}/deliver",
        gw.guard,
        {"pickup_code": other_code, "delivered_to": "Ayşe Yılmaz"},
    )
    assert other_ok.status_code == 200, other_ok.text

    gw.later(minutes=16)
    delivered = await gw.post(deliver, gw.guard, right)
    assert delivered.status_code == 200, delivered.text
    attempts = [a.after for a in await _audit(gw, "packages") if (a.after or {}).get("event")]
    assert [a["attempt"] for a in attempts if a is not None] == [1, 2, 3, 4, 5]
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            row = await session.get(Package, package["id"])
            assert row is not None
            assert (row.code_failed_attempts, row.code_locked_until) == (0, None)


async def test_guvenligin_kaydettigi_ziyaretci_sakin_gecmisinde(gw: GateWorld) -> None:
    recorded = await gw.post(
        "/visitors",
        gw.guard,
        {"unit_id": gw.w.unit_id, "full_name": "Can Misafir", "plate_number": "34 ABC 123"},
    )
    assert recorded.status_code == 201, recorded.text
    mine = (await gw.get("/resident/visitors", gw.resident)).json()
    assert [(v["full_name"], v["unit_name"], v["status"]) for v in mine] == [
        ("Can Misafir", "A-1", "entered")
    ]
    assert mine[0]["plate"] is not None
    assert "phone" not in mine[0]
    assert (await gw.get("/resident/visitors", gw.neighbour)).json() == []
    assert (await gw.get("/resident/visitors", gw.guard)).status_code == 403
    [row] = await _audit(gw, "visitors")
    assert row.after == {"unit_id": gw.w.unit_id, "kind": "guest", "status": "entered"}


async def test_denetci_denetim_kaydinda_ad_ve_ip_gormez(gw: GateWorld) -> None:
    await _package(gw)
    auditor = await gw.member("denetci@test.local", "Denetçi", "Deniz DENETÇİ")
    manager_rows = (await gw.get("/audit", gw.manager, params={"entity": "packages"})).json()[
        "items"
    ]
    auditor_rows = (await gw.get("/audit", auditor, params={"entity": "packages"})).json()["items"]
    assert manager_rows[0]["actor_name"] == "Ali GÖREVLİ"
    assert auditor_rows[0]["actor_name"] == "***"
    assert auditor_rows[0]["ip"] in (None, "***")
    assert auditor_rows[0]["user_agent"] in (None, "***")
    assert auditor_rows[0]["after"] == manager_rows[0]["after"]  # ad/plaka yok → aynı
    assert auditor_rows[0]["user_id"] == manager_rows[0]["user_id"]


async def test_uzun_bekleyen_hatirlatma_200_kargoyla_sinirli_degil(
    gw: GateWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    for carrier in ("Aras", "MNG", "PTT"):
        await _package(gw, carrier)
    monkeypatch.setattr(security_jobs, "REMIND_BATCH", 2)  # sayfa sınırını küçült
    results = await security_jobs.run_daily(gw.w.factory, now=datetime(2026, 7, 2, 3, 0, tzinfo=TZ))
    mine = next(r for r in results if r.site_id == gw.w.site_id)
    assert mine.reminded == 3
