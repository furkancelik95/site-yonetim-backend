"""Bildirim gönderim katmanı ve SMS hesabı — Yazılım Dokümanı PLT-05, ILT-05, §4.8 (K5: test
sağlayıcısı). Gerçek PostgreSQL.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from site_yonetim.core.config import Settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import NotificationDelivery, PushToken, Site, User
from site_yonetim.services import delivery
from site_yonetim.services import notifications as notify_svc
from site_yonetim.services.channels import TEST_INBOX, channels_from_settings
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

PHONE = "+905321234567"
API_KEY = "gizli-anahtar-1234"  # gitleaks:allow — sahte sağlayıcı anahtarı
# 23:10 İstanbul = 20:10 UTC (sessiz saat 22:00–08:00)
NIGHT = datetime(2026, 6, 20, 20, 10, tzinfo=UTC)
MORNING = datetime(2026, 6, 21, 5, 0, tzinfo=UTC)  # 08:00 İstanbul
NOON = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)


async def recipient(w: World, *, push: bool = False, email: str | None = "elif@test.local") -> User:
    async with w.factory() as session, session.begin():
        user = User(phone=PHONE, email=email, password_hash=None, full_name="Elif SAKİN",
                    kind="resident")  # fmt: skip
        session.add(user)
        await session.flush()
        if push:
            session.add(
                PushToken(user_id=user.id, token="tok-1", platform="ios", last_seen_at=NOON)
            )
    await add_site_membership(w.factory, w.site_id, user.id, "Sakin")
    return user


async def notify(
    w: World,
    user_id: uuid.UUID,
    category: NotificationCategory,
    now: datetime,
    key: str = "olay-1",
    site_id: uuid.UUID | None = None,
) -> int:
    with site_scope(site_id or w.site_id):
        async with w.factory() as session, session.begin():
            return await notify_svc.notify(
                session,
                [user_id],
                notify_svc.Message(
                    category=category, kind="test.event", title="Başlık", body="Metin"
                ),
                dedupe_key=key,
                now=now,
            )


async def deliveries(w: World, site_id: uuid.UUID | None = None) -> list[NotificationDelivery]:
    with site_scope(site_id or w.site_id):
        async with w.factory() as session:
            rows = await session.scalars(
                select(NotificationDelivery).order_by(
                    NotificationDelivery.created_at, NotificationDelivery.channel
                )
            )
            return list(rows)


def settings_of(w: World) -> Settings:
    settings: Settings = w.app.state.settings
    return settings


async def dispatch(w: World, now: datetime) -> list[tuple[uuid.UUID, delivery.DispatchResult]]:
    s = settings_of(w)
    return await delivery.dispatch_due(
        w.factory, channels_from_settings(s), now=now, default_quota=s.sms_default_monthly_quota
    )


async def test_tercih_ve_sessiz_saat_kuyrugu(world: World) -> None:
    user = await recipient(world)
    # operasyon: varsayılan yalnız push — jeton yok → dış kanal yok
    assert await notify(world, user.id, NotificationCategory.OPERATIONS, NOON, "op-1") == 1
    assert await deliveries(world) == []
    # finansal: push + e-posta varsayılan → e-posta (push jetonu yok); gece → sabaha ertelenir
    await notify(world, user.id, NotificationCategory.FINANCIAL, NIGHT, "fin-1")
    [email] = await deliveries(world)
    assert (email.channel, email.status, email.deferred) == ("email", "queued", True)
    assert email.scheduled_for == MORNING
    assert await dispatch(world, NIGHT + timedelta(minutes=5)) == []
    [(_, result)] = await dispatch(world, MORNING)
    assert result.sent == 1
    [sent] = await deliveries(world)
    assert (sent.status, sent.provider) == ("sent", "test")
    # aynı olay iki kez: ikinci bildirim ve teslim yok (S3)
    assert await notify(world, user.id, NotificationCategory.FINANCIAL, NIGHT, "fin-1") == 0
    assert len(await deliveries(world)) == 1


async def test_tercih_kapatilan_kanal_gonderilmez(world: World) -> None:
    user = await recipient(world)
    headers = await _resident_headers(world, user)
    changed = await world.api.put(
        "/api/v1/me/notification-preferences",
        json={"changes": [{"category": "financial", "channel": "sms", "enabled": True},
                          {"category": "financial", "channel": "email", "enabled": False}]},
        headers=headers,
    )  # fmt: skip
    assert changed.status_code == 200, changed.text
    await notify(world, user.id, NotificationCategory.FINANCIAL, NOON, "fin-2")
    assert [d.channel for d in await deliveries(world)] == ["sms"]


async def _resident_headers(w: World, user: User) -> dict[str, str]:
    sent = await w.api.post("/api/v1/auth/otp/request", json={"phone": PHONE})
    code = sent.json()["data"]["test_code"]
    token = await w.api.post("/api/v1/auth/otp/verify", json={"phone": PHONE, "code": code})
    assert token.status_code == 200, token.text
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


async def test_acil_sessiz_saati_asar_push_yoksa_sms_hemen(world: World) -> None:
    user = await recipient(world, email=None)
    await notify(world, user.id, NotificationCategory.URGENT, NIGHT, "acil-1")
    [sms] = await deliveries(world)
    assert (sms.channel, sms.is_critical, sms.deferred, sms.scheduled_for) == (
        "sms", True, False, NIGHT
    )  # fmt: skip
    TEST_INBOX.clear()
    await dispatch(world, NIGHT)
    [sent] = await deliveries(world)
    assert sent.status == "sent"
    assert sent.cost_units == 1
    assert sent.body.startswith("Aksu Konakları: ")  # SMS başında site adı
    assert [i.to for i in TEST_INBOX.items()] == [PHONE]


async def test_kritik_push_2_dakikada_teslim_yoksa_sms_yedegi(world: World) -> None:
    user = await recipient(world, push=True, email=None)
    await notify(world, user.id, NotificationCategory.URGENT, NOON, "acil-2")
    [push] = await deliveries(world)
    assert push.channel == "push"  # push gidebiliyor: SMS yedek
    await dispatch(world, NOON)
    assert (
        await delivery.run_critical_fallbacks(world.factory, now=NOON + timedelta(minutes=1)) == 0
    )
    assert (
        await delivery.run_critical_fallbacks(world.factory, now=NOON + timedelta(minutes=2)) == 1
    )
    assert (
        await delivery.run_critical_fallbacks(world.factory, now=NOON + timedelta(minutes=3)) == 0
    )
    await dispatch(world, NOON + timedelta(minutes=2))
    rows = await deliveries(world)
    sms = next(d for d in rows if d.channel == "sms")
    assert (sms.status, sms.fallback_for_id) == ("sent", push.id)


async def test_kota_asimi_gonderimi_durdurmaz_ve_uyarir(world: World) -> None:
    user = await recipient(world, email=None)
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            from site_yonetim.services.messaging import get_or_create

            row = await get_or_create(session)
            row.sms_monthly_quota = 2
    for n in range(3):
        await notify(world, user.id, NotificationCategory.URGENT, NOON, f"acil-q{n}")
    await dispatch(world, NOON)
    rows = [d for d in await deliveries(world) if d.channel == "sms" and d.kind == "test.event"]
    assert [d.status for d in rows] == ["sent", "sent", "sent"]
    assert [d.is_overage for d in rows] == [False, False, True]
    titles = (await world.get("/notifications")).json()
    warnings = [n["title"] for n in titles["items"] if n["kind"] == "sms.quota"]
    # 2. SMS'te %80 ve %100 birlikte aşıldı; her eşik ayda bir kez uyarır
    assert sorted(warnings) == sorted(
        {
            "SMS kotası doldu; gönderim sürüyor, aşım faturaya yansır",
            "SMS kotasının %80'i kullanıldı",
        }
    )


async def test_sms_hesabi_ve_kota_ekrani(world: World) -> None:
    url = world.url("/settings/sms")
    account = (await world.api.get(url, headers=world.headers)).json()
    assert (account["mode"], account["monthly_quota"], account["test_provider"]) == (
        "platform", 1000, True
    )  # fmt: skip
    bad = await world.api.put(url, json={"mode": "own", "sender": "x"}, headers=world.headers)
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"sender", "provider", "api_key"}
    own = await world.api.put(
        url,
        json={
            "mode": "own",
            "sender": "AKSU SITE",
            "provider": "netgsm",
            "api_key": API_KEY,
        },
        headers=world.headers,
    )
    assert own.status_code == 200, own.text
    assert own.json()["data"]["api_key_last4"] == "1234"
    assert "gizli-anahtar" not in own.text
    with site_scope(world.site_id):
        async with world.factory() as session:
            sealed = await session.scalar(
                text("SELECT sms_api_key_sealed FROM site_messaging_settings")
            )
    assert sealed
    assert "gizli-anahtar" not in sealed
    audit = (await world.get("/audit", params={"entity": "site_messaging_settings"})).json()
    assert "gizli" not in str(audit)

    test_sms = await world.api.post(
        url + "/test", json={"phone": "0532 123 45 67"}, headers=world.headers
    )
    assert test_sms.status_code == 200, test_sms.text
    assert test_sms.json()["data"]["status"] == "sent"
    usage = (await world.api.get(url + "/usage", headers=world.headers)).json()
    assert (usage["used"], usage["quota"], usage["messages"]) == (1, 1000, 1)
    assert usage["by_category"][0]["category"] == "operations"
    listed = (await world.get("/notification-deliveries", params={"channel": "sms"})).json()
    assert listed["total"] == 1
    assert listed["items"][0]["recipient"] == "+90******4567"


async def test_sessiz_saat_ayari(world: World) -> None:
    url = world.url("/communication-settings")
    assert (await world.api.get(url, headers=world.headers)).json() == {
        "quiet_from": "22:00",
        "quiet_to": "08:00",
    }
    same = await world.api.put(
        url, json={"quiet_from": "23:00", "quiet_to": "23:00"}, headers=world.headers
    )
    assert same.status_code == 422
    assert same.json()["error"]["fields"] == {"quiet_to": "Başlangıç ve bitiş aynı olamaz."}
    ok = await world.api.put(
        url, json={"quiet_from": "21:30", "quiet_to": "07:00"}, headers=world.headers
    )
    assert ok.status_code == 200
    user = await recipient(world)
    late = datetime(2026, 6, 20, 18, 45, tzinfo=UTC)  # 21:45 İstanbul
    await notify(world, user.id, NotificationCategory.FINANCIAL, late, "fin-3")
    [d] = await deliveries(world)
    assert d.scheduled_for == datetime(2026, 6, 21, 4, 0, tzinfo=UTC)  # 07:00 İstanbul


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    tech = await create_user(world.factory, "teknik@test.local", full_name="Teknik KİŞİ")
    await add_site_membership(world.factory, world.site_id, tech.id, "Teknik Personel")
    headers = await login_headers(world.api, "teknik@test.local")
    for path in ("/settings/sms", "/settings/sms/usage", "/notification-deliveries"):
        assert (await world.api.get(world.url(path), headers=headers)).status_code == 403
    assert (
        await world.api.get(world.url("/communication-settings"), headers=headers)
    ).status_code == 200
    put = await world.api.put(
        world.url("/communication-settings"), json={"quiet_from": "22:00", "quiet_to": "07:00"},
        headers=headers,
    )  # fmt: skip
    assert put.status_code == 403
    # B sitesinin teslim kaydı A'da görünmez
    user = await recipient(world, email=None)
    await notify(
        world, user.id, NotificationCategory.URGENT, NOON, "b-1", site_id=world.other_site_id
    )
    assert len(await deliveries(world, world.other_site_id)) == 1
    assert (await world.get("/notification-deliveries")).json()["total"] == 0
    outsider = await create_user(world.factory, "disari@test.local", full_name="Dış KİŞİ")
    await add_site_membership(world.factory, world.other_site_id, outsider.id, "Yönetici")
    other = await login_headers(world.api, "disari@test.local")
    assert (await world.api.get(world.url("/settings/sms"), headers=other)).status_code == 404


async def test_istek_sonrasi_hemen_gonderim(world: World) -> None:
    """Kayıt başvurusu onayı SMS'i istek biter bitmez gönderilir (api/deps.get_session)."""
    link = (await world.get("/registration-link")).json()
    code = link.get("code") or link["data"]["code"]
    form: dict[str, Any] = {
        "first_name": "Test", "last_name": "Deneme", "phone": "0532 999 88 77", "email": "",
        "unit_text": "A 1", "relation": "tenant", "explicit_consent": False, "kvkk_ack": True,
    }  # fmt: skip
    assert (await world.api.post(f"/api/v1/public/registration/{code}", json=form)).status_code in (
        200,
        201,
    )
    reg = (await world.get("/registrations", params={"status": "pending"})).json()["items"][0]
    approved = await world.post(
        f"/registrations/{reg['id']}/approve",
        {"unit_id": world.unit_id, "start_date": "2026-06-20"},
    )
    assert approved.status_code == 200, approved.text
    [sms] = await deliveries(world)
    assert (sms.kind, sms.status, sms.recipient) == (
        "registration.approved",
        "sent",
        "+905329998877",
    )


