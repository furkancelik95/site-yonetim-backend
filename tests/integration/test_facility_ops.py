"""Operasyon ekleri — servis istekleri 53–58 (Yazılım Dokümanı OPS-01..06; gerçek PostgreSQL).

Kurgu `finance_world` (bugün 20.06.2026, A-1'in maliki Ayşe Yılmaz). Saat gerektiren uçlar için
`get_now` sabitlenir (20.06.2026 10:00 İstanbul).
"""

import asyncio
import base64
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.api.deps import get_now
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import (
    AuditLog,
    Notification,
    Plan,
    Request,
    Site,
    StaffMember,
    StockMove,
)
from site_yonetim.services import insurance as insurance_svc
from site_yonetim.services import keys as keys_svc
from site_yonetim.services import routine_tasks as tasks_svc
from site_yonetim.services.files import FileStore
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

FORM = "Formda düzeltilmesi gereken alanlar var."
NOW = datetime(2026, 6, 20, 7, 0, tzinfo=UTC)  # 10:00 İstanbul
CLOCK = [NOW]
JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64).decode()


@pytest.fixture
async def ops(world: World, api_app: FastAPI, tmp_path: Path) -> AsyncIterator[World]:
    async with world.factory() as session, session.begin():
        plan = Plan(
            name="Operasyon", max_units=150, allowed_modules=["finance", "requests", "staff"]
        )
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    for key in ("requests", "staff"):
        assert (await world.post(f"/modules/{key}/toggle")).status_code == 200
    api_app.dependency_overrides[get_file_store] = lambda: FileStore(tmp_path)
    CLOCK[0] = NOW
    api_app.dependency_overrides[get_now] = lambda: CLOCK[0]
    yield world
    api_app.dependency_overrides.pop(get_now, None)


async def login(w: World, email: str) -> dict[str, str]:
    """Jeton gerçek saatle alınır (sabit saat geçmişte; jeton süresi dolmuş sayılırdı)."""
    w.app.dependency_overrides.pop(get_now, None)
    try:
        return await login_headers(w.api, email)
    finally:
        w.app.dependency_overrides[get_now] = lambda: CLOCK[0]


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["message"] == FORM
    return dict(body["fields"])


def code(response: httpx2.Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def staff(
    w: World, email: str, role: str, name: str = "Test KULLANICI"
) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, email, full_name=name)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return user.id, await login(w, email)


async def resident(w: World) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, "ayse@test.local", full_name="Ayşe YILMAZ")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    return user.id, await login(w, "ayse@test.local")


async def patch(w: World, path: str, body: dict[str, Any], **kw: Any) -> httpx2.Response:
    return await w.api.patch(w.url(path), json=body, headers=kw.pop("headers", w.headers))


async def notifications_of(w: World, user_id: uuid.UUID) -> list[Notification]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            return list(
                await session.scalars(
                    select(Notification)
                    .where(Notification.user_id == user_id)
                    .order_by(Notification.created_at)
                )
            )


async def audit_rows(w: World, entity: str) -> list[AuditLog]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            return list(await session.scalars(select(AuditLog).where(AuditLog.entity == entity)))


# --- 53 Anahtar ve emanet -----------------------------------------------------------


async def test_anahtar_teslim_iade_ve_yetkili_rol(ops: World) -> None:
    w = ops
    created = await w.post(
        "/keys",
        {
            "kind": "key",
            "code": "toplantı-1",
            "name": "Toplantı salonu",
            "authorized_roles": ["Görevli"],
        },
    )
    assert created.status_code == 201, created.text
    key = created.json()["data"]
    assert (key["code"], key["loan"], key["overdue"]) == ("TOPLANTI-1", None, False)
    assert fields(await w.post("/keys", {"kind": "x", "code": "TOPLANTI-1", "name": ""})) == {
        "name": "Ad yazın (ör. Toplantı salonu).",
        "code": "Bu kodla anahtar var.",
        "kind": "Tip seçin.",
    }
    assert "authorized_roles" in fields(
        await w.post(
            "/keys", {"kind": "card", "code": "K2", "name": "Kart", "authorized_roles": ["Patron"]}
        )
    )
    loan_body = {
        "to_name": "Hasan Demir",
        "to_phone": "0532 111 22 33",
        "role": "Görevli",
        "due_at": "2026-06-20T12:00:00Z",
    }
    # Yetkili rol dışındaki kişiye verilmez
    refused = await w.post(f"/keys/{key['id']}/loans", {**loan_body, "role": "Sakin"})
    assert code(refused) == (409, "not_authorized")
    assert "yetkili: Görevli" in refused.json()["error"]["message"]
    assert "due_at" in fields(
        await w.post(f"/keys/{key['id']}/loans", {**loan_body, "due_at": "2026-06-20T06:00:00Z"})
    )
    given = await w.post(f"/keys/{key['id']}/loans", loan_body)
    assert given.status_code == 201, given.text
    loan = given.json()["data"]
    assert (loan["to_phone"], loan["key_name"]) == ("+905321112233", "Toplantı salonu")
    assert code(await w.post(f"/keys/{key['id']}/loans", loan_body)) == (409, "key_out")

    # Gecikme: saat 13:00 → listede gecikmiş; saatlik iş yöneticiye bir kez bildirir
    CLOCK[0] = late_at = datetime(2026, 6, 20, 13, 0, tzinfo=UTC)
    listed = (await w.get("/keys")).json()
    assert listed[0]["overdue"] is True
    assert listed[0]["loan"]["id"] == loan["id"]
    first = await keys_svc.run_overdue_warnings(w.factory, now=late_at)
    again = await keys_svc.run_overdue_warnings(w.factory, now=late_at)
    assert first == [(w.site_id, 1)]
    assert again == [(w.site_id, 0)]

    returned = await w.post(f"/key-loans/{loan['id']}/return")
    assert returned.status_code == 200
    assert returned.json()["data"]["returned_to"] == "Kerem YILDIRIM"
    assert code(await w.post(f"/key-loans/{loan['id']}/return")) == (409, "already_returned")
    assert [x["id"] for x in (await w.get("/key-loans")).json()] == [loan["id"]]
    off = await patch(w, f"/keys/{key['id']}", {"is_active": False})
    assert off.json()["data"]["is_active"] is False
    assert off.json()["data"]["code"] == "TOPLANTI-1"
    assert code(await w.post(f"/keys/{key['id']}/loans", loan_body)) == (409, "key_inactive")
    # Telefon denetim kaydına maskeli
    created_rows = [r for r in await audit_rows(w, "key_loans") if r.action == "create"]
    assert created_rows[0].after is not None
    assert created_rows[0].after["to_phone"] == "+90******2233"


