"""Bölüm 360° — servis isteği 20 (YAP-03, YAP-04 S4): özet, maskeli iletişim ve görüntüleme
kaydı (D-22), talep/ziyaretçi bölüm süzgeci, hane üyeleri."""

import uuid
from datetime import date

from sqlalchemy import select

from site_yonetim.models import AuditLog, Visitor
from tests.integration.finance_world import World
from tests.integration.p02_helpers import (
    OTHER,
    call,
    enable_modules,
    new_unit,
    resident,
    scope,
    staff,
)


async def _with_contact(world: World) -> str:
    """A-1 malikine telefon/e-posta ekler; kişi kimliğini döner."""
    await call(
        world, "PATCH", f"/people/{world.person_id}",
        {"phone": "0532 111 22 33", "email": "ayse@ornek.com"},
    )  # fmt: skip
    return world.person_id


async def test_bolum_ozeti(world: World) -> None:
    overview = (await world.get(f"/units/{world.unit_id}/overview")).json()

    assert overview == {
        "balance": "0.00",
        "share_total": "100.00",
        "warnings": ["no_land_share"],
        "packages_waiting": 0,
        "vehicles": 0,
        "household": 0,
    }


async def test_bolum_ozeti_malik_yok_ve_finans_izni_yoksa_bakiye_gizli(world: World) -> None:
    empty = await new_unit(world, "2", owner=None)
    security = await staff(world, "teknik@test.local", "Teknik Personel")
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")

    overview = (await world.get(f"/units/{empty}/overview")).json()
    technician = await call(world, "GET", f"/units/{empty}/overview", headers=security)
    board_view = (await call(world, "GET", f"/units/{empty}/overview", headers=board)).json()

    assert overview["warnings"] == ["no_owner", "no_land_share"]
    assert technician.status_code == 403  # units.read yok
    assert board_view["balance"] == "0.00"  # YK finans okur


async def test_iletisim_maskeli_doner_gosterince_denetim_kaydi_yazilir(world: World) -> None:
    person_id = await _with_contact(world)

    people = (await world.get("/people")).json()["items"]
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    shown = await world.post(f"/people/{person_id}/contact-views", {})

    assert people[0]["phone"] == "+90 532 *** ** 33"
    assert people[0]["email"] == "a***@ornek.com"
    assert unit["parties"][0]["person"]["phone"] == "+90 532 *** ** 33"
    assert shown.json()["data"] == {"phone": "+905321112233", "email": "ayse@ornek.com"}
    with scope(world):
        async with world.factory() as session:
            rows = list(
                await session.scalars(select(AuditLog).where(AuditLog.action == "contact_view"))
            )
    assert len(rows) == 1
    assert rows[0].entity == "persons"
    assert rows[0].entity_id == uuid.UUID(person_id)
    assert rows[0].actor_name == "Kerem YILDIRIM"
    assert rows[0].after == {"fields": ["phone", "email"]}  # değer kayda girmez


async def test_yonetim_kurulu_iletisim_alanini_hic_almaz(world: World) -> None:
    """D-22 (YAP-03 S1): YK üyesi kişileri görür ama iletişim alanı API'den hiç dönmez."""
    person_id = await _with_contact(world)
    board = await staff(world, "yk@test.local", "Yönetim Kurulu Üyesi")

    people = (await call(world, "GET", "/people", headers=board)).json()["items"]
    unit = (await call(world, "GET", f"/units/{world.unit_id}", headers=board)).json()
    view = await call(world, "POST", f"/people/{person_id}/contact-views", {}, headers=board)

    assert (people[0]["phone"], people[0]["email"]) == (None, None)
    assert unit["parties"][0]["person"]["phone"] is None
    assert view.status_code == 403


async def test_iletisim_goruntuleme_baska_site_404(world: World) -> None:
    person_id = await _with_contact(world)

    response = await call(world, "POST", f"/people/{person_id}/contact-views", {}, slug=OTHER)

    assert response.status_code == 404


async def test_talepler_bolum_suzgeci(world: World) -> None:
    await enable_modules(world, "requests")
    other = await new_unit(world, "2")
    for unit_id, title in ((world.unit_id, "Musluk"), (other, "Kapı"), (world.unit_id, "Lamba")):
        created = await world.post("/requests", {"title": title, "unit_id": unit_id})
        assert created.status_code == 201, created.text

    page = (await world.get("/requests", params={"unit_id": world.unit_id, "page_size": 1})).json()

    assert page["total"] == 2
    assert [r["title"] for r in page["items"]] == ["Lamba"]


