"""Yönetim paketi — frontend servis istekleri 14–18, issue #42–#46 (gerçek PostgreSQL).

Kurgu `finance_world` (bugün 20.06.2026): toplantı, anket ve personel modülleri planla açılır;
sözleşme ve demirbaş/stok çekirdek. Ayşe Yılmaz A-1'in maliki; sakin hesabıyla oy verir.
"""

import asyncio
import uuid
from datetime import date
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, Plan, Site, StockMove
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

MODULES = ["finance", "general-assembly", "surveys", "staff"]


@pytest.fixture
async def mgmt(world: World) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Geniş", max_units=150, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    for key in MODULES[1:]:
        assert (await world.post(f"/modules/{key}/toggle")).status_code == 200
    return world


async def staff_headers(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def resident_headers(w: World, email: str = "ayse@test.local") -> dict[str, str]:
    user = await create_user(w.factory, email, full_name="Ayşe YILMAZ")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    return await login_headers(w.api, email)


async def patch(w: World, path: str, body: dict[str, Any]) -> httpx2.Response:
    return await w.api.patch(w.url(path), json=body, headers=w.headers)


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["message"] == "Formda düzeltilmesi gereken alanlar var."
    return dict(body["fields"])


async def audited(w: World, entity: str) -> list[str]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            rows = await session.scalars(select(AuditLog.action).where(AuditLog.entity == entity))
            return list(rows)


# --- 14 Toplantı --------------------------------------------------------------------

MEETING = {
    "kind": "general_ordinary",
    "title": "2026 olağan genel kurulu",
    "scheduled_at": "2026-07-15T16:30:00Z",
    "location": "Sosyal tesis salonu",
    "agenda": ["Açılış ve divan heyetinin seçimi", "Bütçenin görüşülmesi", "Dilek ve temenniler"],
}


async def test_toplanti_planla_karar_kilitle(mgmt: World) -> None:
    w = mgmt
    bad = await w.post("/meetings", {"kind": "x", "title": "", "agenda": []})
    assert set(fields(bad)) == {"kind", "title", "scheduled_at", "location", "agenda"}
    too_many = await w.post("/meetings", {**MEETING, "agenda": ["Madde"] * 31})
    assert fields(too_many) == {"agenda": "1 ile 30 arasında gündem maddesi yazın."}

    created = await w.post("/meetings", MEETING)
    assert created.status_code == 201, created.text
    assert created.json()["message"] == '"2026 olağan genel kurulu" planlandı.'
    meeting = created.json()["data"]
    assert (meeting["number"], meeting["status"], meeting["created_by"]) == (
        1,
        "planned",
        "Kerem YILDIRIM",
    )
    assert [(a["order"], a["result"]) for a in meeting["agenda"]] == [
        (1, None),
        (2, None),
        (3, None),
    ]
    items = meeting["agenda"]
    url = f"/meetings/{meeting['id']}/decisions"

    missing = await w.post(
        url,
        {
            "attendance_note": "48 bölümden 31'i katıldı",
            "items": [
                {"id": items[0]["id"], "result": "accepted", "decision": "Divan seçildi."},
                {"id": items[1]["id"], "result": "accepted", "decision": "  "},
            ],
        },
    )
    assert fields(missing) == {
        "items.2": "2. maddenin karar metnini yazın.",
        "items.3": "3. maddenin sonucunu seçin.",
    }
    negative = await w.post(
        url,
        {
            "attendance_note": "31 bölüm",
            "items": [
                {"id": i["id"], "result": "info", "votes_for": -1 if i["order"] == 1 else None}
                for i in items
            ],
        },
    )
    assert fields(negative) == {"items.1": "1. maddenin oy sayıları sıfır ya da daha büyük olmalı."}
    assert "attendance_note" in fields(await w.post(url, {"items": []}))

    decided = await w.post(
        url,
        {
            "attendance_note": "48 bölümden 31'i katıldı veya temsil edildi",
            "items": [
                {"id": items[0]["id"], "result": "accepted", "decision": "Divan seçildi."},
                {
                    "id": items[1]["id"],
                    "result": "accepted",
                    "decision": "Bütçe kabul edildi.",
                    "votes_for": 27,
                    "votes_against": 3,
                    "votes_abstain": 1,
                },
                {"id": items[2]["id"], "result": "info"},  # bilgi: karar metni gerekmez
            ],
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["message"] == (
        "Kararlar kaydedildi; toplantı yapıldı olarak işaretlendi. Kayıt artık değiştirilemez."
    )
    held = decided.json()["data"]
    assert held["status"] == "held"
    assert held["held_at"] is not None
    assert (held["agenda"][1]["votes_for"], held["agenda"][2]["decision"]) == (27, None)

    again = await w.post(url, {"attendance_note": "x", "items": []})
    assert again.status_code == 409
    assert (again.json()["error"]["code"], again.json()["error"]["message"]) == (
        "not_planned",
        "Kararlar yalnız planlanan toplantıya girilir.",
    )
    cancel_held = await w.post(f"/meetings/{held['id']}/cancel", {"reason": "Vazgeçildi"})
    assert cancel_held.status_code == 409

    board = (await w.post("/meetings", {**MEETING, "kind": "board", "title": "Kurul"})).json()
    board_id = board["data"]["id"]
    assert board["data"]["number"] == 2
    assert fields(await w.post(f"/meetings/{board_id}/cancel", {"reason": ""})) == {
        "reason": "Gerekçe yazın."
    }
    cancelled = await w.post(f"/meetings/{board_id}/cancel", {"reason": "Salon dolu"})
    assert cancelled.json()["message"] == '"Kurul" iptal edildi.'
    assert cancelled.json()["data"]["cancel_reason"] == "Salon dolu"

    listed = (await w.get("/meetings", params={"status": "held"})).json()
    assert (listed["total"], listed["items"][0]["id"]) == (1, held["id"])
    assert (await w.get("/meetings")).json()["total"] == 2
    detail = await w.get(f"/meetings/{held['id']}")
    assert detail.json()["attendance_note"] == "48 bölümden 31'i katıldı veya temsil edildi"
    assert (await w.get(f"/meetings/{uuid.uuid4()}")).status_code == 404
    assert "create" in await audited(w, "meetings")


async def test_toplanti_yetki_modul_izolasyon(mgmt: World) -> None:
    w = mgmt
    meeting = (await w.post("/meetings", MEETING)).json()["data"]
    auditor = await staff_headers(w, "denetci@test.local", "Denetçi")
    assert (await w.api.get(w.url("/meetings"), headers=auditor)).status_code == 200
    assert (await w.api.post(w.url("/meetings"), json=MEETING, headers=auditor)).status_code == 403
    tech = await staff_headers(w, "teknik@test.local", "Teknik Personel")
    assert (await w.api.get(w.url("/meetings"), headers=tech)).status_code == 403

    # Yıldız'da modül kapalı → 404; Aksu'nun toplantısı Yıldız adresinden de 404
    other = await w.api.get(w.url(f"/meetings/{meeting['id']}", "yildiz-sitesi"), headers=w.headers)
    assert other.status_code == 404
    assert (await w.post("/modules/general-assembly/toggle")).status_code == 200
    assert (await w.get("/meetings")).status_code == 404


# --- 15 Anket -----------------------------------------------------------------------

POLL = {
    "question": "Havuz açılış saati 08:00 olsun mu?",
    "options": ["Evet, 08:00", "Hayır, 09:00 kalsın"],
    "audience": "all",
    "ends_on": "2026-06-25",
}


async def second_unit(w: World) -> str:
    """Ayşe'nin ikinci bölümü (A-2, malik)."""
    unit = (await w.post("/units", {"block_id": w.lookups["block"], "number": "2"})).json()
    response = await w.post(
        f"/units/{unit['data']['id']}/parties",
        {"role": "owner", "start_date": "2020-01-01", "person_id": w.person_id},
    )
    assert response.status_code == 201, response.text
    return str(unit["data"]["id"])


async def test_anket_ac_dogrula_kapat(mgmt: World) -> None:
    w = mgmt
    bad = await w.post(
        "/polls", {"question": "?", "options": ["Tek"], "audience": "x", "ends_on": "2026-06-19"}
    )
    assert fields(bad) == {
        "question": "Soruyu yazın (3–300 karakter).",
        "options": "2 ile 8 arasında seçenek yazın.",
        "audience": "Kimin oy vereceğini seçin.",
        "ends_on": "Bitiş tarihi bugünden önce olamaz.",
    }
    same = await w.post("/polls", {**POLL, "options": ["Evet", "evet ", "Hayır"]})
    assert fields(same) == {"options": "Aynı seçenek iki kez yazılmış."}

    created = await w.post("/polls", POLL)
    assert created.status_code == 201, created.text
    assert created.json()["message"] == "Anket açıldı; sakinler kendi ekranlarında görüyor."
    poll = created.json()["data"]
    assert (poll["status"], poll["total_votes"], poll["created_by"]) == (
        "open",
        0,
        "Kerem YILDIRIM",
    )
    assert [o["votes"] for o in poll["options"]] == [0, 0]

    closed = await w.post(f"/polls/{poll['id']}/close")
    assert closed.json()["message"] == "Anket kapatıldı; sonuç sakinlere açıldı."
    assert closed.json()["data"]["status"] == "closed"
    again = await w.post(f"/polls/{poll['id']}/close")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_closed")

    later = (await w.post("/polls", {**POLL, "ends_on": "2026-06-20"})).json()["data"]
    assert [
        p["id"] for p in (await w.get("/polls", params={"status": "open"})).json()["items"]
    ] == [later["id"]]
    w.set_today(date(2026, 6, 21))  # ends_on günü dahil açıktı; ertesi gün kapanır
    assert (await w.get("/polls", params={"status": "open"})).json()["total"] == 0
    assert (await w.get("/polls", params={"status": "closed"})).json()["total"] == 2
    assert "create" in await audited(w, "polls")


async def test_sakin_oyu_gizli_bolum_basina_bir(mgmt: World) -> None:
    w = mgmt
    unit_2 = await second_unit(w)
    poll = (await w.post("/polls", POLL)).json()["data"]
    yes, no = (o["id"] for o in poll["options"])
    ayse = await resident_headers(w)

    def url(path: str) -> str:
        return w.url(f"/resident/polls{path}")

    # Sakin personel ucundan ara sonucu göremez
    assert (await w.api.get(w.url("/polls"), headers=ayse)).status_code == 403

    [mine] = (await w.api.get(url(""), headers=ayse)).json()
    assert (mine["total_votes"], [o["votes"] for o in mine["options"]]) == (None, [None, None])
    assert sorted(v["unit_name"] for v in mine["my_votes"]) == ["A-1", "A-2"]
    assert all(v["option_id"] is None for v in mine["my_votes"])

    voted = await w.api.post(
        url(f"/{poll['id']}/vote"), json={"unit_id": w.unit_id, "option_id": yes}, headers=ayse
    )
    assert voted.status_code == 200, voted.text
    assert voted.json() == {"data": {"option_id": yes}, "message": "Oyunuz kaydedildi."}
    again = await w.api.post(
        url(f"/{poll['id']}/vote"), json={"unit_id": w.unit_id, "option_id": no}, headers=ayse
    )
    assert (again.status_code, again.json()["error"]["message"]) == (
        409,
        "Bu bölüm adına zaten oy verildi.",
    )

    # Eşzamanlı iki oy aynı bölüm adına: biri geçer, öteki 409 (benzersiz kısıt)
    both = await asyncio.gather(
        *(
            w.api.post(
                url(f"/{poll['id']}/vote"), json={"unit_id": unit_2, "option_id": no}, headers=ayse
            )
            for _ in range(2)
        )
    )
    assert sorted(r.status_code for r in both) == [200, 409]

    [after] = (await w.api.get(url(""), headers=ayse)).json()
    assert (after["total_votes"], [o["votes"] for o in after["options"]]) == (2, [1, 1])
    staff_view = (await w.get("/polls")).json()["items"][0]
    assert staff_view["total_votes"] == 2
    assert "my_votes" not in staff_view  # kim neye oy verdi personelde yok
    assert await audited(w, "poll_votes") == []  # gizli oy denetim kaydına yazılmaz

    stranger = await w.api.post(
        url(f"/{poll['id']}/vote"),
        json={"unit_id": str(uuid.uuid4()), "option_id": yes},
        headers=ayse,
    )
    assert (stranger.status_code, stranger.json()["error"]["message"]) == (
        404,
        "Bu bölüm adına oy veremezsiniz.",
    )
    tenants = (await w.post("/polls", {**POLL, "audience": "tenants"})).json()["data"]
    not_eligible = await w.api.post(
        url(f"/{tenants['id']}/vote"),
        json={"unit_id": w.unit_id, "option_id": tenants["options"][0]["id"]},
        headers=ayse,
    )
    assert (not_eligible.status_code, not_eligible.json()["error"]["code"]) == (
        403,
        "not_eligible",
    )
    wrong_option = await w.api.post(
        url(f"/{tenants['id']}/vote"), json={"unit_id": w.unit_id, "option_id": yes}, headers=ayse
    )
    assert wrong_option.status_code == 403  # önce hedef kitle

    w.set_today(date(2026, 6, 26))
    late = await w.api.post(
        url(f"/{poll['id']}/vote"), json={"unit_id": unit_2, "option_id": yes}, headers=ayse
    )
    assert (late.status_code, late.json()["error"]["code"]) == (409, "poll_closed")
    listed = (await w.api.get(url(""), headers=ayse)).json()
    closed = next(p for p in listed if p["id"] == poll["id"])
    assert (closed["status"], closed["total_votes"]) == ("closed", 2)
    # Kapanan, oy verilmemiş ankette sonuç açılır
    other = next(p for p in listed if p["id"] == tenants["id"])
    assert (other["status"], other["total_votes"], other["my_votes"]) == ("closed", 0, [])

    w.set_today(date(2026, 8, 1))  # 30 günden eski kapananlar sakin listesinden düşer
    assert (await w.api.get(url(""), headers=ayse)).json() == []


async def test_anket_secenek_ve_izolasyon(mgmt: World) -> None:
    w = mgmt
    poll = (await w.post("/polls", POLL)).json()["data"]
    ayse = await resident_headers(w)
    bad = await w.api.post(
        w.url(f"/resident/polls/{poll['id']}/vote"),
        json={"unit_id": w.unit_id, "option_id": str(uuid.uuid4())},
        headers=ayse,
    )
    assert bad.json()["error"]["fields"] == {"option_id": "Bir seçenek işaretleyin."}
    missing = await w.api.post(
        w.url(f"/resident/polls/{uuid.uuid4()}/vote"),
        json={"unit_id": w.unit_id, "option_id": str(uuid.uuid4())},
        headers=ayse,
    )
    assert missing.status_code == 404
    assert (await w.post(f"/polls/{uuid.uuid4()}/close")).status_code == 404
    tech = await staff_headers(w, "teknik@test.local", "Teknik Personel")
    assert (await w.api.get(w.url("/polls"), headers=tech)).status_code == 200
    assert (await w.api.post(w.url("/polls"), json=POLL, headers=tech)).status_code == 403
    staff_only = await w.api.get(w.url("/resident/polls"), headers=tech)
    assert staff_only.status_code == 403  # kişisi olmayan üyelik sakin ucuna giremez
    yildiz = await w.api.get(w.url("/polls", "yildiz-sitesi"), headers=w.headers)
    assert yildiz.status_code == 404


# --- 16 Sözleşme --------------------------------------------------------------------

CONTRACT = {
    "vendor": "Asansör Ltd.",
    "subject": "4 asansörün aylık bakımı",
    "category": "elevator",
    "start_date": "2025-12-09",
    "end_date": "2026-07-10",
    "amount": "12500.00",
    "period": "monthly",
    "notice_days": 30,
}


async def test_sozlesme_durum_arsiv_ve_yetki(mgmt: World) -> None:
    w = mgmt
    created = await w.post("/contracts", CONTRACT)
    assert created.status_code == 201, created.text
    assert created.json()["message"] == "Asansör Ltd. sözleşmesi eklendi."
    item = created.json()["data"]
    assert (item["amount"], item["days_left"], item["state"]) == ("12500.00", 20, "expiring")
    await w.post("/contracts", {**CONTRACT, "vendor": "Bahçe A.Ş.", "end_date": "2026-12-31"})
    await w.post("/contracts", {**CONTRACT, "vendor": "Eski Firma", "end_date": "2026-06-01"})
    listed = (await w.get("/contracts")).json()
    assert [(c["vendor"], c["state"]) for c in listed] == [
        ("Eski Firma", "expired"),
        ("Asansör Ltd.", "expiring"),
        ("Bahçe A.Ş.", "active"),
    ]
    assert listed[0]["days_left"] == -19

    bad = await w.post(
        "/contracts", {**CONTRACT, "end_date": "2025-01-01", "category": "x", "notice_days": 400}
    )
    assert fields(bad) == {
        "end_date": "Bitiş, başlangıçtan önce olamaz.",
        "category": "Sözleşme türünü seçin.",
        "notice_days": "İhbar süresi 0–365 gün olmalı.",
    }
    edited = await patch(w, f"/contracts/{item['id']}", {"end_date": "2026-09-30", "note": "Ek"})
    assert edited.json()["message"] == "Asansör Ltd. sözleşmesi güncellendi."
    assert (edited.json()["data"]["state"], edited.json()["data"]["note"]) == ("active", "Ek")
    archived = await patch(w, f"/contracts/{item['id']}", {"is_archived": True})
    assert archived.json()["message"] == "Asansör Ltd. sözleşmesi arşive kaldırıldı."
    assert archived.json()["data"]["state"] == "archived"
    assert item["id"] not in [c["id"] for c in (await w.get("/contracts")).json()]
    assert [c["id"] for c in (await w.get("/contracts", params={"archived": "true"})).json()] == [
        item["id"]
    ]
    restored = await patch(w, f"/contracts/{item['id']}", {"is_archived": False})
    assert restored.json()["message"] == "Asansör Ltd. sözleşmesi arşivden çıkarıldı."
    assert fields(await patch(w, f"/contracts/{item['id']}", {"start_date": "2027-01-01"})) == {
        "end_date": "Bitiş, başlangıçtan önce olamaz."
    }
    assert "update" in await audited(w, "contracts")

    auditor = await staff_headers(w, "denetci@test.local", "Denetçi")
    assert (await w.api.get(w.url("/contracts"), headers=auditor)).status_code == 200
    assert (
        await w.api.post(w.url("/contracts"), json=CONTRACT, headers=auditor)
    ).status_code == 403
    accounting = await staff_headers(w, "muhasebe@test.local", "Muhasebe")
    assert (
        await w.api.post(w.url("/contracts"), json=CONTRACT, headers=accounting)
    ).status_code == 201
    yildiz = await w.api.patch(
        w.url(f"/contracts/{item['id']}", "yildiz-sitesi"), json={"note": "x"}, headers=w.headers
    )
    assert yildiz.status_code == 404


# --- 17 Demirbaş ve stok ------------------------------------------------------------


async def test_demirbas_kod_sirali_ve_durum(mgmt: World) -> None:
    w = mgmt
    first = await w.post(
        "/assets",
        {
            "name": "Çim biçme makinesi",
            "category": "Bahçe ekipmanı",
            "location": "B blok depo",
            "acquired_on": "2026-04-02",
            "value": "18500.00",
        },
    )
    assert first.status_code == 201, first.text
    assert first.json()["message"] == "DB-0001 Çim biçme makinesi eklendi."
    assert (first.json()["data"]["status"], first.json()["data"]["value"]) == ("in_use", "18500.00")
    second = (await w.post("/assets", {"name": "Merdiven"})).json()["data"]
    assert second["code"] == "DB-0002"
    assert fields(await w.post("/assets", {"name": "", "value": "-1"})) == {
        "name": "Demirbaşın adını yazın.",
        "value": "Bedel sıfır ya da daha büyük olmalı.",
    }
    retired = await patch(w, f"/assets/{second['id']}", {"status": "retired", "code": "X"})
    assert (retired.json()["data"]["status"], retired.json()["data"]["code"]) == (
        "retired",
        "DB-0002",
    )
    assert [a["code"] for a in (await w.get("/assets", params={"status": "retired"})).json()] == [
        "DB-0002"
    ]
    assert len((await w.get("/assets")).json()) == 2
    assert "update" in await audited(w, "assets")
    assert (await patch(w, f"/assets/{uuid.uuid4()}", {"name": "x y"})).status_code == 404

    yildiz = await w.api.get(w.url("/assets", "yildiz-sitesi"), headers=w.headers)
    assert yildiz.json() == []  # Yıldız'ın listesi ayrı; Aksu'nun demirbaşı orada yok
    other = await w.api.post(
        w.url("/assets", "yildiz-sitesi"), json={"name": "Süpürge"}, headers=w.headers
    )
    assert other.json()["data"]["code"] == "DB-0001"  # numara site içinde


async def test_stok_ondalik_eksiye_dusmez_eszamanli(mgmt: World) -> None:
    w = mgmt
    created = await w.post(
        "/stock-items", {"name": "Çamaşır suyu", "unit_label": "litre", "min_quantity": "5"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["message"] == (
        "Çamaşır suyu eklendi. Mevcudu girmek için giriş hareketi yapın."
    )
    item = created.json()["data"]
    assert (item["quantity"], item["min_quantity"], item["is_low"]) == ("0", "5", True)
    dup = await w.post("/stock-items", {"name": "ÇAMAŞIR  SUYU", "unit_label": "litre"})
    assert fields(dup) == {"name": "Bu adla bir malzeme var."}

    moves = f"/stock-items/{item['id']}/moves"
    for qty in ("0.1", "0,2"):
        assert (await w.post(moves, {"direction": "in", "quantity": qty})).status_code == 201
    over = await w.post(moves, {"direction": "out", "quantity": "0.5"})
    assert over.status_code == 409
    assert (over.json()["error"]["code"], over.json()["error"]["message"]) == (
        "insufficient_stock",
        "Stokta 0,3 litre var; daha fazlası çıkarılamaz.",
    )
    for qty in ("0", "1.2345", "abc", "-1"):
        bad = await w.post(moves, {"direction": "in", "quantity": qty})
        assert fields(bad) == {"quantity": "Sıfırdan büyük bir miktar girin (en çok 3 ondalık)."}
    assert "direction" in fields(await w.post(moves, {"direction": "x", "quantity": "1"}))

    filled = await w.post(moves, {"direction": "in", "quantity": "0.7", "note": "Satın alma"})
    data = filled.json()["data"]
    assert data["item"]["quantity"] == "1"
    assert (data["move"]["quantity"], data["move"]["moved_by"]) == ("0.7", "Kerem YILDIRIM")
    assert filled.json()["message"] == "Çamaşır suyu: giriş kaydedildi."

    # Eşzamanlı iki çıkış (0,7 + 0,7 > 1): satır kilidi — biri geçer, stok eksiye düşmez
    both = await asyncio.gather(
        *(w.post(moves, {"direction": "out", "quantity": "0.7"}) for _ in range(2))
    )
    assert sorted(r.status_code for r in both) == [201, 409]
    [stock] = (await w.get("/stock-items")).json()
    assert stock["quantity"] == "0.3"

    history = (await w.get(moves)).json()
    assert [(m["direction"], m["quantity"]) for m in history] == [
        ("out", "0.7"),
        ("in", "0.7"),
        ("in", "0.2"),
        ("in", "0.1"),
    ]
    assert len((await w.get(moves, params={"limit": 2})).json()) == 2
    assert (await w.get(f"/stock-items/{uuid.uuid4()}/moves")).status_code == 404

    # Hareket değişmez (tetikleyici)
    with site_scope(w.site_id):
        async with w.factory() as session:
            move_id = await session.scalar(select(StockMove.id).limit(1))
            with pytest.raises(DBAPIError, match="Geçmiş kaydı değiştirilemez"):
                await session.execute(
                    text("UPDATE stock_moves SET quantity = 99 WHERE id = :id"), {"id": move_id}
                )


async def test_envanter_yetki(mgmt: World) -> None:
    w = mgmt
    item = (await w.post("/stock-items", {"name": "Ampul", "unit_label": "adet"})).json()["data"]
    tech = await staff_headers(w, "teknik@test.local", "Teknik Personel")
    moved = await w.api.post(
        w.url(f"/stock-items/{item['id']}/moves"),
        json={"direction": "in", "quantity": "10"},
        headers=tech,
    )
    assert moved.status_code == 201  # teknik personel stok hareketi yapar
    board = await staff_headers(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/assets"), headers=board)).status_code == 200
    assert (
        await w.api.post(w.url("/assets"), json={"name": "Masa"}, headers=board)
    ).status_code == 403
    guard = await staff_headers(w, "guvenlik@test.local", "Güvenlik")
    assert (await w.api.get(w.url("/stock-items"), headers=guard)).status_code == 403


# --- 18 Personel --------------------------------------------------------------------

STAFF = {
    "full_name": "Mehmet Kaya",
    "position": "Kapı görevlisi",
    "employer": "contractor",
    "contractor_name": "Güvenlik A.Ş.",
    "phone": "0532 111 22 33",
    "start_date": "2026-01-05",
    "shift": "Hafta içi 08:00–17:00",
}


async def test_personel_kayit_ayrilis_ve_suzgec(mgmt: World) -> None:
    w = mgmt
    created = await w.post("/staff", STAFF)
    assert created.status_code == 201, created.text
    member = created.json()["data"]
    assert (member["phone"], member["is_active"]) == ("+905321112233", True)
    assert created.json()["message"] == "Mehmet Kaya eklendi."
    site_staff = (
        await w.post(
            "/staff",
            {**STAFF, "full_name": "Ali Demir", "employer": "site", "phone": None},
        )
    ).json()["data"]
    assert site_staff["contractor_name"] is None  # site kadrosunda firma tutulmaz

    bad = await w.post(
        "/staff",
        {
            **STAFF,
            "full_name": "A1",
            "contractor_name": " ",
            "phone": "12345",
            "end_date": "2025-01-01",
        },
    )
    assert set(fields(bad)) == {"full_name", "contractor_name", "phone", "end_date"}
    assert fields(bad)["end_date"] == "Ayrılış tarihi başlangıçtan önce olamaz."
    assert fields(await w.post("/staff", {**STAFF, "full_name": "A" * 61}))["full_name"]

    left = await patch(w, f"/staff/{member['id']}", {"end_date": "2026-06-20"})
    assert left.json()["message"] == "Mehmet Kaya için ayrılış kaydedildi."
    assert left.json()["data"]["is_active"] is True  # ayrılış bugün: hâlâ çalışan
    active = (await w.get("/staff", params={"active": "true"})).json()
    assert sorted(s["full_name"] for s in active) == ["Ali Demir", "Mehmet Kaya"]
    w.set_today(date(2026, 6, 21))
    assert [s["full_name"] for s in (await w.get("/staff", params={"active": "false"})).json()] == [
        "Mehmet Kaya"
    ]
    assert len((await w.get("/staff")).json()) == 2
    renamed = await patch(w, f"/staff/{member['id']}", {"shift": "Gece"})
    assert renamed.json()["message"] == "Mehmet Kaya güncellendi."
    assert "update" in await audited(w, "staff_members")
    assert (await patch(w, f"/staff/{uuid.uuid4()}", {"shift": "x"})).status_code == 404


async def test_personel_yetki_ve_izolasyon(mgmt: World) -> None:
    w = mgmt
    member = (await w.post("/staff", STAFF)).json()["data"]
    guard = await staff_headers(w, "guvenlik@test.local", "Güvenlik")
    assert (await w.api.get(w.url("/staff"), headers=guard)).status_code == 403  # telefon görmez
    board = await staff_headers(w, "kurul@test.local", "Yönetim Kurulu Üyesi")
    assert (await w.api.get(w.url("/staff"), headers=board)).status_code == 200
    assert (await w.api.post(w.url("/staff"), json=STAFF, headers=board)).status_code == 403
    yildiz = await w.api.patch(
        w.url(f"/staff/{member['id']}", "yildiz-sitesi"), json={"shift": "x"}, headers=w.headers
    )
    assert yildiz.status_code == 404