async def test_anahtar_yetki_ve_izolasyon(ops: World) -> None:
    w = ops
    _, guard = await staff(w, "guvenlik@test.local", "Güvenlik")
    made = await w.api.post(
        w.url("/keys"),
        json={"kind": "remote", "code": "B1", "name": "Bariyer kumandası"},
        headers=guard,
    )
    assert made.status_code == 201  # güvenlik masası (security.visitors)
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/keys"), headers=board)).status_code == 403
    assert (await w.api.get(w.url("/deposited-keys"), headers=board)).status_code == 403
    other = await w.api.get(w.url("/keys", "yildiz-sitesi"), headers=w.headers)
    assert other.json() == []
    key_id = made.json()["data"]["id"]
    assert (
        await w.api.patch(
            w.url(f"/keys/{key_id}", "yildiz-sitesi"), json={"name": "X Y"}, headers=w.headers
        )
    ).status_code == 404


async def test_emanet_anahtar_yalniz_yetkiliye_kimlik_kontrolu(ops: World) -> None:
    w = ops
    resident_id, me = await resident(w)
    assert "valid_until" in fields(
        await w.api.post(
            w.url("/resident/deposited-keys"),
            json={"authorized": [{"name": "İsmail Işık"}], "valid_until": "2026-06-19"},
            headers=me,
        )
    )
    assert (
        fields(
            await w.api.post(w.url("/resident/deposited-keys"), json={"authorized": []}, headers=me)
        )["authorized"]
        == "En az bir yetkili kişi yazın."
    )
    made = await w.api.post(
        w.url("/resident/deposited-keys"),
        json={
            "authorized": [{"name": "İsmail Işık", "phone": "05321234567"}],
            "valid_until": "2026-06-25",
        },
        headers=me,
    )
    assert made.status_code == 201, made.text
    deposit = made.json()["data"]
    assert (deposit["status"], deposit["unit_name"]) == ("requested", "A-1")
    assert deposit["authorized"] == [{"name": "İsmail Işık", "phone": "+905321234567"}]
    mine = (await w.api.get(w.url("/resident/deposited-keys"), headers=me)).json()
    assert [d["id"] for d in mine] == [deposit["id"]]
    # Personel uçlarına sakin giremez
    assert (await w.api.get(w.url("/deposited-keys"), headers=me)).status_code == 403

    path = f"/deposited-keys/{deposit['id']}"
    assert code(await w.post(f"{path}/hand", {"name": "İsmail Işık", "id_checked": True})) == (
        409,
        "invalid_status",
    )
    assert (await w.post(f"{path}/receive")).json()["data"]["status"] == "held"
    assert code(await w.post(f"{path}/receive")) == (409, "invalid_status")
    refused = await w.post(f"{path}/hand", {"name": "Yabancı Kişi", "id_checked": True})
    assert code(refused) == (409, "not_authorized")
    assert fields(await w.post(f"{path}/hand", {"name": "ismail ışık"})) == {
        "id_checked": "Kimlik kontrolünü işaretleyin."
    }
    handed = await w.post(f"{path}/hand", {"name": "ISMAİL IŞIK", "id_checked": True})
    assert handed.status_code == 200, handed.text
    assert handed.json()["data"]["handed_to"] == "İsmail Işık"
    kinds = [n.kind for n in await notifications_of(w, resident_id)]
    assert kinds == ["deposited_key.refused", "deposited_key.handed"]
    assert code(await w.post(f"{path}/return")) == (409, "invalid_status")

    # Süresi geçen emanet verilmez, listede "expired"
    second = (
        await w.api.post(
            w.url("/resident/deposited-keys"),
            json={"authorized": [{"name": "Can Ak"}], "valid_until": "2026-06-21"},
            headers=me,
        )
    ).json()["data"]
    await w.post(f"/deposited-keys/{second['id']}/receive")
    w.set_today(date(2026, 6, 22))
    listed = {d["id"]: d["status"] for d in (await w.get("/deposited-keys")).json()}
    assert listed[second["id"]] == "expired"
    late = await w.post(
        f"/deposited-keys/{second['id']}/hand", {"name": "Can Ak", "id_checked": True}
    )
    assert code(late) == (409, "expired")
    assert (await w.post(f"/deposited-keys/{second['id']}/return")).json()["data"][
        "status"
    ] == "returned"
    rows = [r for r in await audit_rows(w, "deposited_keys") if r.action == "create"]
    assert rows[0].after is not None
    assert rows[0].after["authorized"] == "***"