async def test_test_gelen_kutusu_yalniz_platform_yoneticisi(world: World) -> None:
    assert (
        await world.api.get("/api/v1/platform/test-inbox", headers=world.headers)
    ).status_code == 404
    await create_user(
        world.factory, "platform@test.local", full_name="Platform", is_platform_admin=True
    )
    admin = await login_headers(world.api, "platform@test.local")
    TEST_INBOX.clear()
    await world.api.post("/api/v1/auth/otp/request", json={"phone": "05321234567"})
    inbox = await world.api.get("/api/v1/platform/test-inbox", headers=admin)
    assert inbox.status_code == 200
    [item] = inbox.json()["items"]
    assert item["to"] == PHONE
    assert "Giriş kodunuz" in item["body"]


async def test_uretimde_test_kodu_disari_verilmez(world: World) -> None:
    settings = settings_of(world)
    world.app.state.settings = settings.model_copy(update={"test_inbox_enabled": False})
    try:
        sent = await world.api.post("/api/v1/auth/otp/request", json={"phone": "05321234567"})
        assert sent.json()["data"]["test_code"] is None
    finally:
        world.app.state.settings = settings


async def test_site_kaydi_yoksa_varsayilan(world: World) -> None:
    with site_scope(world.site_id):
        async with world.factory() as session:
            site = await session.get(Site, world.site_id)
            assert site is not None
            assert await delivery.site_settings(session) is None
