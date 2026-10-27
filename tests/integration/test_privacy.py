"""KVKK — Yazılım Dokümanı RAP-04, PLT-01 adım 14–15; servis isteği 80; PM kararı P6
(gerçek PostgreSQL).

Kurgu `finance_world`: Aksu'da A-1 maliki Ayşe; yönetici Kerem iki sitede; bugün 20.06.2026.
"""

import uuid
from datetime import UTC, date, datetime

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import (
    AuditLog,
    ExplicitConsent,
    Notification,
    PrivacyConsent,
    Registration,
)
from site_yonetim.services import privacy as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

NOTICE = (
    "Kişisel verileriniz site yönetimi tarafından 6698 sayılı KVKK kapsamında; aidat ve ortak "
    "gider takibi, duyuru ve talep hizmetleri ile site güvenliği amaçlarıyla işlenir. Hukuki "
    "sebep sözleşmenin ifası ve kanuni yükümlülüktür. Haklarınız için yönetime başvurabilirsiniz."
)


async def _publish(
    w: World, summary: str = "İlk sürüm", headers: dict[str, str] | None = None
) -> httpx2.Response:
    return await w.api.post(
        w.url("/privacy-notices"),
        json={"text": NOTICE, "summary": summary},
        headers=headers or w.headers,
    )