# --- 54 Stok sayımı -----------------------------------------------------------------


async def _item(w: World, name: str, quantity: str) -> str:
    item = (await w.post("/stock-items", {"name": name, "unit_label": "adet"})).json()["data"]
    moved = await w.post(
        f"/stock-items/{item['id']}/moves", {"direction": "in", "quantity": quantity}
    )
    assert moved.status_code == 201
    return str(item["id"])


async def test_stok_sayimi_fark_hareketi(ops: World) -> None:
    w = ops
    bulb, soap = await _item(w, "Ampul", "10"), await _item(w, "Sabun", "4.5")
    assert fields(await w.post("/stock-counts", {"lines": [{"item_id": bulb, "counted": ""}]})) == {
        "lines": "En az bir malzeme."
    }
    assert fields(
        await w.post("/stock-counts", {"lines": [{"item_id": bulb, "counted": "-1"}]})
    ) == {f"lines.{bulb}": "Geçersiz miktar."}
    assert code(
        await w.post("/stock-counts", {"lines": [{"item_id": str(uuid.uuid4()), "counted": "1"}]})
    ) == (404, "not_found")
    counted = await w.post(
        "/stock-counts",
        {"lines": [{"item_id": bulb, "counted": "8"}, {"item_id": soap, "counted": "4,5"}]},
    )
    assert counted.status_code == 201, counted.text
    body = counted.json()
    assert body["message"] == "Sayım kaydedildi; 1 malzemede fark düzeltme hareketi olarak yazıldı."
    lines = {x["name"]: x for x in body["data"]["lines"]}
    assert (lines["Ampul"]["system"], lines["Ampul"]["counted"], lines["Ampul"]["diff"]) == (
        "10",
        "8",
        "-2",
    )
    assert lines["Sabun"]["diff"] == "0"
    assert body["data"]["status"] == "applied"
    items = {i["name"]: i["quantity"] for i in (await w.get("/stock-items")).json()}
    assert items == {"Ampul": "8", "Sabun": "4.5"}
    moves = (await w.get(f"/stock-items/{bulb}/moves")).json()
    assert (moves[0]["direction"], moves[0]["quantity"], moves[0]["note"]) == (
        "out",
        "2",
        "Sayım farkı",
    )
    assert len((await w.get("/stock-counts")).json()) == 1
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/stock-counts"), headers=board)).status_code == 200
    assert (
        await w.api.post(w.url("/stock-counts"), json={"lines": []}, headers=board)
    ).status_code == 403
    assert (
        await w.api.get(w.url("/stock-counts", "yildiz-sitesi"), headers=w.headers)
    ).json() == []


async def test_stok_sayim_farki_onay_kuralindan_gecer(ops: World) -> None:
    w = ops
    bulb = await _item(w, "Ampul", "10")
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi", "Ayşe YK")
    types = (await w.get("/approval-action-types")).json()
    assert next(t for t in types if t["key"] == "stock_count_difference")["connected"] is True
    rule = await w.post(
        "/approval-rules",
        {
            "action_type": "stock_count_difference",
            "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": "2026-06-20",
        },
    )
    assert rule.status_code == 201, rule.text
    pending = await w.post("/stock-counts", {"lines": [{"item_id": bulb, "counted": "13"}]})
    assert pending.status_code == 202, pending.text
    data = pending.json()["data"]
    assert data["status"] == "approval_pending"
    assert data["approval_request_id"]
    assert "onaya gönderildi" in pending.json()["message"]
    assert (await w.get("/stock-items")).json()[0]["quantity"] == "10"  # henüz hareket yok
    approved = await w.api.post(
        w.url(f"/approvals/{data['approval_request_id']}/approve"), json={}, headers=board
    )
    assert approved.status_code == 200, approved.text
    assert (await w.get("/stock-items")).json()[0]["quantity"] == "13"
    assert (await w.get("/stock-counts")).json()[0]["status"] == "applied"

    # Ret → hareket yazılmaz, sayım "rejected"
    second = (await w.post("/stock-counts", {"lines": [{"item_id": bulb, "counted": "1"}]})).json()[
        "data"
    ]
    rejected = await w.api.post(
        w.url(f"/approvals/{second['approval_request_id']}/reject"),
        json={"comment": "Yeniden sayılsın"},
        headers=board,
    )
    assert rejected.status_code == 200, rejected.text
    statuses = {c["id"]: c["status"] for c in (await w.get("/stock-counts")).json()}
    assert statuses[second["id"]] == "rejected"
    assert (await w.get("/stock-items")).json()[0]["quantity"] == "13"

    # Onay beklerken stok düşerse çıkış yetmez → "onaylandı – yürütülemedi", sayım "failed"
    third = (await w.post("/stock-counts", {"lines": [{"item_id": bulb, "counted": "0"}]})).json()[
        "data"
    ]
    await w.post(f"/stock-items/{bulb}/moves", {"direction": "out", "quantity": "5"})
    failed = await w.api.post(
        w.url(f"/approvals/{third['approval_request_id']}/approve"), json={}, headers=board
    )
    assert failed.status_code == 200, failed.text
    statuses = {c["id"]: c["status"] for c in (await w.get("/stock-counts")).json()}
    assert statuses[third["id"]] == "failed"
    with site_scope(w.site_id):
        async with w.factory() as session:
            notes = list(await session.scalars(select(StockMove.note)))
    assert notes.count("Sayım farkı") == 1


