"""Platform destek — Yazılım Dokümanı PYN-04, PYN-06, PLT-12; servis istekleri 82–84; K12
(gerçek PostgreSQL).

Kurgu `finance_world`: Aksu ve Yıldız; yönetici Kerem (iki sitede); A-1 maliki Ayşe (kişi).
Platform yöneticisi Pelin (site üyeliği yok).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from site_yonetim.api.deps import get_now
from site_yonetim.db.tenancy import all_sites_scope, site_scope
from site_yonetim.domain.notifications import NotificationCategory
from site_yonetim.models import (
    AuditLog,
    Notification,
    Plan,
    Site,
    SupportTicketMessage,
    User,
)
from site_yonetim.services import support as svc
from site_yonetim.services.notifications import Message, notify
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

T0 = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
AKSU = "aksu-konaklari"
YILDIZ = "yildiz-sitesi"


class Clock:
    """İstek saati. Giriş gerçek saatle yapılır (jeton süresi gerçek saate göre doğrulanır)."""

    def __init__(self) -> None:
        self.now = T0
        self.real = False

    def __call__(self) -> datetime:
        return datetime.now(UTC) if self.real else self.now


async def login(w: World, email: str) -> dict[str, str]:
    clock = w.app.dependency_overrides.get(get_now)
    if isinstance(clock, Clock):
        clock.real = True
    try:
        return await login_headers(w.api, email)
    finally:
        if isinstance(clock, Clock):
            clock.real = False


@pytest.fixture
async def destek(world: World) -> tuple[World, dict[str, str], Clock]:
    clock = Clock()
    world.app.dependency_overrides[get_now] = clock
    await create_user(
        world.factory, "pelin@platform.local", full_name="Pelin DESTEK", is_platform_admin=True
    )
    return world, await login(world, "pelin@platform.local"), clock


def ticket_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Tahsilat ekranı açılmıyor",
        "body": "Tahsilat kaydet ekranında hata alıyoruz.",
        "category": "payment",
        "priority": "high",
        "context": {"path": "/s/aksu-konaklari/tahsilat", "app_version": "web 1.4"},
    }
    body.update(overrides)
    return body


async def staff_with(
    w: World, email: str, role: str, slug_site: uuid.UUID | None = None
) -> dict[str, str]:
    user = await create_user(w.factory, email, full_name="Personel KULLANICI")
    await add_site_membership(w.factory, slug_site or w.site_id, user.id, role)
    return await login(w, email)


# --- 82: destek biletleri --------------------------------------------------------------


async def test_bilet_acilir_baglam_ve_sla_yazilir(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    w, _, _ = destek
    response = await w.post("/support-tickets", ticket_body())
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["number"].startswith("DST-")
    assert data["status"] == "open"
    assert data["site_slug"] == AKSU
    assert data["site_name"] == "Aksu Konakları"
    assert data["context"]["path"] == "/s/aksu-konaklari/tahsilat"
    # İz kimliği: bu isteğin X-Request-ID'si (backend günlükleriyle eşleşir)
    assert data["context"]["correlation_id"] == response.headers["X-Request-ID"]
    assert data["sla"]["first_due"] == (T0 + timedelta(hours=4)).isoformat().replace("+00:00", "Z")
    assert data["sla"]["resolve_due"].startswith("2026-06-21T09:00")
    assert data["support_tier"] == "business_hours"
    assert data["on_call"] is False
    assert data["messages"][0]["side"] == "customer"
    assert data["created_by"] == "Kerem YILDIRIM"

    listed = (await w.get("/support-tickets")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == data["id"]
    # Site izolasyonu: Yıldız'ın listesinde yok, Yıldız adresinden yazılamaz
    other = await w.api.get(w.url("/support-tickets", YILDIZ), headers=w.headers)
    assert other.json()["total"] == 0
    cross = await w.api.post(
        w.url(f"/support-tickets/{data['id']}/messages", YILDIZ),
        json={"body": "merhaba"},
        headers=w.headers,
    )
    assert cross.status_code == 404


async def test_bilet_dogrulama_ve_yetki(destek: tuple[World, dict[str, str], Clock]) -> None:
    w, _, _ = destek
    bad = await w.post(
        "/support-tickets", ticket_body(title="kıs", category="hepsi", priority="acil", body="")
    )
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) >= {"title", "body", "category", "priority"}
    # Güvenlik rolü yönetim rolü değil → 403; sakin de açamaz (19.3)
    guard = await staff_with(w, "guvenlik@test.local", "Güvenlik")
    denied = await w.api.post(w.url("/support-tickets"), json=ticket_body(), headers=guard)
    assert denied.status_code == 403
    assert (await w.api.get(w.url("/support-tickets"), headers=guard)).status_code == 403
    # Muhasebe (finance.read) açabilir
    accounting = await staff_with(w, "muhasebe@test.local", "Muhasebe")
    ok = await w.api.post(w.url("/support-tickets"), json=ticket_body(), headers=accounting)
    assert ok.status_code == 201


async def test_bilet_yazisma_durum_ve_kapanis(destek: tuple[World, dict[str, str], Clock]) -> None:
    w, platform, clock = destek
    tid = (await w.post("/support-tickets", ticket_body())).json()["data"]["id"]

    # Platform dışı kullanıcı platform ucunu görmez (404)
    assert (
        await w.api.get("/api/v1/platform/support-tickets", headers=w.headers)
    ).status_code == 404

    early = await w.post(f"/support-tickets/{tid}/close")
    assert (early.status_code, early.json()["error"]["code"]) == (409, "invalid_status")

    clock.now = T0 + timedelta(hours=1)
    reply = await w.api.post(
        f"/api/v1/platform/support-tickets/{tid}/messages",
        json={
            "body": "İnceliyoruz, ekran görüntüsü gönderir misiniz?",
            "status": "waiting_customer",
        },
        headers=platform,
    )
    assert reply.status_code == 200, reply.text
    data = reply.json()["data"]
    assert data["status"] == "waiting_customer"
    assert data["first_response_at"].startswith("2026-06-20T10:00")
    assert data["messages"][-1]["side"] == "support"
    assert data["messages"][-1]["by"] == "Pelin DESTEK (destek)"
    assert data["sla"]["first_breached"] is False

    # Bileti açana bildirim
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = list(await session.scalars(select(Notification.kind)))
    assert "support.ticket_answered" in kinds

    answer = await w.post(f"/support-tickets/{tid}/messages", {"body": "Görüntü ekte."})
    assert answer.json()["data"]["status"] == "open"

    await w.api.post(
        f"/api/v1/platform/support-tickets/{tid}/messages",
        json={"body": "Düzeltildi.", "status": "resolved"},
        headers=platform,
    )
    closed = await w.post(f"/support-tickets/{tid}/close")
    assert closed.status_code == 200
    assert closed.json()["data"]["status"] == "closed"
    late = await w.post(f"/support-tickets/{tid}/messages", {"body": "Bir şey daha"})
    assert (late.status_code, late.json()["error"]["code"]) == (409, "closed")
    late_support = await w.api.post(
        f"/api/v1/platform/support-tickets/{tid}/messages",
        json={"body": "Ek bilgi"},
        headers=platform,
    )
    assert late_support.status_code == 409

    listed = (
        await w.api.get(
            "/api/v1/platform/support-tickets", params={"status": "closed"}, headers=platform
        )
    ).json()
    assert [t["id"] for t in listed["items"]] == [tid]

    # Yazışma değişmez
    with site_scope(w.site_id):
        async with w.factory() as session:
            message = await session.scalar(select(SupportTicketMessage))
            assert message is not None
            message.body = "değişti"
            with pytest.raises(Exception, match="değiştirilemez"):
                await session.commit()


async def test_sla_asimi_ve_nobet_hatti(destek: tuple[World, dict[str, str], Clock]) -> None:
    w, platform, clock = destek
    async with w.factory() as session, session.begin():
        plan = Plan(name="Profesyonel", support_tier="critical_24_7")
        session.add(plan)
        await session.flush()
        site = await session.get(Site, w.site_id)
        assert site is not None
        site.plan_id = plan.id
    critical = await w.post("/support-tickets", ticket_body(priority="critical"))
    assert critical.json()["data"]["on_call"] is True
    assert "nöbet hattına" in critical.json()["message"]
    normal = await w.post("/support-tickets", ticket_body(priority="normal"))
    assert normal.json()["data"]["on_call"] is False

    clock.now = T0 + timedelta(hours=2)
    listed = (await w.api.get("/api/v1/platform/support-tickets", headers=platform)).json()
    first = listed["items"][0]  # kritik önce
    assert first["priority"] == "critical"
    assert first["sla"]["first_breached"] is True
    assert listed["items"][1]["sla"]["first_breached"] is False

    result = await svc.run_jobs(w.factory, now=clock.now)
    assert (result.first_breached, result.resolve_breached) == (1, 0)
    again = await svc.run_jobs(w.factory, now=clock.now)  # tekrar güvenli
    assert again.first_breached == 0


# --- 83: bilgi bankası -----------------------------------------------------------------


async def test_bilgi_bankasi(destek: tuple[World, dict[str, str], Clock]) -> None:
    w, platform, _ = destek
    body = {
        "title": "Satın alma talebi açma",
        "body": "Satın alma ekranında yeni talep düğmesine basın ve kalemleri girin.",
        "audience": "staff",
        "screens": ["/satin-alma", "satin-alma", ""],
    }
    denied = await w.api.post("/api/v1/platform/knowledge-articles", json=body, headers=w.headers)
    assert denied.status_code == 404
    short = await w.api.post(
        "/api/v1/platform/knowledge-articles", json={**body, "body": "kısa"}, headers=platform
    )
    assert short.status_code == 422
    assert "body" in short.json()["error"]["fields"]
    created = await w.api.post("/api/v1/platform/knowledge-articles", json=body, headers=platform)
    assert created.status_code == 201, created.text
    article = created.json()["data"]
    assert article["screens"] == ["satin-alma"]

    found = await w.api.get(
        "/api/v1/knowledge-articles",
        params={"screen": "satin-alma/talepler", "audience": "staff"},
        headers=w.headers,
    )
    assert [a["id"] for a in found.json()["items"]] == [article["id"]]
    none = await w.api.get(
        "/api/v1/knowledge-articles", params={"screen": "rezervasyon"}, headers=w.headers
    )
    assert none.json()["items"] == []
    search = await w.api.get(
        "/api/v1/knowledge-articles", params={"q": "kalemleri"}, headers=w.headers
    )
    assert search.json()["total"] == 1

    url = f"/api/v1/knowledge-articles/{article['id']}/feedback"
    await w.api.post(url, json={"helpful": False}, headers=w.headers)
    voted = await w.api.post(url, json={"helpful": True}, headers=w.headers)  # oy değişir
    assert (voted.json()["data"]["helpful"], voted.json()["data"]["not_helpful"]) == (1, 0)
    await w.api.post(url, json={"helpful": False}, headers=platform)
    patched = await w.api.patch(
        f"/api/v1/platform/knowledge-articles/{article['id']}",
        json={"title": "Satın alma talebi nasıl açılır"},
        headers=platform,
    )
    assert patched.status_code == 200
    out = patched.json()["data"]
    assert (out["title"], out["helpful"], out["not_helpful"]) == (
        "Satın alma talebi nasıl açılır",
        1,
        1,
    )


# --- 84: destek oturumu (K12) ----------------------------------------------------------


async def _request(w: World, platform: dict[str, str], **body: Any) -> Any:
    payload = {"site_slug": AKSU, "reason": "DST-0001 tahsilat hatası incelemesi", "hours": 2}
    payload.update(body)
    return await w.api.post("/api/v1/platform/support-sessions", json=payload, headers=platform)


async def test_destek_oturumu_onaysiz_acilmaz_onayla_salt_okunur(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    w, platform, clock = destek
    site_url = f"/api/v1/sites/{AKSU}"

    bad = await _request(w, platform, hours=25, reason="kısa")
    assert bad.status_code == 422
    assert {"hours", "reason"} <= set(bad.json()["error"]["fields"])
    unknown = await _request(w, platform, site_slug="olmayan-site")
    assert unknown.status_code == 422
    assert "site_slug" in unknown.json()["error"]["fields"]
    created = await _request(w, platform)
    assert created.status_code == 201, created.text
    assert created.json()["message"] == "Oturum talebi tesis yöneticisinin onayına gönderildi."

    sessions = (await w.api.get("/api/v1/platform/support-sessions", headers=platform)).json()
    sid = sessions["items"][0]["id"]
    assert sessions["items"][0]["status"] == "pending"
    dup = await _request(w, platform)
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "exists")

    # Onaydan önce erişim yok: site 404
    assert (await w.api.get(site_url, headers=platform)).status_code == 404
    # Tesis yöneticisine onay bildirimi
    with site_scope(w.site_id):
        async with w.factory() as session:
            kinds = list(await session.scalars(select(Notification.kind)))
    assert "support.session_requested" in kinds

    # Onay members.manage ister
    accounting = await staff_with(w, "muhasebe@test.local", "Muhasebe")
    denied = await w.api.post(f"{site_url}/support-sessions/{sid}/approve", headers=accounting)
    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == "Destek oturumunu tesis yöneticisi onaylar."

    approved = await w.post(f"/support-sessions/{sid}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "active"
    assert approved.json()["data"]["ends_at"].startswith("2026-06-20T11:00")
    again = await w.post(f"/support-sessions/{sid}/approve")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "invalid_status")

    # Bant: aktif oturum sitedeki herkese görünür
    active = (await w.get("/support-sessions/active")).json()
    assert active["requested_by"] == "Pelin DESTEK"

    # Salt okunur erişim: okuma var, yazma 403, kişisel iletişim izni yok
    site = (await w.api.get(site_url, headers=platform)).json()
    assert (site["access_mode"], site["read_only"]) == ("support_session", True)
    assert "units.read" in site["permissions"]
    assert "people.contact" not in site["permissions"]
    assert "members.manage" not in site["permissions"]
    me = (await w.api.get("/api/v1/me", headers=platform)).json()
    assert [s["slug"] for s in me["sites"]] == [AKSU]
    assert me["sites"][0]["read_only"] is True
    units = await w.api.get(f"{site_url}/units", headers=platform)
    assert units.status_code == 200
    write = await w.api.post(f"{site_url}/blocks", json={"name": "Z"}, headers=platform)
    assert (write.status_code, write.json()["error"]["code"]) == (403, "read_only")
    end_own = await w.api.post(f"{site_url}/support-sessions/{sid}/end", headers=platform)
    assert end_own.status_code == 403
    # Site izolasyonu: onay yalnız Aksu için — Yıldız 404
    assert (await w.api.get(f"/api/v1/sites/{YILDIZ}", headers=platform)).status_code == 404
    assert (await w.api.get(f"/api/v1/sites/{YILDIZ}/units", headers=platform)).status_code == 404

    # Denetim: her okuma "destek oturumu" aktörüyle; yazma denemesi kayıt üretmedi
    with site_scope(w.site_id):
        async with w.factory() as session:
            rows = list(
                await session.scalars(
                    select(AuditLog).where(AuditLog.actor_type == "support_session")
                )
            )
    assert rows
    assert all(r.action == "view" for r in rows)
    assert any(r.after and r.after["path"].endswith("/units") for r in rows)
    assert rows[0].actor_name == "Pelin DESTEK (destek)"

    # Süre dolunca erişim kendiliğinden kapanır; iş durumu yazar
    clock.now = T0 + timedelta(hours=2, minutes=1)
    assert (await w.api.get(site_url, headers=platform)).status_code == 404
    listed = (await w.get("/support-sessions")).json()
    assert listed["items"][0]["status"] == "expired"
    result = await svc.run_jobs(w.factory, now=clock.now)
    assert result.sessions_expired == 1
    # Yeni talep açılabilir
    assert (await _request(w, platform)).status_code == 201


async def test_destek_oturumu_red_ve_erken_bitirme(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    w, platform, clock = destek
    first = (await _request(w, platform)).json()["data"]["id"]
    rejected = await w.post(f"/support-sessions/{first}/reject", {"reason": "Gerek yok"})
    assert rejected.json()["data"]["status"] == "rejected"
    assert rejected.json()["data"]["reject_reason"] == "Gerek yok"
    assert (await w.api.get(f"/api/v1/sites/{AKSU}", headers=platform)).status_code == 404

    second = (await _request(w, platform)).json()["data"]["id"]
    await w.post(f"/support-sessions/{second}/approve")
    assert (await w.api.get(f"/api/v1/sites/{AKSU}", headers=platform)).status_code == 200
    clock.now = T0 + timedelta(minutes=30)
    ended = await w.post(f"/support-sessions/{second}/end")
    assert ended.status_code == 200
    assert (ended.json()["data"]["status"], ended.json()["data"]["ended_by"]) == (
        "expired",
        "Kerem YILDIRIM",
    )
    assert (await w.api.get(f"/api/v1/sites/{AKSU}", headers=platform)).status_code == 404
    assert (await w.get("/support-sessions/active")).json() is None
    # Başka sitenin oturumu bu siteden yönetilemez
    other = await w.api.post(
        f"/api/v1/sites/{YILDIZ}/support-sessions/{second}/approve", headers=w.headers
    )
    assert other.status_code == 404


# --- PLT-12: sakin olarak görüntüle ------------------------------------------------------


async def _viewer(w: World) -> dict[str, str]:
    role = await w.post(
        "/roles",
        {
            "base_key": "hr",
            "name": "Sakin Destek",
            "permissions": ["people.read", "support.view_as_resident"],
        },
    )
    assert role.status_code == 201, role.text
    return await staff_with(w, "destek.sorumlusu@test.local", role.json()["data"]["key"])


async def test_sakin_olarak_goruntule(destek: tuple[World, dict[str, str], Clock]) -> None:
    w, _, clock = destek
    resident = await create_user(
        w.factory, "ayse@test.local", full_name="Ayşe YILMAZ", kind="resident"
    )
    await add_site_membership(
        w.factory, w.site_id, resident.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    start_url = f"/people/{w.person_id}/view-as-sessions"

    # İzni olmayan yönetici: 403 (hiçbir şablonda varsayılan açık değil)
    denied = await w.post(start_url, {"reason": "Sakin ekranı farklı görünüyor"})
    assert denied.status_code == 403

    viewer = await _viewer(w)
    no_reason = await w.api.post(w.url(start_url), json={"reason": ""}, headers=viewer)
    assert no_reason.status_code == 422
    assert "reason" in no_reason.json()["error"]["fields"]
    started = await w.api.post(
        w.url(start_url), json={"reason": "Sakin ekranı farklı görünüyor"}, headers=viewer
    )
    assert started.status_code == 201, started.text
    session = started.json()["data"]
    assert session["person_name"] == "Ayşe YILMAZ"
    assert session["ends_at"].startswith("2026-06-20T09:30")
    assert session["resident_notified"] is True
    with site_scope(w.site_id):
        async with w.factory() as db:
            note = await db.scalar(select(Notification).where(Notification.user_id == resident.id))
    assert note is not None
    assert note.title == "Site yönetiminiz destek amacıyla hesabınızı görüntüledi"

    view = {**viewer, "X-View-As-Session": session["id"]}
    site = (await w.api.get(w.url(""), headers=view)).json()
    assert (site["access_mode"], site["read_only"]) == ("view_as", True)
    assert site["view_as_person_name"] == "Ayşe YILMAZ"
    assert "people.read" not in site["permissions"]  # görüntüleyenin yetkileri eklenmez
    home = await w.api.get(w.url("/resident/home"), headers=view)
    assert home.status_code == 200, home.text
    # Yetki sakininkiyle sınırlı: yönetim ekranı 403
    assert (await w.api.get(w.url("/people"), headers=view)).status_code == 403
    pay = await w.api.post(
        w.url("/resident/requests"), json={"title": "x", "description": "y"}, headers=view
    )
    assert (pay.status_code, pay.json()["error"]["code"]) == (403, "read_only")

    # Başka kullanıcı bu oturumu kullanamaz; başka sitede geçmez
    stolen = await w.api.get(
        w.url("/resident/home"), headers={**w.headers, "X-View-As-Session": session["id"]}
    )
    assert stolen.status_code == 403
    cross = await w.api.get(
        w.url("/resident/home", YILDIZ), headers={**w.headers, "X-View-As-Session": session["id"]}
    )
    assert cross.status_code in (403, 404)

    # Denetim: gerekçe, başlangıç ve açılan ekranlar
    with site_scope(w.site_id):
        async with w.factory() as db:
            rows = list(
                await db.scalars(select(AuditLog).where(AuditLog.entity == "view_as_sessions"))
            )
    assert any(r.action == "create" and r.after and r.after["reason"] for r in rows)
    assert any(
        r.action == "view" and r.after and r.after["path"].endswith("/resident/home") for r in rows
    )

    # 30 dakika sonra kapanır
    clock.now = T0 + timedelta(minutes=31)
    expired = await w.api.get(w.url("/resident/home"), headers=view)
    assert (expired.status_code, expired.json()["error"]["code"]) == (403, "view_as_ended")
    assert (await svc.run_jobs(w.factory, now=clock.now)).view_as_expired == 1


async def test_sakin_olarak_goruntule_cikis_ve_bildirim_ayari(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    w, _, _ = destek
    settings = await w.api.put(
        w.url("/settings/view-as"), json={"notify_resident": False}, headers=w.headers
    )
    assert settings.status_code == 200
    assert (await w.get("/settings/view-as")).json() == {"notify_resident": False}
    viewer = await _viewer(w)
    started = await w.api.post(
        w.url(f"/people/{w.person_id}/view-as-sessions"),
        json={"reason": "Borç ekranı kontrolü"},
        headers=viewer,
    )
    session = started.json()["data"]
    assert session["resident_notified"] is False
    view = {**viewer, "X-View-As-Session": session["id"]}
    out = await w.api.delete(w.url(f"/view-as-sessions/{session['id']}"), headers=view)
    assert out.status_code == 200
    assert out.json()["data"]["status"] == "ended"
    after = await w.api.get(w.url("/resident/home"), headers=view)
    assert after.status_code == 403


async def test_sakin_olarak_goruntulemede_kisisel_ekranlar_sakinin(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    """Bildirimler, KVKK onayları ve başvuruları görüntüleyenin değil görüntülenen sakinin
    hesabından gelir (PLT-12: ekran sakinin gözünden); okundu işaretleme ve veri paketi 403."""
    w, _, _ = destek
    resident = await create_user(
        w.factory, "ayse@test.local", full_name="Ayşe YILMAZ", kind="resident"
    )
    await add_site_membership(
        w.factory, w.site_id, resident.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    ayse = await login(w, "ayse@test.local")
    consent = await w.api.put(
        w.url("/my-privacy/explicit-consents/communications"),
        json={"granted": True},
        headers=ayse,
    )
    assert consent.status_code == 200, consent.text
    asked = await w.api.post(
        w.url("/resident/privacy-requests"), json={"kind": "access"}, headers=ayse
    )
    assert asked.status_code == 201, asked.text

    viewer = await _viewer(w)
    async with w.factory() as db:
        viewer_id = await db.scalar(
            select(User.id).where(User.email == "destek.sorumlusu@test.local")
        )
    assert viewer_id is not None
    with site_scope(w.site_id):
        async with w.factory() as db, db.begin():
            await notify(
                db,
                [viewer_id],
                Message(NotificationCategory.OPERATIONS, "test.viewer", "Görüntüleyenin bildirimi"),
                dedupe_key="test:viewer",
            )
    own = (await w.api.get(w.url("/notifications"), headers=viewer)).json()
    assert [n["title"] for n in own["items"]] == ["Görüntüleyenin bildirimi"]

    started = await w.api.post(
        w.url(f"/people/{w.person_id}/view-as-sessions"),
        json={"reason": "Bildirimler görünmüyor diyor"},
        headers=viewer,
    )
    assert started.status_code == 201, started.text
    view = {**viewer, "X-View-As-Session": started.json()["data"]["id"]}

    listed = await w.api.get(w.url("/notifications"), headers=view)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    titles = [n["title"] for n in body["items"]]
    assert "Görüntüleyenin bildirimi" not in titles
    assert "Site yönetiminiz destek amacıyla hesabınızı görüntüledi" in titles
    assert body["total"] == len(titles)
    assert body["unread_count"] == body["total"]
    count = await w.api.get(w.url("/notifications/unread-count"), headers=view)
    assert count.json() == body["unread_count"]
    read = await w.api.post(w.url("/notifications/read"), json={"ids": None}, headers=view)
    assert (read.status_code, read.json()["error"]["code"]) == (403, "read_only")
    again = await w.api.get(w.url("/notifications/unread-count"), headers=view)
    assert again.json() == body["unread_count"]  # sakinin okunmamışları değişmedi
    # Görüntüleyenin kendi bildirimi başlıksız istekte yine kendisine görünür
    mine = await w.api.get(w.url("/notifications/unread-count"), headers=viewer)
    assert mine.json() == 1

    privacy = await w.api.get(w.url("/my-privacy"), headers=view)
    assert privacy.status_code == 200, privacy.text
    assert privacy.json()["explicit"][0]["granted"] is True
    requests = await w.api.get(w.url("/resident/privacy-requests"), headers=view)
    assert requests.status_code == 200, requests.text
    assert [r["kind"] for r in requests.json()["items"]] == ["access"]
    export = await w.api.get(w.url("/my-privacy/data-export"), headers=view)
    assert (export.status_code, export.json()["error"]["code"]) == (403, "read_only")


async def test_sakin_olarak_goruntulemede_hesabi_olmayan_kisinin_bildirimi_yok(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    """Sisteme giriş yapmayan kişi: görüntüleyenin bildirimleri yerine boş liste."""
    w, _, _ = destek
    viewer = await _viewer(w)
    async with w.factory() as db:
        viewer_id = await db.scalar(
            select(User.id).where(User.email == "destek.sorumlusu@test.local")
        )
    assert viewer_id is not None
    with site_scope(w.site_id):
        async with w.factory() as db, db.begin():
            await notify(
                db,
                [viewer_id],
                Message(NotificationCategory.OPERATIONS, "test.viewer", "Görüntüleyenin bildirimi"),
                dedupe_key="test:viewer",
            )
    started = await w.api.post(
        w.url(f"/people/{w.person_id}/view-as-sessions"),
        json={"reason": "Hesabı yok, ekranına bakılıyor"},
        headers=viewer,
    )
    assert started.status_code == 201, started.text
    assert started.json()["data"]["resident_notified"] is False
    view = {**viewer, "X-View-As-Session": started.json()["data"]["id"]}
    listed = (await w.api.get(w.url("/notifications"), headers=view)).json()
    assert (listed["items"], listed["total"], listed["unread_count"]) == ([], 0, 0)
    assert (await w.api.get(w.url("/notifications/unread-count"), headers=view)).json() == 0
    privacy = (await w.api.get(w.url("/my-privacy"), headers=view)).json()
    assert (privacy["accepted"], privacy["explicit"][0]["granted"]) == ([], False)


async def test_destek_ekibi_oturumda_sakin_olarak_goruntuleyebilir(
    destek: tuple[World, dict[str, str], Clock],
) -> None:
    w, platform, _ = destek
    sid = (await _request(w, platform)).json()["data"]["id"]
    await w.post(f"/support-sessions/{sid}/approve")
    started = await w.api.post(
        w.url(f"/people/{w.person_id}/view-as-sessions"),
        json={"reason": "DST-0001 sakin ekranı"},
        headers=platform,
    )
    assert started.status_code == 201, started.text
    view = {**platform, "X-View-As-Session": started.json()["data"]["id"]}
    assert (await w.api.get(w.url("/resident/home"), headers=view)).status_code == 200
    # Oturum bitince sakin görünümü de kapanır
    await w.post(f"/support-sessions/{sid}/end")
    assert (await w.api.get(w.url("/resident/home"), headers=view)).status_code == 404
    with all_sites_scope():
        async with w.factory() as db:
            types = set(
                await db.scalars(
                    select(AuditLog.actor_type).where(AuditLog.entity == "view_as_sessions")
                )
            )
    assert "support_session" in types
