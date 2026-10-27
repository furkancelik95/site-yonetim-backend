"""Bildirim merkezi — Yazılım Dokümanı PLT-05 (gerçek PostgreSQL).

Bitti ölçütü: bildirim aynı olayda bir kez yazılır (tekrar güvenli); kişi yalnız kendi
bildirimini görür ve okur; başka sitenin bildirimi görünmez; tercihlerde kapatılamayan kanal
kapatılamaz; push jetonu cihazda hesap değişince yeni kişiye taşınır.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import PushToken, User
from site_yonetim.services import notifications as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

MESSAGE = svc.Message(
    category=NotificationCategory.OPERATIONS,
    kind="approval.requested",
    title="Onayınız bekleniyor",
    body="Satın alma — Asansör halatı — 42.500,00 TL",
    link_entity="approval_requests",
)


async def me(w: World) -> uuid.UUID:
    return uuid.UUID((await w.api.get("/api/v1/me", headers=w.headers)).json()["user_id"])


async def send(w: World, users: list[uuid.UUID], key: str, site_id: uuid.UUID | None = None) -> int:
    with site_scope(site_id or w.site_id):
        async with w.factory() as session, session.begin():
            return await svc.notify(session, users, MESSAGE, dedupe_key=key)


async def test_tekrar_guvenli_ve_kendi_bildirimim(world: World) -> None:
    w = world
    kerem = await me(w)
    other = await create_user(w.factory, "selin@test.local", full_name="Selin ARI")
    await add_site_membership(w.factory, w.site_id, other.id, "Muhasebe")

    assert await send(w, [kerem, other.id], "olay-1") == 2
    assert await send(w, [kerem, other.id], "olay-1") == 0  # aynı olay: ikinci bildirim yok
    assert await send(w, [kerem], "olay-2") == 1

    listed = await w.get("/notifications")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert (body["total"], body["unread_count"]) == (2, 2)
    first = body["items"][0]
    assert (first["title"], first["kind"], first["category"], first["read_at"]) == (
        "Onayınız bekleniyor",
        "approval.requested",
        "operations",
        None,
    )
    selin = await login_headers(w.api, "selin@test.local")
    theirs = (await w.api.get(w.url("/notifications"), headers=selin)).json()
    assert theirs["total"] == 1  # yalnız kendi bildirimi

    # başkasının bildirim kimliğiyle okundu işaretlemek etkisiz
    marked = await w.api.post(
        w.url("/notifications/read"), json={"ids": [first["id"]]}, headers=selin
    )
    assert marked.json()["data"] == {"marked": 0, "unread_count": 1}
    mine = await w.post("/notifications/read", {"ids": [first["id"]]})
    assert mine.json() == {
        "data": {"marked": 1, "unread_count": 1},
        "message": "1 bildirim okundu olarak işaretlendi.",
    }
    assert (await w.get("/notifications", params={"unread": "true"})).json()["total"] == 1
    everything = await w.post("/notifications/read", {})
    assert everything.json()["data"] == {"marked": 1, "unread_count": 0}
    assert everything.json()["message"] == "Tüm bildirimler okundu olarak işaretlendi."
    assert (await w.get("/notifications/unread-count")).json() == 0


async def test_site_izolasyonu_ve_ozet(world: World) -> None:
    w = world
    kerem = await me(w)
    await send(w, [kerem], "aksu-1")
    await send(w, [kerem], "yildiz-1", site_id=w.other_site_id)
    await send(w, [kerem], "yildiz-2", site_id=w.other_site_id)

    aksu = (await w.get("/notifications")).json()
    assert aksu["total"] == 1
    yildiz = await w.api.get(w.url("/notifications", "yildiz-sitesi"), headers=w.headers)
    assert yildiz.json()["total"] == 2
    summary = (await w.api.get("/api/v1/me/notifications/summary", headers=w.headers)).json()
    assert {s["slug"]: s["unread_count"] for s in summary} == {
        "aksu-konaklari": 1,
        "yildiz-sitesi": 2,
    }
    # erişimi olmayan sitenin bildirim listesi yok (404)
    stranger = await create_user(w.factory, "yabanci@test.local")
    await add_site_membership(w.factory, w.other_site_id, stranger.id, "Muhasebe")
    headers = await login_headers(w.api, "yabanci@test.local")
    assert (await w.api.get(w.url("/notifications"), headers=headers)).status_code == 404


async def test_kategori_suzgeci_ve_90_gun(world: World) -> None:
    w = world
    kerem = await me(w)
    await send(w, [kerem], "op-1")
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            await svc.notify(
                session,
                [kerem],
                svc.Message(NotificationCategory.FINANCIAL, "payment.received", "Ödeme alındı"),
                dedupe_key="fin-1",
            )
    financial = (await w.get("/notifications", params={"category": "financial"})).json()
    assert [n["kind"] for n in financial["items"]] == ["payment.received"]
    old = datetime.now(UTC) + timedelta(days=91)
    with site_scope(w.site_id):
        async with w.factory() as session:
            assert await svc.unread_count(session, kerem, now=old) == 0


async def test_tercihler_kilitli_kanal_ve_en_az_bir(world: World) -> None:
    w = world
    url = "/api/v1/me/notification-preferences"
    current = (await w.api.get(url, headers=w.headers)).json()
    assert (current["in_app"], current["delivery_enabled"], current["test_provider"]) == (
        True,
        True,
        True,
    )
    urgent = next(c for c in current["categories"] if c["category"] == "urgent")
    assert {c["channel"]: (c["enabled"], c["locked"]) for c in urgent["channels"]} == {
        "push": (True, True),
        "sms": (True, True),
        "email": (False, False),
    }

    locked = await w.api.put(
        url,
        json={"changes": [{"category": "urgent", "channel": "sms", "enabled": False}]},
        headers=w.headers,
    )
    assert locked.status_code == 422
    assert locked.json()["error"]["message"] == "Acil bildirimleri bu kanalda kapatılamaz."
    empty = await w.api.put(
        url,
        json={
            "changes": [
                {"category": "financial", "channel": "push", "enabled": False},
                {"category": "financial", "channel": "email", "enabled": False},
            ]
        },
        headers=w.headers,
    )
    assert empty.json()["error"]["code"] == "preference_not_allowed"

    saved = await w.api.put(
        url,
        json={
            "changes": [
                {"category": "operations", "channel": "push", "enabled": False},
                {"category": "financial", "channel": "sms", "enabled": True},
            ]
        },
        headers=w.headers,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["message"] == "Bildirim tercihleriniz kaydedildi."
    again = (await w.api.get(url, headers=w.headers)).json()
    rows = {
        c["category"]: {x["channel"]: x["enabled"] for x in c["channels"]}
        for c in again["categories"]
    }
    assert rows["operations"]["push"] is False
    assert rows["financial"] == {"push": True, "sms": True, "email": True}
    # varsayılana dönmek kaydı siler (yalnız farklar saklanır)
    back = await w.api.put(
        url,
        json={"changes": [{"category": "operations", "channel": "push", "enabled": True}]},
        headers=w.headers,
    )
    assert back.status_code == 200


async def test_push_jetonu_kaydi_ve_tasinmasi(world: World) -> None:
    w = world
    token = "cihaz-jetonu-" + "x" * 40
    url = "/api/v1/me/push-tokens"
    first = await w.api.post(url, json={"token": token, "platform": "ios"}, headers=w.headers)
    assert first.status_code == 204
    again = await w.api.post(url, json={"token": token, "platform": "ios"}, headers=w.headers)
    assert again.status_code == 204  # tekrar güvenli

    await create_user(w.factory, "selin@test.local", full_name="Selin ARI")
    selin = await login_headers(w.api, "selin@test.local")
    moved = await w.api.post(url, json={"token": token, "platform": "android"}, headers=selin)
    assert moved.status_code == 204
    async with w.factory() as session:
        rows = list(await session.scalars(select(PushToken)))
        owner = await session.get(User, rows[0].user_id)
    assert len(rows) == 1
    assert (owner.email if owner else None, rows[0].platform) == ("selin@test.local", "android")

    gone = await w.api.post(f"{url}/remove", json={"token": token}, headers=w.headers)
    assert gone.status_code == 404  # artık Selin'in
    removed = await w.api.post(f"{url}/remove", json={"token": token}, headers=selin)
    assert removed.status_code == 204
    bad = await w.api.post(url, json={"token": token, "platform": "symbian"}, headers=w.headers)
    assert bad.status_code == 422
