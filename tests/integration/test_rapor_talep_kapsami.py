"""Test raporu (10.11.2026) — talep tarafı: teknik personel kapsamı (#8), atama `assignee_id`,
açılış kanalı ve sakin onayı (K18), atama bildirimi (#6), denetim kaydı (#3), okuma onaylı duyuru
bildirimi (#6). Ürün Dokümanı 5.2: Teknik "Kısıtlı: atananlar"."""

import uuid
from typing import Any

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, User
from tests.integration.helpers import create_user
from tests.integration.test_operations import Ops, open_request, ops, resident

__all__ = ["ops"]


async def _user_id(o: Ops, email: str) -> str:
    async with o.factory() as session:
        user = await session.scalar(select(User).where(User.email == email))
    assert user is not None
    return str(user.id)


async def _kinds(o: Ops, headers: dict[str, str]) -> list[str]:
    body = (await o.get("/notifications", headers)).json()
    return [n["kind"] for n in body["items"]]


async def _audit(o: Ops, entity: str) -> list[dict[str, Any]]:
    with site_scope(o.site_id):
        async with o.factory() as session:
            rows = await session.scalars(
                select(AuditLog).where(AuditLog.entity == entity).order_by(AuditLog.created_at)
            )
            return [
                {"action": r.action, "after": r.after, "actor": r.actor_name} for r in rows.all()
            ]


async def test_teknik_personel_yalniz_atanan_ve_kendi_actigi_talepleri_isler(ops: Ops) -> None:
    tech = await ops.user("teknik@test.local", "Teknik Personel")
    tech_id = await _user_id(ops, "teknik@test.local")
    first = (await open_request(ops, title="Kazan dairesi su kaçağı")).json()["data"]
    second = (await open_request(ops, title="Bahçe kapısı kilidi")).json()["data"]

    # Atanmadan önce hiçbir talep görünmez (eskiden 8 talebin hepsi görünüyordu).
    assert (await ops.get("/requests", tech)).json()["total"] == 0
    assert (await ops.get(f"/requests/{first['id']}", tech)).status_code == 404

    staff = (await ops.get("/requests/assignable-staff")).json()
    by_id = {s["id"]: s for s in staff}
    assert by_id[tech_id]["all_requests"] is False
    assert by_id[tech_id]["roles"] == ["Teknik Personel"]
    assert (await ops.get("/requests/assignable-staff", tech)).status_code == 403

    assigned = await ops.post(f"/requests/{first['id']}/assign", {"assignee_id": tech_id})
    assert assigned.status_code == 200, assigned.text
    data = assigned.json()["data"]
    assert (data["assigned_to"], data["assignee_id"], data["status"]) == (
        "Test KULLANICI",
        tech_id,
        "assigned",
    )
    assert "request.assigned" in await _kinds(ops, tech)  # #6: atanana bildirim

    listed = (await ops.get("/requests", tech)).json()
    assert [r["id"] for r in listed["items"]] == [first["id"]]
    assert set(listed["items"][0]["allowed_transitions"]) == {"in_progress", "cancelled"}
    assert (await ops.get(f"/requests/{second['id']}", tech)).status_code == 404
    other = await ops.post(f"/requests/{second['id']}/comments", {"body": "?"}, tech)
    assert other.status_code == 404

    # Kapsamındaki talepte durum, yorum, iç not; atama ve departman yok.
    progress = await ops.post(f"/requests/{first['id']}/status", {"status": "in_progress"}, tech)
    assert progress.status_code == 200, progress.text
    comment = await ops.post(f"/requests/{first['id']}/comments", {"body": "Vana kapatıldı"}, tech)
    assert comment.status_code == 201
    note = await ops.post(f"/requests/{first['id']}/internal-notes", {"body": "Parça lazım"}, tech)
    assert note.status_code == 201
    assert len((await ops.get(f"/requests/{first['id']}/internal-notes", tech)).json()) == 1
    reassign = await ops.post(f"/requests/{first['id']}/assign", {"assignee": "Başka Usta"}, tech)
    assert reassign.status_code == 403
    department = await ops.post(
        f"/requests/{first['id']}/department", {"department_id": None}, tech
    )
    assert department.status_code == 403
    assert (await ops.get(f"/requests/{first['id']}/merge-suggestions", tech)).status_code == 403
    assert (await ops.get("/reply-templates", tech)).status_code == 200
    assert (await ops.get("/service-tariffs", tech)).status_code == 200

    # Kendi açtığı talebi de görür; açtığı talep personel kanalıdır.
    own = await open_request(ops, tech, title="Pompa sesi")
    assert own.status_code == 201, own.text
    assert own.json()["data"]["channel"] == "staff"
    titles = {r["title"] for r in (await ops.get("/requests", tech)).json()["items"]}
    assert titles == {"Kazan dairesi su kaçağı", "Pompa sesi"}

    # Başkasına geçince talep kapsamdan çıkar.
    await ops.post(f"/requests/{first['id']}/assign", {"assignee": "Tesisat Firması"})
    assert (await ops.get(f"/requests/{first['id']}", tech)).status_code == 404

    # #3: açma ve atama denetim kaydında (başlık değil, numara ve atanan).
    rows = await _audit(ops, "requests")
    assert [r["action"] for r in rows].count("create") == 3
    updates = [r["after"] for r in rows if r["action"] == "update"]
    assert updates[0] == {"number": 1, "assigned_to": "Test KULLANICI", "assignee_user_id": tech_id}
    assert all("title" not in (r["after"] or {}) for r in rows)