async def test_ziyaretciler_bolum_suzgeci_son_30_gun(world: World) -> None:
    await enable_modules(world, "visitors")
    other = await new_unit(world, "2")
    with scope(world):
        async with world.factory() as session, session.begin():
            for unit_id, day in (
                (world.unit_id, date(2026, 6, 20)),
                (world.unit_id, date(2026, 6, 1)),
                (world.unit_id, date(2026, 4, 1)),  # 30 günden eski
                (other, date(2026, 6, 20)),
            ):
                session.add(
                    Visitor(
                        unit_id=uuid.UUID(unit_id), full_name="Test DENEME", kind="guest",
                        visit_date=day, status="expected", recorded_by="Güvenlik",
                    )
                )  # fmt: skip

    page = (await world.get("/visitors", params={"unit_id": world.unit_id})).json()
    today_only = (await world.get("/visitors", params={"unit_id": other})).json()

    assert page["total"] == 2
    assert today_only["total"] == 1


# --- Hane üyeleri ------------------------------------------------------------------------

PHONES = [f"+90532000000{n}" for n in range(10)]


async def _member(world: World, phone: str, *, headers: dict[str, str] | None = None) -> object:
    return await call(
        world, "POST", f"/units/{world.unit_id}/household",
        {"full_name": "Test Deneme", "phone": phone, "permissions": {"requests": True}},
        headers=headers,
    )  # fmt: skip


async def test_hane_uyesi_siniri_ve_ayni_telefon(world: World) -> None:
    created = [await _member(world, PHONES[i]) for i in range(5)]
    same = await _member(world, PHONES[0])
    created.append(await _member(world, PHONES[5]))
    seventh = await _member(world, PHONES[6])

    first = created[0].json()["data"]  # type: ignore[attr-defined]
    assert first["status"] == "invited"
    assert first["invite_link"].startswith("/davet/")
    assert first["permissions"] == {
        "visitors": True, "packages": True, "reservations": False, "requests": True,
    }  # fmt: skip
    assert seventh.status_code == 409  # type: ignore[attr-defined]
    assert seventh.json()["error"]["code"] == "household_limit"  # type: ignore[attr-defined]

    ended = await world.post(f"/household/{first['id']}/end")
    again = await world.post(f"/household/{first['id']}/end")
    after = await _member(world, PHONES[6])

    assert ended.json()["data"]["status"] == "ended"
    assert again.json()["error"]["code"] == "already_ended"
    assert after.status_code == 201  # type: ignore[attr-defined]
    listed = (await world.get(f"/units/{world.unit_id}/household")).json()
    assert len(listed) == 7
    assert all(m["invite_link"] is None for m in listed)  # belirteç yalnız yanıtta bir kez
    assert same.status_code == 409  # type: ignore[attr-defined]
    assert same.json()["error"]["code"] == "already_member"  # type: ignore[attr-defined]


async def test_hane_uyesi_daveti_kabul_edilince_etkin(world: World) -> None:
    member = (await _member(world, PHONES[1])).json()["data"]  # type: ignore[attr-defined]
    token = member["invite_link"].removeprefix("/davet/")

    info = await world.api.get(f"/api/v1/public/invitations/{token}")
    accepted = await world.api.post(
        f"/api/v1/public/invitations/{token}/accept",
        json={"phone": "0532 000 00 01", "kvkk_ack": True, "email": "hane@test.local"},
    )

    assert info.json()["relation"] == "household"
    assert info.json()["email_required"] is True
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["data"]["temporary_password"]
    listed = (await world.get(f"/units/{world.unit_id}/household")).json()
    assert listed[0]["status"] == "active"
    unit = (await world.get(f"/units/{world.unit_id}")).json()
    assert {p["role"] for p in unit["parties"]} == {"owner", "resident"}
    assert len(unit["accounts"]) == 2  # hane üyesine cari hesap açılmaz


async def test_sakin_yalniz_kendi_bolumune_hane_uyesi_ekler(world: World) -> None:
    other = await new_unit(world, "2")
    headers = await resident(world, "ayse@test.local", world.person_id)

    own = await call(
        world, "POST", "/resident/household",
        {"full_name": "Test Deneme", "phone": PHONES[2]}, headers=headers,
    )  # fmt: skip
    foreign = await call(
        world, "POST", "/resident/household",
        {"full_name": "Test Deneme", "phone": PHONES[3], "unit_id": other}, headers=headers,
    )  # fmt: skip
    mine = await call(world, "GET", "/resident/household", headers=headers)
    staff_only = await call(world, "GET", f"/units/{world.unit_id}/household", headers=headers)

    assert own.status_code == 201, own.text
    assert own.json()["data"]["unit_name"] == "A-1"
    assert foreign.status_code == 404
    assert [m["phone"] for m in mine.json()] == [PHONES[2]]
    assert staff_only.status_code == 403