async def _member(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def _resident(w: World) -> dict[str, str]:
    user = await create_user(w.factory, "ayse@test.local", kind="resident")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    return await login_headers(w.api, "ayse@test.local")


# --- Aydınlatma metni ve onay kapısı (P6) ----------------------------------------------------


async def test_metin_yayinlama_dogrulama_ve_yetki(world: World) -> None:
    short = await world.post("/privacy-notices", {"text": "kısa", "summary": ""})
    assert short.status_code == 422
    assert set(short.json()["error"]["fields"]) == {"text", "summary"}
    board = await _member(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    assert (await _publish(world, headers=board)).status_code == 403

    first = await _publish(world)
    assert first.status_code == 201, first.text
    assert first.json()["data"]["version"] == 1
    second = await _publish(world, "Saklama süreleri eklendi")
    assert second.json()["data"]["version"] == 2

    listed = (await world.get("/privacy-notices")).json()
    assert [n["version"] for n in listed["items"]] == [2, 1]
    assert listed["items"][0]["published_by"] == "Kerem YILDIRIM"
    # Yayınlayan yönetici metni onaylamış sayılır; işine devam eder.
    assert (await world.get("/privacy-notices")).status_code == 200
    with site_scope(world.site_id):
        async with world.factory() as session:
            assert (await session.scalars(select(AuditLog.entity))).all().count(
                "privacy_notices"
            ) == 2


async def test_onaysiz_oturumda_yalniz_me_ve_onay_uclari(world: World) -> None:
    board = await _member(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    assert (await world.api.get(world.url("/units"), headers=board)).status_code == 200
    await _publish(world)

    blocked = await world.api.get(world.url("/units"), headers=board)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "consent_required"
    assert (await world.api.get("/api/v1/me/sessions", headers=board)).status_code == 403
    me = (await world.api.get("/api/v1/me", headers=board)).json()
    assert me["consent_required"] is True

    state = (await world.api.get("/api/v1/me/consents", headers=board)).json()
    assert state["consent_required"] is True
    [pending] = state["pending"]
    assert (pending["site_slug"], pending["version"]) == ("aksu-konaklari", 1)
    assert pending["text"].startswith("Kişisel verileriniz")

    accepted = await world.api.post(
        "/api/v1/me/consents", json={"notice_ids": [pending["notice_id"]]}, headers=board
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["consent_required"] is False
    assert accepted.json()["data"]["given"][0]["source"] == "login"
    assert (await world.api.get(world.url("/units"), headers=board)).status_code == 200
    assert (await world.api.get("/api/v1/me", headers=board)).json()["consent_required"] is False

    # Yeni sürüm: herkes yeniden onaylar; eski sürümün kimliğiyle onay 409.
    await _publish(world, "İkinci sürüm")
    assert (await world.api.get(world.url("/units"), headers=board)).status_code == 403
    stale = await world.api.post(
        "/api/v1/me/consents", json={"notice_ids": [pending["notice_id"]]}, headers=board
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "consent_outdated"
    [newer] = (await world.api.get("/api/v1/me/consents", headers=board)).json()["pending"]
    assert newer["version"] == 2


async def test_onay_baska_siteye_sizmaz(world: World) -> None:
    """Yalnız Yıldız'daki kullanıcı Aksu'nun metnini onaylamak zorunda değildir; Yıldız'ın
    sürüm listesi boştur."""
    user = await create_user(world.factory, "yildiz@test.local")
    await add_site_membership(world.factory, world.other_site_id, user.id, "Yönetim Kurulu Üyesi")
    headers = await login_headers(world.api, "yildiz@test.local")
    await _publish(world)

    assert (await world.api.get("/api/v1/me", headers=headers)).json()["consent_required"] is False
    other = await world.api.get(world.url("/privacy-notices", "yildiz-sitesi"), headers=headers)
    assert other.json()["total"] == 0
    aksu = (await world.api.get("/api/v1/me/consents", headers=headers)).json()
    accept = await world.api.post(
        "/api/v1/me/consents", json={"notice_ids": [str(uuid.uuid4())]}, headers=headers
    )
    assert aksu["pending"] == []
    assert accept.status_code == 409


async def test_herkese_acik_metin(world: World) -> None:
    empty = await world.api.get("/api/v1/public/privacy-notice/aksu-konaklari")
    assert empty.status_code == 200
    assert empty.json()["version"] is None
    await _publish(world)
    body = (await world.api.get("/api/v1/public/privacy-notice/aksu-konaklari")).json()
    assert (body["site_name"], body["version"]) == ("Aksu Konakları", 1)
    assert body["text"] == NOTICE
    assert (await world.api.get("/api/v1/public/privacy-notice/yok-boyle")).status_code == 404


async def test_davet_ve_kayit_onayi_hesaba_baglanir(world: World) -> None:
    await _publish(world)
    user = await create_user(world.factory, "kayit@test.local", kind="resident")
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            registration = Registration(
                number=1, first_name="Can", last_name="Er", phone="+905321112233",
                email="kayit@test.local", unit_text="A 1", relation="owner",
                kvkk_ack_at=datetime.now(UTC), explicit_consent_at=datetime.now(UTC),
            )  # fmt: skip
            session.add(registration)
            await session.flush()
            await svc.link_registration(session, user.id, registration)
            await svc.link_registration(session, user.id, registration)  # tekrar güvenli
        async with world.factory() as session:
            consents = list(await session.scalars(select(PrivacyConsent)))
            rizalar = list(await session.scalars(select(ExplicitConsent)))
    mine = [c for c in consents if c.user_id == user.id]
    assert [(c.version, c.source) for c in mine] == [(1, "registration")]
    assert [(r.purpose, r.granted, r.source) for r in rizalar] == [
        ("communications", True, "registration")
    ]


# --- İlgili kişi başvurusu -----------------------------------------------------------------


async def test_sakin_basvurusu_ve_yonetici_yaniti(world: World) -> None:
    ayse = await _resident(world)
    url = world.url("/resident/privacy-requests")
    missing = await world.api.post(url, json={"kind": "erase", "detail": ""}, headers=ayse)
    assert missing.status_code == 422
    assert missing.json()["error"]["fields"] == {"detail": "Talebinizi kısaca açıklayın."}

    created = await world.api.post(url, json={"kind": "access"}, headers=ayse)
    assert created.status_code == 201, created.text
    assert created.json()["data"]["due_on"] == "2026-07-20"
    again = await world.api.post(url, json={"kind": "access"}, headers=ayse)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_open"
    erase = await world.api.post(
        url, json={"kind": "erase", "detail": "Telefon numaramı silin lütfen."}, headers=ayse
    )
    assert erase.status_code == 201

    # Yönetici sakin değildir → 403
    assert (await world.post("/resident/privacy-requests", {"kind": "access"})).status_code == 403
    listed = (await world.get("/privacy-requests")).json()
    assert listed["total"] == 2
    first = listed["items"][0]
    assert (first["name"], first["units"], first["days_left"]) == ("Ayşe YILMAZ", ["A-1"], 30)
    with site_scope(world.site_id):
        async with world.factory() as session:
            notes = list(await session.scalars(select(Notification)))
    assert any(n.kind == "privacy.request_created" for n in notes)

    access_id = next(r["id"] for r in listed["items"] if r["kind"] == "access")
    package = await world.get(f"/privacy-requests/{access_id}/data-package")
    assert package.status_code == 200
    data = package.json()
    assert data["kisi"]["ad"] == "Ayşe"
    assert data["bolumler"][0]["bolum"] == "A-1"
    assert data["kvkk_basvurulari"]

    erase_id = next(r["id"] for r in listed["items"] if r["kind"] == "erase")
    no_reason = await world.post(
        f"/privacy-requests/{erase_id}/answer",
        {"response": "Finansal kayıtlar saklanır.", "outcome": "partial"},
    )
    assert no_reason.status_code == 422
    assert set(no_reason.json()["error"]["fields"]) == {"retained"}
    answered = await world.post(
        f"/privacy-requests/{erase_id}/answer",
        {"response": "Telefon silindi; tahakkuklar saklanır.", "outcome": "partial",
         "retained": "Tahakkuk ve tahsilat — TTK m.82, 10 yıl"},
    )  # fmt: skip
    assert answered.status_code == 200, answered.text
    assert answered.json()["data"]["status"] == "answered"
    twice = await world.post(
        f"/privacy-requests/{erase_id}/answer",
        {"response": "Tekrar yanıt deniyorum.", "outcome": "fulfilled"},
    )
    assert twice.status_code == 409

    mine = (await world.api.get(url, headers=ayse)).json()["items"]
    done = next(r for r in mine if r["kind"] == "erase")
    assert (done["outcome"], done["outcome_label"]) == ("partial", "Kısmen yerine getirildi")
    assert "name" not in done  # sakin kendi listesinde yönetim alanlarını almaz

    # Başka site: liste boş, kayıt yok (404)
    other = await world.api.get(
        world.url("/privacy-requests", "yildiz-sitesi"), headers=world.headers
    )
    assert other.json()["total"] == 0
    cross = await world.api.post(
        world.url(f"/privacy-requests/{access_id}/answer", "yildiz-sitesi"),
        json={"response": "Yanıt veriyorum burada.", "outcome": "fulfilled"},
        headers=world.headers,
    )
    assert cross.status_code == 404


async def test_basvuru_yetkisi(world: World) -> None:
    board = await _member(world, "yk@test.local", "Yönetim Kurulu Üyesi")
    assert (await world.api.get(world.url("/privacy-requests"), headers=board)).status_code == 403
    assert (await world.api.get(world.url("/settings/retention"), headers=board)).status_code == 403


async def test_son_gun_uyarisi_gunde_bir(world: World) -> None:
    ayse = await _resident(world)
    await world.api.post(world.url("/resident/privacy-requests"), json={"kind": "access"},
                         headers=ayse)  # fmt: skip
    assert await svc.run_due_warnings(world.factory, today=date(2026, 7, 1)) == {}
    warned = await svc.run_due_warnings(world.factory, today=date(2026, 7, 15))
    assert warned == {world.site_id: 1}
    assert await svc.run_due_warnings(world.factory, today=date(2026, 7, 15)) == {}
    with site_scope(world.site_id):
        async with world.factory() as session:
            notes = list(
                await session.scalars(
                    select(Notification).where(Notification.kind == "privacy.request_due")
                )
            )
    assert [n.title for n in notes] == ["KVKK başvurusuna 5 gün kaldı"]


# --- Kendi verim: açık rıza, verilerimi indir ------------------------------------------------


async def test_acik_riza_ve_verilerimi_indir(world: World) -> None:
    ayse = await _resident(world)
    await _publish(world)
    pending = (await world.api.get("/api/v1/me/consents", headers=ayse)).json()["pending"]
    await world.api.post(
        "/api/v1/me/consents", json={"notice_ids": [p["notice_id"] for p in pending]}, headers=ayse
    )
    mine = (await world.api.get(world.url("/my-privacy"), headers=ayse)).json()
    assert mine["notice"]["version"] == 1
    assert [a["version"] for a in mine["accepted"]] == [1]
    assert mine["explicit"] == [
        {"purpose": "communications", "label": mine["explicit"][0]["label"], "granted": False,
         "changed_at": None}
    ]  # fmt: skip

    url = world.url("/my-privacy/explicit-consents/communications")
    given = await world.api.put(url, json={"granted": True}, headers=ayse)
    assert given.json()["data"]["explicit"][0]["granted"] is True
    withdrawn = await world.api.put(url, json={"granted": False}, headers=ayse)
    assert withdrawn.json()["data"]["explicit"][0]["granted"] is False
    assert "geri çektiniz" in withdrawn.json()["message"]
    bad = await world.api.put(
        world.url("/my-privacy/explicit-consents/pazarlama"), json={"granted": True}, headers=ayse
    )
    assert bad.status_code == 422

    export = await world.api.get(world.url("/my-privacy/data-export"), headers=ayse)
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    body = export.json()
    assert body["veri_sorumlusu"] == "Aksu Konakları"
    assert [r["verildi"] for r in body["acik_riza"]] == [True, False]
    assert body["aydinlatma_onaylari"][0]["surum"] == 1


@pytest.mark.parametrize("kind", ["rectify", "object"])
async def test_aciklama_zorunlu(world: World, kind: str) -> None:
    ayse = await _resident(world)
    response = await world.api.post(
        world.url("/resident/privacy-requests"), json={"kind": kind, "detail": "kısa"},
        headers=ayse,
    )  # fmt: skip
    assert response.status_code == 422


async def test_onay_gecmisi_degismez(world: World) -> None:
    """Onay kaydı ve metin sürümü değişmez (forbid_history_change)."""
    await _publish(world)
    for statement in (
        "UPDATE privacy_consents SET source = 'seed'",
        "DELETE FROM privacy_notices",
    ):
        with site_scope(world.site_id):
            async with world.factory() as session, session.begin():
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement))