async def test_atama_dogrulama_ve_serbest_metin_geriye_uyumlu(ops: Ops) -> None:
    request_id = (await open_request(ops)).json()["data"]["id"]
    outsider = await create_user(ops.factory, "disari@test.local")
    bad = await ops.post(f"/requests/{request_id}/assign", {"assignee_id": str(outsider.id)})
    assert bad.status_code == 422
    assert "assignee_id" in bad.json()["error"]["fields"]
    empty = await ops.post(f"/requests/{request_id}/assign", {})
    assert empty.json()["error"]["code"] == "assignee_required"
    text = await ops.post(f"/requests/{request_id}/assign", {"assignee": "Asansör firması"})
    assert text.status_code == 200
    assert (text.json()["data"]["assigned_to"], text.json()["data"]["assignee_id"]) == (
        "Asansör firması",
        None,
    )


async def _resolve(ops: Ops, request_id: str) -> None:
    await ops.post(f"/requests/{request_id}/assign", {"assignee": "Teknik ekip"})
    await ops.post(f"/requests/{request_id}/status", {"status": "in_progress"})
    resolved = await ops.post(
        f"/requests/{request_id}/status", {"status": "resolved", "resolution": "Giderildi"}
    )
    assert resolved.status_code == 200, resolved.text


async def test_personelin_actigi_talep_sakin_onayi_beklemez(ops: Ops) -> None:
    """K18: yalnız sakinin (sakin ucundan) açtığı talep personelce kapatılamaz."""
    sakin = await resident(ops)
    by_resident = await ops.post(
        "/resident/requests", {"title": "Musluk damlatıyor", "unit_id": ops.ids["unit_A"]}, sakin
    )
    assert by_resident.json()["data"]["channel"] == "resident"
    on_behalf = await open_request(
        ops, title="Kapı zili", reported_by_person_id=ops.ids["elif"], unit_id=ops.ids["unit_A"]
    )
    assert on_behalf.json()["data"]["channel"] == "staff"
    assert on_behalf.json()["data"]["reported_by_person_id"] == ops.ids["elif"]

    for created in (by_resident, on_behalf):
        await _resolve(ops, created.json()["data"]["id"])
    resident_id = by_resident.json()["data"]["id"]
    staff_id = on_behalf.json()["data"]["id"]
    detail = (await ops.get(f"/requests/{resident_id}")).json()
    assert "closed" not in detail["allowed_transitions"]
    blocked = await ops.post(f"/requests/{resident_id}/status", {"status": "closed"})
    assert blocked.json()["error"]["code"] == "awaiting_confirmation"
    closed = await ops.post(f"/requests/{staff_id}/status", {"status": "closed"})
    assert closed.status_code == 200, closed.text


async def test_sakin_de_olan_personelin_talebi_personel_kanalidir(ops: Ops) -> None:
    """Rapor: personelin açtığı talepte "Sakin onayı bekleniyor" — kişisi olan personel
    hesabı eskiden sakin sayılıyordu."""
    guard = await ops.user("guvenlik@test.local", "Güvenlik", person_id=uuid.UUID(ops.ids["elif"]))
    created = await open_request(ops, guard, title="Kapı kartı okumuyor", location="Ana kapı")
    data = created.json()["data"]
    assert (data["channel"], data["reported_by_person_id"]) == ("staff", None)
    # Sakin hesabı personel ucuna gelirse geriye uyum: sakin talebi.
    sakin = await resident(ops, "mehmet", "mehmet@test.local")
    legacy = await open_request(ops, sakin, unit_id=ops.ids["unit_B"])
    assert legacy.json()["data"]["channel"] == "resident"


async def test_okuma_onayli_duyuruda_sakine_bildirim_ve_denetim(ops: Ops) -> None:
    sakin = await resident(ops)
    plain = await ops.post("/announcements", {"title": "Bahçe ilaçlama", "body": "Cuma günü."})
    assert plain.status_code == 201
    assert "announcement.ack_required" not in await _kinds(ops, sakin)
    acked = await ops.post(
        "/announcements",
        {
            "title": "Genel kurul",
            "body": "Toplantı çağrısı.",
            "requires_ack": True,
            "ack_hours": 48,
        },
    )
    assert acked.status_code == 201, acked.text
    assert (await _kinds(ops, sakin)).count("announcement.ack_required") == 1
    rows = await _audit(ops, "announcements")
    assert [r["after"]["requires_ack"] for r in rows] == [False, True]
    assert all(r["actor"] == "Kerem YILDIRIM" for r in rows)