# --- 55 Denetim listeleri -----------------------------------------------------------


async def test_denetim_sablon_skor_ve_talep(ops: World, tmp_path: Path) -> None:
    w = ops
    templates = (await w.get("/checklist-templates")).json()
    assert [t["name"] for t in templates] == [
        "Güvenlik turu",
        "Havuz hijyeni",
        "Sosyal tesis açılış kontrolü",
        "Taşınma hasar kontrolü",
        "Temizlik kalitesi",
    ]
    assert len((await w.get("/checklist-templates")).json()) == 5  # ikinci kez eklenmez
    pool = next(t for t in templates if t["name"] == "Havuz hijyeni")
    assert pool["items"][1] == {
        "text": "pH",
        "type": "number",
        "photo_required": False,
        "min": "7.2",
        "max": "7.8",
    }
    cleaning = next(t for t in templates if t["name"] == "Temizlik kalitesi")

    assert fields(
        await w.post("/inspections", {"template_id": cleaning["id"], "location": "x"})
    ) == {"location": "Konumu yazın."}
    missing = await w.post(
        "/inspections",
        {
            "template_id": cleaning["id"],
            "location": "A blok",
            "answers": [{"value": "yes"}, {"value": ""}, {"value": "no"}, {"value": "9"}],
        },
    )
    assert fields(missing) == {
        "answers.1": "Yanıtlayın.",
        "answers.2": "Bu madde için fotoğraf zorunlu.",
        "answers.3": "Geçersiz yanıt.",
    }
    done = await w.post(
        "/inspections",
        {
            "template_id": cleaning["id"],
            "location": "A blok giriş",
            "answers": [
                {"value": "yes"},
                {"value": "no"},
                {"value": "yes", "photo": JPEG},
                {"value": "4"},
            ],
        },
    )
    assert done.status_code == 201, done.text
    record = done.json()["data"]
    assert (record["score"], record["failed"], len(record["opened_request_ids"])) == (75, 1, 1)
    assert (
        done.json()["message"] == "Denetim kaydedildi: skor %75. 1 olumsuz madde için talep açıldı."
    )
    with site_scope(w.site_id):
        async with w.factory() as session:
            opened = await session.get(Request, uuid.UUID(record["opened_request_ids"][0]))
            assert opened is not None
            assert (opened.title, opened.category, opened.location) == (
                "Denetim: Asansör kabini temiz",
                "common_area",
                "A blok giriş",
            )
            with pytest.raises(DBAPIError):  # denetim değişmez
                await session.execute(text("UPDATE checklist_inspections SET score = 1"))
    photo_id = record["answers"][2]["photo_id"]
    photo = await w.get(f"/operation-photos/{photo_id}")
    assert photo.status_code == 200
    assert photo.headers["content-type"] == "image/jpeg"
    _, tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (
        await w.api.get(w.url(f"/operation-photos/{photo_id}"), headers=tech)
    ).status_code == 200
    _, guard = await staff(w, "guvenlik@test.local", "Güvenlik")
    assert (
        await w.api.get(w.url(f"/operation-photos/{photo_id}"), headers=guard)
    ).status_code == 403
    assert (await w.get(f"/operation-photos/{uuid.uuid4()}")).status_code == 404
    bad_photo = await w.post(
        "/inspections",
        {
            "template_id": cleaning["id"],
            "location": "B blok",
            "answers": [
                {"value": "yes"},
                {"value": "yes"},
                {"value": "yes", "photo": "data:image/png;base64,AAAA"},
                {"value": "5"},
            ],
        },
    )
    assert "answers.2" in fields(bad_photo)
    history = (await w.get("/inspections")).json()
    assert [h["id"] for h in history] == [record["id"]]

    # Şablon ekle / doğrula / pasifleştir
    assert fields(await w.post("/checklist-templates", {"name": "Havuz hijyeni", "items": []})) == {
        "name": "Bu adla bir şablon var.",
        "items": "En az bir madde ekleyin.",
    }
    assert "items.0" in fields(
        await w.post(
            "/checklist-templates",
            {"name": "Otopark", "items": [{"text": "Işık", "type": "number", "min": 5, "max": 1}]},
        )
    )
    new = await w.post(
        "/checklist-templates",
        {"name": "Otopark", "items": [{"text": "Aydınlatma", "type": "yesno"}]},
    )
    assert new.status_code == 201
    off = await patch(w, f"/checklist-templates/{new.json()['data']['id']}", {"is_active": False})
    assert off.json()["data"]["is_active"] is False
    assert "template_id" in fields(
        await w.post(
            "/inspections",
            {
                "template_id": new.json()["data"]["id"],
                "location": "Otopark",
                "answers": [{"value": "yes"}],
            },
        )
    )
    # Talep modülü kapalıysa talep açılmaz
    assert (await w.post("/modules/requests/toggle")).status_code == 200
    quiet = await w.post(
        "/inspections",
        {
            "template_id": pool["id"],
            "location": "Havuz",
            "answers": [{"value": "5"}, {"value": "7.5"}, {"value": "yes"}],
        },
    )
    assert quiet.status_code == 201
    assert quiet.json()["data"]["opened_request_ids"] == []
    assert (
        len(await asyncio.to_thread(lambda: list(tmp_path.rglob("*.jpg")))) == 1
    )  # red: diske yok
    # yetki ve izolasyon
    assert (await w.api.get(w.url("/inspections"), headers=guard)).status_code == 403
    assert (await w.api.get(w.url("/inspections", "yildiz-sitesi"), headers=w.headers)).json() == []


