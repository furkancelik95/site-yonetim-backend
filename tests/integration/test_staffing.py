"""İnsan kaynakları — servis istekleri 35–39 + PM kararı K29 (gerçek PostgreSQL).

Kurgu `finance_world` (bugün 20.06.2026 cumartesi; hafta 15–21.06): personel modülü planla açılır.
Kerem YILDIRIM yönetici (staff.manage). Saha personeli Güvenlik rolüyle giriş yapar ve kartına
bağlanır (K29).
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.api.deps import get_now
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import (
    AuditLog,
    Notification,
    Plan,
    Site,
    SiteMembership,
    SiteRoleAssignment,
    StaffMember,
)
from site_yonetim.services import staff as staff_svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

MODULES = ["finance", "staff"]
MANAGER_FORM = "Formda düzeltilmesi gereken alanlar var."


@pytest.fixture
async def hr(world: World) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="İK", max_units=150, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/staff/toggle")).status_code == 200
    return world


async def user_headers(
    w: World, email: str, role: str, name: str = "Test KULLANICI"
) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, email, full_name=name)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return user.id, await login_headers(w.api, email)


async def patch(w: World, path: str, body: dict[str, Any], **kw: Any) -> httpx2.Response:
    headers = kw.pop("headers", w.headers)
    return await w.api.patch(w.url(path), json=body, headers=headers)


async def delete(w: World, path: str) -> httpx2.Response:
    return await w.api.delete(w.url(path), headers=w.headers)


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["message"] == MANAGER_FORM
    return dict(body["fields"])


def code(response: httpx2.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


STAFF = {
    "full_name": "Mehmet Kaya",
    "position": "Güvenlik görevlisi",
    "employer": "site",
    "start_date": "2026-01-05",
}


async def add_staff(w: World, **changes: Any) -> dict[str, Any]:
    response = await w.post("/staff", {**STAFF, **changes})
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def add_template(
    w: World, name: str, start: str, end: str, break_minutes: int = 60
) -> dict[str, Any]:
    response = await w.post(
        "/shift-templates",
        {"name": name, "start": start, "end": end, "break_minutes": break_minutes},
    )
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def assign(w: World, staff_id: str, day: str, template_id: str) -> httpx2.Response:
    return await w.post(
        "/shift-plans/assignments", {"staff_id": staff_id, "date": day, "template_id": template_id}
    )


async def audited(w: World, entity: str) -> list[AuditLog]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            return list(await session.scalars(select(AuditLog).where(AuditLog.entity == entity)))


# --- 35 + K29: sicil no, giriş hesabı, ayrılış ---------------------------------------------


async def test_sicil_no_bicim_ve_tekillik(hr: World) -> None:
    w = hr
    member = await add_staff(w, payroll_no=" p-01 ")
    assert member["payroll_no"] == "P-01"
    taken = await w.post("/staff", {**STAFF, "full_name": "Ali Demir", "payroll_no": "P-01"})
    assert code(taken) == (409, "payroll_no_taken")
    bad = await w.post("/staff", {**STAFF, "full_name": "Ali Demir", "payroll_no": "12/34"})
    assert fields(bad)["payroll_no"] == "Sicil no harf, rakam ve tire; en fazla 20 karakter."
    cleared = await patch(w, f"/staff/{member['id']}", {"payroll_no": ""})
    assert cleared.json()["data"]["payroll_no"] is None
    listed = (await w.get("/staff")).json()
    assert listed[0]["payroll_no"] is None


async def test_k29_hesap_baglama_ve_ayrilista_erisim_kapanir(hr: World) -> None:
    w = hr
    guard_id, guard = await user_headers(w, "guvenlik@test.local", "Güvenlik", "Mehmet KAYA")
    linkable = (await w.get("/staff/linkable-users")).json()
    assert {x["full_name"] for x in linkable} == {"Kerem YILDIRIM", "Mehmet KAYA"}
    member = await add_staff(w, user_id=str(guard_id))
    assert (member["user_id"], member["user_name"]) == (str(guard_id), "Mehmet KAYA")
    again = await w.post("/staff", {**STAFF, "full_name": "Ali Demir", "user_id": str(guard_id)})
    assert code(again) == (409, "user_taken")
    resident = await create_user(w.factory, "sakin@test.local")
    await add_site_membership(
        w.factory, w.site_id, resident.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    not_staff = await w.post(
        "/staff", {**STAFF, "full_name": "Ali Demir", "user_id": str(resident.id)}
    )
    assert "user_id" in fields(not_staff)

    # Ayrılış bugün: hâlâ çalışıyor, erişim açık
    left = await patch(w, f"/staff/{member['id']}", {"end_date": "2026-06-20"})
    assert left.json()["message"].endswith("ayrılış tarihinden sonra kendiliğinden kapanır.")
    me = (await w.api.get("/api/v1/me", headers=guard)).json()
    assert [s["slug"] for s in me["sites"]] == ["aksu-konaklari"]
    # Ertesi gün: gece işi çalışmadan da bir sonraki istekte erişim yok (404)
    w.set_today(date(2026, 6, 21))
    assert (await w.api.get(w.url("/me/shifts"), headers=guard)).status_code == 404
    assert (await w.api.get("/api/v1/me", headers=guard)).json()["sites"] == []
    closed = await staff_svc.run_offboarding(
        w.factory, today=date(2026, 6, 21), now=datetime(2026, 6, 20, 22, 0, tzinfo=UTC)
    )
    assert [c.full_name for c in closed] == ["Mehmet Kaya"]
    assert (
        await staff_svc.run_offboarding(w.factory, today=date(2026, 6, 21), now=datetime.now(UTC))
        == []
    )
    with site_scope(w.site_id):
        async with w.factory() as session:
            membership = await session.scalar(
                select(SiteMembership).where(SiteMembership.user_id == guard_id)
            )
            card = await session.get(StaffMember, uuid.UUID(member["id"]))
    assert membership is not None
    assert membership.is_active is False
    assert card is not None
    assert card.access_closed_at is not None


async def test_gecmis_tarihli_ayrilis_erisimi_hemen_kapatir(hr: World) -> None:
    w = hr
    guard_id, guard = await user_headers(w, "guvenlik@test.local", "Güvenlik")
    member = await add_staff(w, user_id=str(guard_id))
    left = await patch(w, f"/staff/{member['id']}", {"end_date": "2026-06-19"})
    assert left.json()["message"] == "Mehmet Kaya için ayrılış kaydedildi. Site erişimi kapatıldı."
    assert left.json()["data"]["access_closed_at"] is not None
    assert (await w.api.get("/api/v1/me", headers=guard)).json()["sites"] == []


async def test_ayrilista_acik_isler(hr: World) -> None:
    w = hr
    member = await add_staff(w)
    sabah = await add_template(w, "Sabah", "08:00", "16:00")
    for day in ("2026-06-22", "2026-06-23", "2026-06-19"):
        assert (await assign(w, member["id"], day, sabah["id"])).status_code == 201
    leave = await w.post(
        "/leave-requests",
        {"staff_id": member["id"], "type": "annual", "from": "2026-07-01", "to": "2026-07-03"},
    )
    assert leave.status_code == 201, leave.text
    items = (await w.get(f"/staff/{member['id']}/open-items")).json()
    assert items == {"future_shifts": 2, "first_shift": "2026-06-22", "pending_leaves": 1}
    after = (
        await w.get(f"/staff/{member['id']}/open-items", params={"after": "2026-06-23"})
    ).json()
    assert after["future_shifts"] == 1
    assert (await w.get(f"/staff/{uuid.uuid4()}/open-items")).status_code == 404


# --- 36: vardiya şablonları ve plan --------------------------------------------------------


async def test_sablon_dogrulama_ve_kapali_sablon(hr: World) -> None:
    w = hr
    sabah = await add_template(w, "Sabah", "08:00", "16:00")
    assert (sabah["start"], sabah["end"], sabah["overnight"]) == ("08:00", "16:00", False)
    gece = await add_template(w, "Gece", "22:00", "06:00", 30)
    assert gece["overnight"] is True
    bad = await w.post(
        "/shift-templates", {"name": "sabah", "start": "8:00", "end": "08:00", "break_minutes": 181}
    )
    assert set(fields(bad)) == {"name", "start", "break_minutes"}
    same = await w.post("/shift-templates", {"name": "X", "start": "08:00", "end": "08:00"})
    assert fields(same)["end"] == "Bitiş başlangıçla aynı olamaz."
    off = await patch(w, f"/shift-templates/{sabah['id']}", {"is_active": False})
    assert off.json()["data"]["is_active"] is False
    assert off.json()["data"]["start"] == "08:00"  # kısmi: diğer alanlar korunur
    member = await add_staff(w)
    assert "template_id" in fields(await assign(w, member["id"], "2026-06-22", sabah["id"]))
    assert len((await w.get("/shift-templates")).json()) == 2
    assert "shift_templates" in {a.entity for a in await audited(w, "shift_templates")}


async def test_plan_cakisma_yayin_ve_kopyalama(hr: World) -> None:
    w = hr
    guard_id, guard = await user_headers(w, "guvenlik@test.local", "Güvenlik")
    ali = await add_staff(w, full_name="Ali Demir", user_id=str(guard_id))
    veli = await add_staff(w, full_name="Veli Şahin")
    gece = await add_template(w, "Gece", "00:00", "08:00", 30)
    aksam = await add_template(w, "Akşam", "16:00", "00:00", 30)
    sabah = await add_template(w, "Sabah", "08:00", "16:00")

    # Kabul: izinli personele o gün vardiya → leave
    leave = (
        await w.post(
            "/leave-requests",
            {"staff_id": veli["id"], "type": "sick", "from": "2026-06-23", "to": "2026-06-23"},
        )
    ).json()["data"]
    assert (await w.post(f"/leave-requests/{leave['id']}/approve")).status_code == 200
    on_leave = await assign(w, veli["id"], "2026-06-23", sabah["id"])
    assert on_leave.status_code == 201
    assert [c["kind"] for c in on_leave.json()["data"]["conflicts"]] == ["leave"]
    assert on_leave.json()["message"].startswith("Atandı — uyarı: Onaylı izin günü (raporlu)")

    # Kabul: gece 00–08 ardından 16–24 aynı gün → dinlenme 8 saat
    assert (await assign(w, ali["id"], "2026-06-22", gece["id"])).status_code == 201
    rest = await assign(w, ali["id"], "2026-06-22", aksam["id"])
    (conflict,) = rest.json()["data"]["conflicts"]
    assert conflict["kind"] == "rest"
    assert "8 saat" in conflict["message"]
    assert code(await assign(w, ali["id"], "2026-06-22", gece["id"])) == (409, "duplicate")
    early = await assign(w, ali["id"], "2025-12-31", gece["id"])
    assert "staff_id" in fields(early)

    plan = (await w.get("/shift-plans", params={"from": "2026-06-22", "to": "2026-06-28"})).json()
    assert (plan["from"], plan["to"]) == ("2026-06-22", "2026-06-28")
    assert {s["full_name"] for s in plan["staff"]} == {"Ali Demir", "Veli Şahin"}
    assert len(plan["assignments"]) == 3
    assert len(plan["conflicts"]) == 2
    assert plan["leaves"] == [
        {"staff_id": veli["id"], "from": "2026-06-23", "to": "2026-06-23", "type": "sick"}
    ]
    assert plan["published"] is None

    # Raporlu izin türü yalnız staff.manage'e: YK üyesi (people.read) türsüz görür
    _, board = await user_headers(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    seen = await w.api.get(
        w.url("/shift-plans"), params={"from": "2026-06-22", "to": "2026-06-28"}, headers=board
    )
    assert seen.json()["leaves"][0]["type"] is None
    assert (await w.api.get(w.url("/shift-plans"), headers=guard)).status_code == 403
    assert (
        await w.api.post(w.url("/shift-templates"), json={"name": "x"}, headers=board)
    ).status_code == 403

    # Kabul: çakışmalı hafta onaysız yayınlanmaz
    week = {"week": "2026-06-22"}
    assert code(await w.post("/shift-plans/publish", week)) == (409, "conflicts")
    assert "week" in fields(await w.post("/shift-plans/publish", {"week": "2026-06-23"}))
    assert code(await w.post("/shift-plans/publish", {"week": "2026-07-06"})) == (
        409,
        "empty_plan",
    )
    published = await w.post("/shift-plans/publish", {**week, "acknowledge_conflicts": True})
    assert published.status_code == 200, published.text
    assert published.json()["data"] == {"week": "2026-06-22", "assignments": 3}

    later = await assign(w, ali["id"], "2026-06-25", sabah["id"])
    assert later.json()["data"]["changed_after_publish"] is True
    plan = (await w.get("/shift-plans", params={"from": "2026-06-22"})).json()
    assert plan["published"]["by"] == "Kerem YILDIRIM"
    assert plan["changed_after_publish"] == 1
    removed = await delete(w, f"/shift-plans/assignments/{rest.json()['data']['id']}")
    assert removed.json()["message"] == "Atama kaldırıldı."
    assert (await w.get("/shift-plans", params={"from": "2026-06-22"})).json()[
        "changed_after_publish"
    ] == 2
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = list(
                await session.scalars(
                    select(Notification.kind).where(Notification.user_id == guard_id)
                )
            )
    assert sorted(kinds) == ["shift.changed", "shift.changed", "shift.published"]

    # Vardiyam (K29): yalnız yayınlanmış haftalar
    own = await w.api.get(w.url("/me/shifts"), params={"from": "2026-06-22"}, headers=guard)
    assert own.status_code == 200, own.text
    assert [(s["date"], s["template"]["name"]) for s in own.json()["shifts"]] == [
        ("2026-06-22", "Gece"),
        ("2026-06-25", "Sabah"),
    ]
    assert (await w.get("/me/shifts")).status_code == 403  # yöneticinin kartı yok

    # Kopyalama: eksikleri ekler, var olanı korur
    copied = await w.post(
        "/shift-plans/copy-week", {"from_week": "2026-06-22", "to_week": "2026-06-29"}
    )
    assert copied.json()["data"] == {"created": 3}
    again = await w.post(
        "/shift-plans/copy-week", {"from_week": "2026-06-22", "to_week": "2026-06-29"}
    )
    assert again.json()["data"] == {"created": 0}
    bad = await w.post(
        "/shift-plans/copy-week", {"from_week": "2026-06-23", "to_week": "2026-06-29"}
    )
    assert set(fields(bad)) == {"from_week"}
    assert (await delete(w, f"/shift-plans/assignments/{uuid.uuid4()}")).status_code == 404


# --- 37: izin kaydı ve onayı ---------------------------------------------------------------


async def test_izin_basit_akis_cakisma_bakiye(hr: World) -> None:
    w = hr
    member = await add_staff(w)
    bad = await w.post("/leave-requests", {"type": "x", "from": "2026-06-10", "to": "2026-06-09"})
    assert set(fields(bad)) == {"staff_id", "type", "to"}
    sick_note = await w.post(
        "/leave-requests",
        {
            "staff_id": member["id"],
            "type": "sick",
            "from": "2026-06-10",
            "to": "2026-06-11",
            "note": "grip",
        },
    )
    assert fields(sick_note)["note"] == "Raporlu izinde açıklama yazılmaz; tanı bilgisi tutulmaz."
    created = await w.post(
        "/leave-requests",
        {
            "staff_id": member["id"],
            "type": "annual",
            "from": "2026-06-29",
            "to": "2026-07-03",
            "note": "Tatil",
        },
    )
    assert created.status_code == 201, created.text
    leave = created.json()["data"]
    assert (leave["days"], leave["status"], leave["source"]) == (5, "pending", "manager")
    assert leave["approval_request_id"] is None
    assert created.json()["message"] == "İzin talebi kaydedildi. Onay bekliyor."
    overlap = await w.post(
        "/leave-requests",
        {"staff_id": member["id"], "type": "excuse", "from": "2026-07-03", "to": "2026-07-03"},
    )
    assert code(overlap) == (409, "leave_overlap")

    no_reason = await w.post(f"/leave-requests/{leave['id']}/reject", {"reason": " "})
    assert fields(no_reason)["reason"] == "Ret gerekçesi yazın."
    approved = await w.post(f"/leave-requests/{leave['id']}/approve")
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["decided_by"] == "Kerem YILDIRIM"
    assert code(await w.post(f"/leave-requests/{leave['id']}/approve")) == (409, "invalid_status")

    other = (
        await w.post(
            "/leave-requests",
            {"staff_id": member["id"], "type": "unpaid", "from": "2026-06-01", "to": "2026-06-02"},
        )
    ).json()["data"]
    rejected = await w.post(f"/leave-requests/{other['id']}/reject", {"reason": "Yoğun dönem"})
    assert rejected.json()["data"]["reject_reason"] == "Yoğun dönem"

    pending = (await w.get("/leave-requests", params={"status": "approved"})).json()
    assert pending["total"] == 1
    assert pending["items"][0]["type"] == "annual"
    assert (await w.get("/leave-requests")).json()["total"] == 2

    balances = (await w.get("/leave-balances")).json()
    assert balances == [
        {
            "staff_id": member["id"],
            "staff_name": "Mehmet Kaya",
            "used": {"annual": 5, "excuse": 0, "sick": 0, "unpaid": 0},
        }
    ]
    cancelled = await w.post(f"/leave-requests/{leave['id']}/cancel")
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert code(await w.post(f"/leave-requests/{leave['id']}/cancel")) == (409, "invalid_status")

    # İzin türü ve açıklaması denetim kaydında maskeli (sağlık verisi olabilir)
    logs = await audited(w, "leave_requests")
    created_log = next(x for x in logs if x.action == "create")
    assert created_log.after is not None
    assert created_log.after["leave_type"] == "***"
    assert created_log.after["leave_note"] == "***"


async def test_izin_yetki_kendi_izni_ve_izolasyon(hr: World) -> None:
    w = hr
    member = await add_staff(w)
    _, board = await user_headers(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/leave-requests"), headers=board)).status_code == 403
    assert (await w.api.get(w.url("/leave-balances"), headers=board)).status_code == 403
    leave = (
        await w.post(
            "/leave-requests",
            {"staff_id": member["id"], "type": "annual", "from": "2026-07-01", "to": "2026-07-01"},
        )
    ).json()["data"]
    assert (
        await w.api.post(w.url(f"/leave-requests/{leave['id']}/approve"), headers=board)
    ).status_code == 403
    yildiz = await w.api.post(
        w.url(f"/leave-requests/{leave['id']}/approve", "yildiz-sitesi"), headers=w.headers
    )
    assert yildiz.status_code == 404  # modül kapalı / başka site

    # Kendi iznini onaylayamaz: yöneticinin de kartı olabilir
    with site_scope(w.site_id):
        async with w.factory() as session:
            manager_id = await session.scalar(
                select(SiteRoleAssignment.user_id).where(SiteRoleAssignment.role_key == "manager")
            )
    mine = await add_staff(w, full_name="Kerem Yıldırım", user_id=str(manager_id))
    own = (
        await w.post(
            "/leave-requests",
            {"staff_id": mine["id"], "type": "annual", "from": "2026-07-01", "to": "2026-07-01"},
        )
    ).json()["data"]
    assert code(await w.post(f"/leave-requests/{own['id']}/approve")) == (403, "own_request")


async def test_izin_onay_kurali_ony_uzerinden(hr: World) -> None:
    w = hr
    board_id, board = await user_headers(w, "kurul@test.local", "Yönetim Kurulu Üyesi", "Ayşe YK")
    guard_id, guard = await user_headers(w, "guvenlik@test.local", "Güvenlik", "Mehmet KAYA")
    await add_staff(w, user_id=str(guard_id))
    types = (await w.get("/approval-action-types")).json()
    assert next(t for t in types if t["key"] == "leave_request")["connected"] is True
    rule = await w.post(
        "/approval-rules",
        {
            "action_type": "leave_request",
            "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": "2026-06-20",
        },
    )
    assert rule.status_code == 201, rule.text

    # Personel kendi talebini açar (K29) → onay kutusuna düşer
    created = await w.api.post(
        w.url("/me/leave-requests"),
        json={"type": "annual", "from": "2026-07-06", "to": "2026-07-08"},
        headers=guard,
    )
    assert created.status_code == 201, created.text
    leave = created.json()["data"]
    assert leave["source"] == "staff"
    assert leave["approval_request_id"] is not None
    assert created.json()["message"].endswith("onay kutusuna gönderildi.")
    # Yönetici bu kuralda onaycı değil
    assert code(await w.post(f"/leave-requests/{leave['id']}/approve")) == (403, "not_an_approver")
    approved = await w.api.post(w.url(f"/leave-requests/{leave['id']}/approve"), headers=board)
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["decided_by"] == "Ayşe YK"

    # Ret onay kutusundan: kapatıcı izni "reddedildi" yapar
    second = (
        await w.api.post(
            w.url("/me/leave-requests"),
            json={"type": "excuse", "from": "2026-07-10", "to": "2026-07-10"},
            headers=guard,
        )
    ).json()["data"]
    rejected = await w.api.post(
        w.url(f"/approvals/{second['approval_request_id']}/reject"),
        json={"comment": "Nöbet değişimi bulun"},
        headers=board,
    )
    assert rejected.status_code == 200, rejected.text
    mine = (await w.api.get(w.url("/me/leave-requests"), headers=guard)).json()["items"]
    by_id = {x["id"]: x for x in mine}
    assert by_id[second["id"]]["status"] == "rejected"
    assert by_id[second["id"]]["reject_reason"] == "Nöbet değişimi bulun"

    # Bekleyen kendi talebini geri çeker (ONY'de geri çekilir)
    third = (
        await w.api.post(
            w.url("/me/leave-requests"),
            json={"type": "unpaid", "from": "2026-07-20", "to": "2026-07-21"},
            headers=guard,
        )
    ).json()["data"]
    withdrawn = await w.api.post(w.url(f"/me/leave-requests/{third['id']}/cancel"), headers=guard)
    assert withdrawn.json()["data"]["status"] == "cancelled"
    other_cancel = await w.api.post(
        w.url(f"/me/leave-requests/{leave['id']}/cancel"), headers=guard
    )
    assert code(other_cancel) == (409, "invalid_status")
    assert (
        await w.api.post(w.url(f"/me/leave-requests/{uuid.uuid4()}/cancel"), headers=guard)
    ).status_code == 404
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = list(
                await session.scalars(
                    select(Notification.kind).where(Notification.user_id == board_id)
                )
            )
    assert kinds.count("approval.requested") == 3


# --- 38: günlük puantaj --------------------------------------------------------------------


async def test_puantaj_hesap_duzeltme_kesinlestirme(hr: World) -> None:
    w = hr
    ali = await add_staff(w, full_name="Ali Demir")
    veli = await add_staff(w, full_name="Veli Şahin")
    sabah = await add_template(w, "Sabah", "08:00", "16:00", 60)
    aksam = await add_template(w, "Akşam", "16:00", "00:00", 30)
    a1 = (await assign(w, ali["id"], "2026-06-15", sabah["id"])).json()["data"]
    a2 = (await assign(w, veli["id"], "2026-06-15", aksam["id"])).json()["data"]
    leave = (
        await w.post(
            "/leave-requests",
            {"staff_id": veli["id"], "type": "annual", "from": "2026-06-16", "to": "2026-06-16"},
        )
    ).json()["data"]
    await w.post(f"/leave-requests/{leave['id']}/approve")

    day = (await w.get("/timesheets", params={"date": "2026-06-15"})).json()
    assert day["is_final"] is False
    rows = {r["staff_name"]: r for r in day["rows"]}
    assert rows["Ali Demir"]["planned_minutes"] == 420
    assert rows["Veli Şahin"]["night_minutes"] == 120  # kabul: 16–24 → gece 2 saat

    bad = await patch(
        w, f"/timesheets/{a1['id']}", {"status": "x", "late_minutes": 961, "reason": ""}
    )
    assert fields(bad) == {
        "status": "Durum seçin.",
        "late_minutes": "0–960 dakika.",
        "reason": "Düzeltme gerekçesi zorunlu.",
    }
    fixed = await patch(
        w, f"/timesheets/{a1['id']}", {"status": "worked", "late_minutes": 20, "reason": "Trafik"}
    )
    assert fixed.status_code == 200, fixed.text
    row = next(
        r
        for r in (await w.get("/timesheets", params={"date": "2026-06-15"})).json()["rows"]
        if r["id"] == a1["id"]
    )
    assert (row["worked_minutes"], row["is_exception"], row["fixed_by"]) == (
        400,
        True,
        "Kerem YILDIRIM",
    )  # kabul: 6 sa 40 dk
    logs = await audited(w, "timesheet_fixes")
    assert logs
    assert logs[0].reason == "Trafik"

    leave_day = (await w.get("/timesheets", params={"date": "2026-06-16"})).json()
    (only,) = leave_day["rows"]
    assert (only["id"], only["status"], only["leave_type"]) == (None, "leave", "annual")

    assert code(await w.post("/timesheets/finalize", {"date": "2026-06-20"})) == (
        409,
        "day_not_ended",
    )
    final = await w.post("/timesheets/finalize", {"date": "2026-06-15"})
    assert final.json()["data"] == {"date": "2026-06-15", "rows": 2}
    assert code(await w.post("/timesheets/finalize", {"date": "2026-06-15"})) == (
        409,
        "already_final",
    )
    # kabul: kesinleşen güne düzeltme 409; atama kaldırılamaz, eklenemez
    assert code(
        await patch(w, f"/timesheets/{a1['id']}", {"status": "absent", "reason": "Gelmedi"})
    ) == (409, "timesheet_final")
    assert code(await delete(w, f"/shift-plans/assignments/{a2['id']}")) == (
        409,
        "timesheet_final",
    )
    assert code(await assign(w, ali["id"], "2026-06-15", aksam["id"])) == (409, "timesheet_final")
    # Şablon sonradan değişse de kesin gün donmuş kalır
    await patch(w, f"/shift-templates/{sabah['id']}", {"break_minutes": 0})
    frozen = (await w.get("/timesheets", params={"date": "2026-06-15"})).json()
    assert frozen["is_final"] is True
    assert frozen["finalized_by"] == "Kerem YILDIRIM"
    assert next(r for r in frozen["rows"] if r["id"] == a1["id"])["worked_minutes"] == 400

    await w.post("/timesheets/finalize", {"date": "2026-06-16"})
    assert code(await w.post(f"/leave-requests/{leave['id']}/cancel")) == (409, "timesheet_final")
    with site_scope(w.site_id), pytest.raises(DBAPIError, match="değiştirilemez"):
        async with w.factory() as session, session.begin():
            await session.execute(text("UPDATE timesheet_lines SET worked_minutes = 0"))
    future = await patch(w, f"/timesheets/{a1['id']}", {"status": "worked", "reason": "x"})
    assert future.status_code == 409


# --- 39: aktarım ---------------------------------------------------------------------------


async def test_puantaj_aktarimi(hr: World) -> None:
    w = hr
    ali = await add_staff(w, full_name="Ali Demir")
    sabah = await add_template(w, "Sabah", "08:00", "16:00", 60)
    for day in ("2026-05-04", "2026-05-05"):
        await assign(w, ali["id"], day, sabah["id"])
    pv = (await w.get("/timesheet-exports/preview", params={"month": "2026-05"})).json()
    assert pv["missing_days"] == ["2026-05-04", "2026-05-05"]
    assert pv["missing_payroll"] == ["Ali Demir"]
    assert pv["rows"][0]["worked_days"] == 2
    assert pv["rows"][0]["worked_hours"] == "14.00"
    assert code(await w.post("/timesheet-exports", {"month": "2026-06"})) == (
        409,
        "month_not_ended",
    )
    assert code(await w.post("/timesheet-exports", {"month": "2026-05"})) == (
        409,
        "days_not_final",
    )
    for day in ("2026-05-04", "2026-05-05"):
        assert (await w.post("/timesheets/finalize", {"date": day})).status_code == 200
    missing = await w.post("/timesheet-exports", {"month": "2026-05"})
    assert code(missing) == (409, "payroll_missing")
    assert "Ali Demir" in missing.json()["error"]["message"]
    await patch(w, f"/staff/{ali['id']}", {"payroll_no": "S-100"})
    done = await w.post("/timesheet-exports", {"month": "2026-05"})
    assert done.status_code == 201, done.text
    export = done.json()["data"]
    assert (export["month"], export["staff_count"]) == ("2026-05", 1)
    assert code(await w.post("/timesheet-exports", {"month": "2026-05"})) == (
        409,
        "already_exported",
    )
    assert "month" in fields(await w.post("/timesheet-exports", {"month": "2026-5"}))

    # Muhasebe okur (finance.read), aktaramaz; teknik personel göremez
    _, accounting = await user_headers(w, "muhasebe@test.local", "Muhasebe")
    history = await w.api.get(w.url("/timesheet-exports"), headers=accounting)
    assert history.json()["items"][0]["file_name"] == "puantaj-aksu-konaklari-2026-05.csv"
    file = await w.api.get(w.url(f"/timesheet-exports/{export['id']}/file"), headers=accounting)
    assert file.headers["content-type"].startswith("text/csv")
    lines = file.content.decode("utf-8-sig").splitlines()
    assert lines[1] == "2026-05;S-100;Ali Demir;2;14,00;0,00;0,00;0;0;0;0;0;0,00"
    assert (
        await w.api.post(w.url("/timesheet-exports"), json={"month": "2026-04"}, headers=accounting)
    ).status_code == 403
    _, tech = await user_headers(w, "teknik@test.local", "Teknik Personel")
    assert (await w.api.get(w.url("/timesheet-exports"), headers=tech)).status_code == 403
    assert (
        await w.api.get(w.url("/timesheet-exports", "yildiz-sitesi"), headers=w.headers)
    ).status_code == 404


# --- K29: vardiya giriş-çıkışı (QR / konum) ------------------------------------------------


async def test_giris_cikis_qr_ve_konum(hr: World) -> None:
    w = hr
    guard_id, guard = await user_headers(w, "guvenlik@test.local", "Güvenlik")
    member = await add_staff(w, user_id=str(guard_id))
    sabah = await add_template(w, "Sabah", "08:00", "16:00", 60)
    a = (await assign(w, member["id"], "2026-06-16", sabah["id"])).json()["data"]
    settings = await w.api.put(
        w.url("/attendance-settings"),
        json={"method": "qr", "late_tolerance_minutes": 10},
        headers=w.headers,
    )
    assert settings.status_code == 200, settings.text
    qr = settings.json()["data"]["qr_code"]
    assert len(qr) == 12
    w.set_today(date(2026, 6, 16))
    now = [datetime(2026, 6, 16, 5, 20, tzinfo=UTC)]  # 08:20 İstanbul
    w.app.dependency_overrides[get_now] = lambda: now[0]
    try:
        assert "code" in fields(
            await w.api.post(w.url("/me/attendance-events"), json={"kind": "in"}, headers=guard)
        )
        wrong = await w.api.post(
            w.url("/me/attendance-events"), json={"kind": "in", "code": "yanlis"}, headers=guard
        )
        assert wrong.status_code == 201, wrong.text
        assert wrong.json()["data"]["verified"] is False
        assert wrong.json()["data"]["assignment_id"] == a["id"]
        assert wrong.json()["message"].endswith("İK incelemesine düşer.")
        again = await w.api.post(
            w.url("/me/attendance-events"), json={"kind": "in", "code": qr}, headers=guard
        )
        assert code(again) == (409, "already_checked_in")
        now[0] = datetime(2026, 6, 16, 13, 0, tzinfo=UTC)  # 16:00
        out = await w.api.post(
            w.url("/me/attendance-events"), json={"kind": "out", "code": qr.lower()}, headers=guard
        )
        assert out.json()["data"]["verified"] is True
        own = (await w.api.get(w.url("/me/shifts"), headers=guard)).json()
        assert own["attendance_method"] == "qr"
        assert own["shifts"] == []
        await w.post("/shift-plans/publish", {"week": "2026-06-15"})
        own = (await w.api.get(w.url("/me/shifts"), headers=guard)).json()
        assert [len(s["events"]) for s in own["shifts"]] == [2]
        assert "qr_code" not in str(own)

        # Ertesi gün puantaj: 20 dk geç (tolerans 10) + doğrulanamayan kayıt
        w.set_today(date(2026, 6, 17))
        row = (await w.get("/timesheets", params={"date": "2026-06-16"})).json()["rows"][0]
        assert (row["late_minutes"], row["worked_minutes"]) == (20, 400)
        assert row["flags"] == ["unverified", "late"]
        assert row["is_exception"] is True

        # Konum yöntemi: tesis yarıçapı dışında doğrulanmaz
        loc = await w.api.put(
            w.url("/attendance-settings"),
            json={
                "method": "location",
                "latitude": "41.015137",
                "longitude": "28.979530",
                "radius_m": 200,
            },
            headers=w.headers,
        )
        assert loc.status_code == 200, loc.text
        b = (await assign(w, member["id"], "2026-06-17", sabah["id"])).json()["data"]
        now[0] = datetime(2026, 6, 17, 5, 0, tzinfo=UTC)
        assert "latitude" in fields(
            await w.api.post(w.url("/me/attendance-events"), json={"kind": "in"}, headers=guard)
        )
        far = await w.api.post(
            w.url("/me/attendance-events"),
            json={"kind": "in", "latitude": "41.025137", "longitude": "28.979530"},
            headers=guard,
        )
        assert far.json()["data"]["verified"] is False
        assert code(await delete(w, f"/shift-plans/assignments/{b['id']}")) == (
            409,
            "has_attendance",
        )
        future = await w.api.post(
            w.url("/me/attendance-events"),
            json={"kind": "out", "occurred_at": "2026-06-17T09:00:00Z"},
            headers=guard,
        )
        assert "occurred_at" in fields(future)
        rotated = await w.post("/attendance-settings/rotate-code")
        assert rotated.json()["data"]["qr_code"] != qr
        assert (await w.api.get(w.url("/attendance-settings"), headers=guard)).status_code == 403
    finally:
        w.app.dependency_overrides.pop(get_now, None)


async def test_modul_kapali_404_ve_baska_site(hr: World) -> None:
    w = hr
    member = await add_staff(w)
    sabah = await add_template(w, "Sabah", "08:00", "16:00")
    a = (await assign(w, member["id"], "2026-06-22", sabah["id"])).json()["data"]
    for path in ("/shift-templates", "/shift-plans", "/leave-requests", "/timesheets"):
        assert (await w.api.get(w.url(path, "yildiz-sitesi"), headers=w.headers)).status_code == 404
    assert (await w.post("/modules/staff/toggle")).status_code == 200
    for path in (
        "/shift-templates",
        "/shift-plans",
        "/leave-requests",
        "/timesheets",
        "/me/shifts",
    ):
        assert (await w.get(path)).status_code == 404
    assert (await delete(w, f"/shift-plans/assignments/{a['id']}")).status_code == 404