# --- 56 Sigorta ---------------------------------------------------------------------

POLICY = {
    "kind": "building",
    "insurer": "Anadolu Sigorta",
    "policy_no": "DP-2026-001",
    "coverage": "Deprem ve yangın",
    "premium": "12500.00",
    "start": "2025-08-01",
    "end": "2026-08-10",
}


async def test_police_uyari_hasar_ve_belge(ops: World) -> None:
    w = ops
    assert fields(
        await w.post(
            "/insurance-policies", {"kind": "x", "start": "2026-01-01", "end": "2025-01-01"}
        )
    ) == {
        "kind": "Tip seçin.",
        "insurer": "Sigortacıyı yazın.",
        "policy_no": "Poliçe no yazın.",
        "premium": "Primi girin (sıfır ya da daha büyük).",
        "end": "Bitiş başlangıçtan sonra olmalı.",
    }
    created = await w.post("/insurance-policies", POLICY)
    assert created.status_code == 201, created.text
    policy = created.json()["data"]
    assert (policy["days_left"], policy["warning"], policy["premium"]) == (51, "60", "12500.00")
    other = (
        await w.post("/insurance-policies", {**POLICY, "policy_no": "C-1", "end": "2026-07-01"})
    ).json()["data"]
    assert other["warning"] == "30"
    assert [p["policy_no"] for p in (await w.get("/insurance-policies")).json()] == [
        "C-1",
        "DP-2026-001",
    ]
    edited = await patch(w, f"/insurance-policies/{policy['id']}", {"end": "2027-08-10"})
    assert edited.json()["data"]["warning"] is None
    assert edited.json()["data"]["insurer"] == "Anadolu Sigorta"

    # Günlük iş: 60/30 uyarısı ve bitince bir kez
    first = await insurance_svc.run_expiry_warnings(w.factory, today=date(2026, 6, 20))
    again = await insurance_svc.run_expiry_warnings(w.factory, today=date(2026, 6, 20))
    later = await insurance_svc.run_expiry_warnings(w.factory, today=date(2026, 7, 2))
    assert (first, again, later) == ([(w.site_id, 1)], [(w.site_id, 0)], [(w.site_id, 1)])

    # Hasar dosyası
    assert fields(await w.post("/insurance-claims", {"incident_date": "2026-06-21"})) == {
        "policy_id": "Poliçe seçin.",
        "description": "Hasarı kısaca anlatın.",
        "incident_date": "Hasar tarihi (bugün ya da önce).",
    }
    claim = (
        await w.post(
            "/insurance-claims",
            {
                "policy_id": policy["id"],
                "source": "talep #12",
                "description": "B blok su baskını",
                "incident_date": "2026-06-18",
                "claimed": "40000.00",
            },
        )
    ).json()["data"]
    assert claim["status"] == "open"
    assert claim["policy_label"].startswith("Bina (deprem/yangın) · Anadolu")
    path = f"/insurance-claims/{claim['id']}"
    assert code(await patch(w, path, {"status": "paid", "paid": "1.00"})) == (409, "invalid_status")
    assert (await patch(w, path, {"status": "submitted"})).json()["data"]["status"] == "submitted"
    assert fields(await patch(w, path, {"status": "paid"})) == {"paid": "Ödenen tazminatı girin."}
    paid = await patch(w, path, {"status": "paid", "paid": "35000.00"})
    assert paid.json()["data"]["paid_on"] == "2026-06-20"
    assert paid.json()["data"]["paid"] == "35000.00"
    assert code(await patch(w, path, {"status": "open"})) == (409, "invalid_status")

    # Belge arşivine bağlı poliçe belgesi
    upload = await w.api.post(
        w.url("/subject-documents"),
        data={"subject_type": "insurance_policies", "subject_id": policy["id"], "title": "Poliçe"},
        files={"file": ("police.pdf", b"%PDF-1.4 police", "application/pdf")},
        headers=w.headers,
    )
    assert upload.status_code == 201, upload.text
    document = upload.json()["data"]
    assert document["title"] == "Poliçe DP-2026-001 (Anadolu Sigorta): Poliçe"
    listed = (await w.get("/insurance-policies")).json()
    assert (
        next(p for p in listed if p["id"] == policy["id"])["documents"][0]["id"] == document["id"]
    )
    download = await w.get(f"/subject-documents/{document['id']}/download")
    assert download.status_code == 200
    assert download.content == b"%PDF-1.4 police"
    assert (
        await w.get(
            "/subject-documents",
            params={"subject_type": "insurance_policies", "subject_id": policy["id"]},
        )
    ).json()[0]["version"] == 1
    bad = await w.api.post(
        w.url("/subject-documents"),
        data={"subject_type": "payments", "subject_id": policy["id"]},
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        headers=w.headers,
    )
    assert "subject_type" in fields(bad)
    missing = await w.api.post(
        w.url("/subject-documents"),
        data={"subject_type": "insurance_claims", "subject_id": str(uuid.uuid4())},
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
        headers=w.headers,
    )
    assert missing.status_code == 404

    # Yetki: muhasebe okur ve yazar, yönetim kurulu yalnız okur; teknik ekip göremez
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/insurance-policies"), headers=board)).status_code == 200
    assert (
        await w.api.post(w.url("/insurance-policies"), json=POLICY, headers=board)
    ).status_code == 403
    _, tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await w.api.get(w.url("/insurance-claims"), headers=tech)).status_code == 403
    assert (
        await w.api.get(w.url(f"/subject-documents/{document['id']}/download"), headers=tech)
    ).status_code == 403
    assert (
        await w.api.get(w.url("/insurance-policies", "yildiz-sitesi"), headers=w.headers)
    ).json() == []
    assert (
        await w.api.get(
            w.url(f"/subject-documents/{document['id']}/download", "yildiz-sitesi"),
            headers=w.headers,
        )
    ).status_code == 404


# --- 57 Sayaç ve okuma --------------------------------------------------------------


async def _read(
    w: World, meter: str, period: str, value: str | None = None, **extra: Any
) -> httpx2.Response:
    body: dict[str, Any] = {"meter_id": meter, "period": period, **extra}
    if value is not None:
        body["value"] = value
    return await w.post("/meter-readings", body)


async def test_sayac_okuma_kurallari(ops: World) -> None:
    w = ops
    assert fields(await w.post("/meters", {"kind": "gas"})) == {
        "serial": "Seri no yazın.",
        "unit_id": "Bölüm ya da ortak alan seçin.",
    }
    meter = (
        await w.post(
            "/meters", {"kind": "heat", "serial": "ISI-1", "unit_id": w.unit_id, "multiplier": "2"}
        )
    ).json()["data"]
    assert (meter["unit_name"], meter["unit_label"], meter["multiplier"]) == ("A-1", "birim", "2")
    assert fields(
        await w.post("/meters", {"kind": "heat", "serial": "ISI-1", "area": "Kazan"})
    ) == {"serial": "Bu seri no kayıtlı."}
    common = (
        await w.post("/meters", {"kind": "common", "serial": "OS-1", "area": "Bahçe"})
    ).json()["data"]
    assert common["unit_label"] == "m³"
    # PATCH kısmidir: gönderilmeyen `status` korunur (ekleme gövdesindeki varsayılan uygulanmaz)
    faulty = (await patch(w, f"/meters/{common['id']}", {"status": "faulty"})).json()["data"]
    renamed = (await patch(w, f"/meters/{common['id']}", {"area": "Ön bahçe"})).json()["data"]
    assert (faulty["status"], renamed["status"], renamed["area"]) == (
        "faulty",
        "faulty",
        "Ön bahçe",
    )
    assert (await patch(w, f"/meters/{common['id']}", {"status": "active"})).status_code == 200
    m = meter["id"]
    assert fields(await _read(w, m, "2026-13", "1")) == {"period": "Dönem seçin."}
    assert (await _read(w, m, "2026-03", "1000")).json()["data"]["consumption"] is None
    april = (await _read(w, m, "2026-04", "1100")).json()["data"]
    assert (april["consumption"], april["status"]) == ("200", "ok")
    assert code(await _read(w, m, "2026-04", "1200")) == (409, "already_read")
    # S1: önceki 1.250, yeni 1.180 → reddedilir
    may = await _read(w, m, "2026-05", "1250")
    assert may.json()["data"]["anomaly_percent"] == 50
    assert may.json()["data"]["status"] == "ok"
    decreased = await _read(w, m, "2026-06", "1180")
    assert code(decreased) == (422, "index_decreased")
    assert decreased.json()["error"]["message"].startswith(
        "Endeks önceki okumadan (1250) küçük olamaz"
    )
    # %50'den fazla sapma → anormal; yeniden okuma istenir, sonra yeniden okunur ve onaylanır
    anomaly = (await _read(w, m, "2026-06", "1500", photo=JPEG)).json()
    assert anomaly["data"]["status"] == "anomaly"
    assert anomaly["data"]["anomaly_percent"] == 67
    assert "yönetici onayı bekliyor" in anomaly["message"]
    assert anomaly["data"]["photo_id"] is not None
    reread = await w.post(f"/meter-readings/{anomaly['data']['id']}/reread")
    assert reread.json()["data"]["status"] == "reread"
    assert code(await w.post(f"/meter-readings/{anomaly['data']['id']}/approve")) == (
        409,
        "invalid_status",
    )
    again = (await _read(w, m, "2026-06", "1490")).json()["data"]
    assert again["status"] == "anomaly"
    assert (await w.post(f"/meter-readings/{again['id']}/approve")).json()["data"][
        "status"
    ] == "approved"

    # Okunamadı → eksik sayılır, sonra okunabilir
    unreadable = (
        await _read(w, common["id"], "2026-06", unreadable_reason="Sayaç erişilemez")
    ).json()["data"]
    assert unreadable["status"] == "unreadable"
    assert unreadable["value"] is None
    period = (await w.get("/meter-readings", params={"period": "2026-06"})).json()
    assert period["missing"] == 1
    rows = {r["meter"]["serial"]: r for r in period["rows"]}
    assert rows["ISI-1"]["reading"]["value"] == "1490"
    assert rows["ISI-1"]["last"]["value"] == "1250"
    assert (await _read(w, common["id"], "2026-06", "10")).json()["data"]["status"] == "ok"
    assert (await w.get("/meter-readings", params={"period": "2026-06"})).json()["missing"] == 0

    # Sayaç değişimi: eski son endeks + yeni ilk endeks; tüketim ikisinin toplamı
    replaced = await _read(
        w,
        m,
        "2026-07",
        "30",
        replacement={"old_final": "1600", "new_serial": "ISI-2", "new_initial": "10"},
    )
    assert replaced.status_code == 201, replaced.text
    assert replaced.json()["data"]["consumption"] == "260"  # (1600−1490)×2 + (30−10)×2
    assert replaced.json()["message"] == "Sayaç değişimi kaydedildi; yeni sayaç ISI-2 açıldı."
    serials = {
        x["serial"]: x["status"]
        for x in (await w.get("/meters", params={"include_replaced": True})).json()
    }
    assert serials == {"ISI-1": "replaced", "ISI-2": "active", "OS-1": "active"}
    assert code(await _read(w, m, "2026-08", "1600")) == (409, "meter_replaced")
    # Ayar: eşik ve fotoğraf zorunluluğu
    assert (await w.get("/operation-settings")).json() == {
        "meter_anomaly_percent": 50,
        "meter_photo_required": False,
    }
    assert "meter_anomaly_percent" in fields(
        await w.api.put(
            w.url("/operation-settings"), json={"meter_anomaly_percent": 0}, headers=w.headers
        )
    )
    saved = await w.api.put(
        w.url("/operation-settings"), json={"meter_photo_required": True}, headers=w.headers
    )
    assert saved.json()["data"]["meter_photo_required"] is True
    assert fields(await _read(w, common["id"], "2026-07", "20")) == {
        "photo": "Sayaç fotoğrafı zorunlu (site ayarı)."
    }

    # Sakin yalnız kendi bölümünün sayaçlarını görür (değişen sayaç geçmişiyle)
    _, me = await resident(w)
    mine = (await w.api.get(w.url("/resident/meter-readings"), headers=me)).json()
    assert {x["meter"]["serial"] for x in mine} == {"ISI-1", "ISI-2"}
    old = next(x for x in mine if x["meter"]["serial"] == "ISI-1")
    assert [r["period"] for r in old["readings"]][:2] == ["2026-07", "2026-06"]
    photo_reading = anomaly["data"]["id"]
    assert (
        await w.api.get(w.url(f"/resident/meter-readings/{photo_reading}/photo"), headers=me)
    ).status_code == 404  # reread
    with_photo = (await _read(w, mine[1]["meter"]["id"], "2026-08", "40", photo=JPEG)).json()[
        "data"
    ]
    assert (
        await w.api.get(w.url(f"/resident/meter-readings/{with_photo['id']}/photo"), headers=me)
    ).status_code == 200
    # Yetki ve izolasyon
    _, tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await w.api.get(w.url("/meters"), headers=tech)).status_code == 403
    assert (await w.api.get(w.url("/meters", "yildiz-sitesi"), headers=w.headers)).json() == []
    assert code(
        await w.api.post(
            w.url("/meter-readings", "yildiz-sitesi"),
            json={"meter_id": m, "period": "2026-06", "value": "1"},
            headers=w.headers,
        )
    ) == (404, "not_found")


# --- 58 Rutin görevler (K29) --------------------------------------------------------

TASK = {
    "name": "Çöplerin toplanması",
    "frequency": "daily",
    "time_from": "08:00",
    "time_to": "10:00",
    "location": "Tüm bloklar",
    "assignee": "Görevli",
    "photo_required": True,
}


async def test_rutin_gorev_uretim_kapatma_ve_rapor(ops: World) -> None:
    w = ops
    assert fields(
        await w.post(
            "/task-templates",
            {
                "name": "",
                "frequency": "monthly",
                "month_day": 30,
                "time_from": "10:00",
                "time_to": "09:00",
            },
        )
    ) == {
        "name": "Görev adını yazın.",
        "month_day": "Ayın 1–28. günü.",
        "time_to": "Saat aralığını kontrol edin (bitiş başlangıçtan sonra).",
        "assignee": "Rol ya da kişi yazın.",
    }
    assert "weekdays" in fields(
        await w.post("/task-templates", {**TASK, "frequency": "weekdays", "weekdays": []})
    )
    daily = (await w.post("/task-templates", TASK)).json()["data"]
    assert (daily["time_from"], daily["weekdays"], daily["staff_name"]) == ("08:00", [], None)
    # Cumartesi (20.06.2026) değil yalnız pazartesi
    weekly = (
        await w.post(
            "/task-templates",
            {
                **TASK,
                "name": "Havuz kontrolü",
                "frequency": "weekdays",
                "weekdays": [0],
                "photo_required": False,
                "time_from": "11:00",
                "time_to": "12:00",
            },
        )
    ).json()["data"]
    today = (await w.get("/tasks")).json()
    assert [(t["template"]["name"], t["status"]) for t in today] == [("Çöplerin toplanması", "due")]
    monday = (await w.get("/tasks", params={"date": "2026-06-22"})).json()
    assert [t["status"] for t in monday] == ["upcoming", "upcoming"]

    path = f"/tasks/{daily['id']}/complete"
    assert code(await w.post(path, {"status": "done"})) == (422, "photo_required")
    assert fields(await w.post(path, {"status": "failed", "reason": ""})) == {
        "reason": "Tamamlanamama nedenini seçin."
    }
    assert code(await w.post(f"/tasks/{weekly['id']}/complete", {})) == (404, "not_found")
    assert code(await w.post(path, {"date": "2026-06-21", "photo": JPEG})) == (409, "future_task")
    closed = await w.post(path, {"photo": JPEG, "note": "Tamam"})
    assert closed.status_code == 201, closed.text
    assert closed.json()["data"]["photo_id"] is not None
    assert code(await w.post(path, {"photo": JPEG})) == (409, "already_done")
    assert (await w.get("/tasks")).json()[0]["status"] == "done"
    failed = await w.post(path, {"date": "2026-06-19", "status": "failed", "reason": "Malzeme yok"})
    assert failed.json()["data"]["status"] == "failed"
    with site_scope(w.site_id):
        async with w.factory() as session:
            with pytest.raises(DBAPIError):  # kapanış değişmez
                await session.execute(text("UPDATE task_completions SET note = 'x'"))

    # Saat 10:00'ı geçince "gecikti"; gün sonu özeti yöneticiye bir kez
    CLOCK[0] = datetime(2026, 6, 20, 8, 0, tzinfo=UTC)
    assert (await w.get("/tasks", params={"date": "2026-06-18"})).json()[0]["status"] == "late"
    summary = await tasks_svc.run_daily_summary(w.factory, today=date(2026, 6, 19))
    assert summary == [(w.site_id, 1)]
    assert await tasks_svc.run_daily_summary(w.factory, today=date(2026, 6, 19)) == [(w.site_id, 0)]

    report = (await w.get("/tasks/report")).json()
    assert (report["from"], report["to"]) == ("2026-06-13", "2026-06-19")
    row = next(r for r in report["rows"] if r["name"] == "Çöplerin toplanması")
    assert (row["done"], row["failed"]) == (0, 1)

    off = await patch(w, f"/task-templates/{weekly['id']}", {"is_active": False})
    assert off.json()["data"]["is_active"] is False
    # Görüntüleme requests.read; şablon yönetimi requests.assign
    _, board = await staff(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/tasks"), headers=board)).status_code == 200
    assert (await w.api.post(w.url("/task-templates"), json=TASK, headers=board)).status_code == 403
    assert (await w.api.get(w.url("/tasks", "yildiz-sitesi"), headers=w.headers)).json() == []


async def test_gorevlerim_personel_kartina_bagli(ops: World) -> None:
    w = ops
    user_id, tech = await staff(w, "teknik@test.local", "Teknik Personel", "Hasan DEMİR")
    assert code(await w.api.get(w.url("/me/tasks"), headers=tech)) == (403, "forbidden")
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            card = StaffMember(
                full_name="Hasan Demir",
                position="Teknik",
                employer="site",
                start_date=date(2026, 1, 1),
                user_id=user_id,
            )
            session.add(card)
            await session.flush()
            card_id = str(card.id)
    assert "staff_member_id" in fields(
        await w.post("/task-templates", {**TASK, "staff_member_id": str(uuid.uuid4())})
    )
    mine = (
        await w.post(
            "/task-templates",
            {**TASK, "assignee": "", "staff_member_id": card_id, "photo_required": False},
        )
    ).json()["data"]
    assert (mine["assignee"], mine["staff_name"]) == ("Hasan Demir", "Hasan Demir")
    others = (await w.post("/task-templates", {**TASK, "name": "Bahçe sulama"})).json()["data"]
    own = (await w.api.get(w.url("/me/tasks"), headers=tech)).json()
    assert [t["template"]["id"] for t in own] == [mine["id"]]
    filtered = (await w.get("/tasks", params={"staff_member_id": card_id})).json()
    assert [t["template"]["id"] for t in filtered] == [mine["id"]]
    assert code(
        await w.api.post(
            w.url(f"/me/tasks/{others['id']}/complete"), json={"photo": JPEG}, headers=tech
        )
    ) == (404, "not_found")
    done = await w.api.post(w.url(f"/me/tasks/{mine['id']}/complete"), json={}, headers=tech)
    assert done.status_code == 201, done.text
    assert done.json()["data"]["by"] == "Hasan DEMİR"
