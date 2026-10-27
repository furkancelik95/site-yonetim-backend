"""İletişim, acil durum, anket (K31) ve şeffaflık raporu (K32) — servis istekleri 71–74,
Yazılım Dokümanı ILT-01..06, RAP-02 (gerçek PostgreSQL).

Kurgu `finance_world` (bugün 20.06.2026, saat 10:00 TSİ sabit): A-1 maliki Ayşe Yılmaz sakin
hesabıyla girer; duyuru, anket ve finans modülleri açık.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.api.deps import get_now
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import EmergencyUnit, Plan, PollBallot, PollParticipant, Site
from site_yonetim.services import announcements as ann_svc
from site_yonetim.services import emergencies as em_svc
from site_yonetim.services.sites import set_module_enabled
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

NOW = datetime(2026, 6, 20, 7, 0, tzinfo=UTC)  # 10:00 TSİ
MODULES = ["finance", "announcements", "surveys"]
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"


@pytest.fixture
async def comm(world: World) -> World:
    world.app.dependency_overrides[get_now] = lambda: NOW
    async with world.factory() as session, session.begin():
        plan = Plan(name="Geniş", max_units=150, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            site = await session.get(Site, world.site_id)
            assert site is not None
            for key in (ModuleKey.ANNOUNCEMENTS, ModuleKey.SURVEYS):
                await set_module_enabled(session, site, key, enable=True)
    return world


async def _login(w: World, email: str) -> dict[str, str]:
    """Giriş gerçek saatle (jeton süresi gerçek saate göre); istekler sabit saatle."""
    frozen = w.app.dependency_overrides.pop(get_now)
    try:
        return await login_headers(w.api, email)
    finally:
        w.app.dependency_overrides[get_now] = frozen


async def staff(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await _login(w, email)


async def ayse(w: World) -> dict[str, str]:
    user = await create_user(w.factory, "ayse@test.local", kind="resident", full_name="Ayşe YILMAZ")
    await add_site_membership(
        w.factory, w.site_id, user.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    return await _login(w, "ayse@test.local")


async def call(
    w: World, method: str, path: str, headers: dict[str, str] | None = None, **kw: Any
) -> httpx2.Response:
    return await w.api.request(method, w.url(path), headers=headers or w.headers, **kw)


def fields(response: httpx2.Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    return dict(response.json()["error"]["fields"])


ANN = {"title": "Su kesintisi", "body": "Yarın 10:00–14:00 su yok.", "channels": ["in_app"]}


# --- 71 Okuma onayı, zamanlama -------------------------------------------------------------


async def test_okuma_onayi_raporu_ve_sakin_onayi(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    bad = await call(
        w, "POST", "/announcements", json={**ANN, "requires_ack": True, "ack_hours": 0}
    )
    assert fields(bad) == {"ack_hours": "Son onay süresi 1–720 saat."}
    created = await call(
        w, "POST", "/announcements",
        json={**ANN, "requires_ack": True, "ack_hours": 48, "sms_fallback": True},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    a = created.json()["data"]
    assert "Okuma onayı 48 saat içinde bekleniyor." in created.json()["message"]
    assert (a["requires_ack"], a["ack_deadline"]) == (True, "2026-06-22T07:00:00Z")
    plain = (await call(w, "POST", "/announcements", json=ANN)).json()["data"]

    pending = (await call(w, "GET", "/resident/pending-acks", sakin)).json()
    assert [p["announcement_id"] for p in pending] == [a["id"]]
    mine = (await call(w, "GET", "/resident/announcements", sakin)).json()["items"]
    first = next(x for x in mine if x["id"] == a["id"])
    assert (first["requires_ack"], first["acked_at"]) == (True, None)

    ok = await call(w, "POST", f"/resident/announcements/{a['id']}/acknowledge", sakin)
    assert ok.status_code == 201, ok.text
    assert ok.json()["message"] == "Okuma onayınız kaydedildi."
    again = await call(w, "POST", f"/resident/announcements/{a['id']}/acknowledge", sakin)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already")
    no_ack = await call(w, "POST", f"/resident/announcements/{plain['id']}/acknowledge", sakin)
    assert no_ack.status_code == 404
    assert (await call(w, "GET", "/resident/pending-acks", sakin)).json() == []
    staff_ack = await call(w, "POST", f"/resident/announcements/{a['id']}/acknowledge")
    assert staff_ack.status_code == 403  # yalnız sakin

    report = (await call(w, "GET", f"/announcements/{a['id']}/read-report")).json()
    assert (report["recipient_count"], report["read_count"], report["not_acked"]) == (1, 1, 0)
    [ack] = report["acks"]
    assert (ack["name"], ack["units"], ack["channel"]) == ("Ayşe YILMAZ", ["A-1"], "in_app")
    assert report["recipients"][0]["acked_at"] is not None
    assert report["sms_fallback"]["due"] is False
    listed = (await call(w, "GET", "/announcements")).json()["items"]
    assert next(x for x in listed if x["id"] == a["id"])["ack_count"] == 1
    assert next(x for x in listed if x["id"] == plain["id"])["ack_count"] is None

    tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await call(w, "GET", f"/announcements/{a['id']}/read-report", tech)).status_code == 403
    other = await w.api.get(
        w.url(f"/announcements/{a['id']}/read-report", "yildiz-sitesi"), headers=w.headers
    )
    assert other.status_code == 404


async def test_zamanlama_sessiz_saat_iptal_ve_isler(comm: World) -> None:
    w = comm
    past = await call(
        w, "POST", "/announcements", json={**ANN, "publish_at": "2026-06-20T06:00:00Z"}
    )
    assert fields(past) == {"publish_at": "Yayın zamanı ileride olmalı."}
    # 23:30 TSİ sessiz saatte → ertesi gün 08:00'e kayar; kritik önem aşar
    night = await call(
        w, "POST", "/announcements", json={**ANN, "publish_at": "2026-06-20T20:30:00Z"}
    )
    assert night.status_code == 201, night.text
    assert "sessiz saat bitişine kaydırıldı" in night.json()["message"]
    assert night.json()["data"]["published_at"] is None
    critical = await call(
        w, "POST", "/announcements",
        json={**ANN, "importance": "critical", "publish_at": "2026-06-20T20:30:00Z",
              "requires_ack": True, "ack_hours": 1, "sms_fallback": True},
    )  # fmt: skip
    assert critical.status_code == 201
    scheduled = (await call(w, "GET", "/scheduled-announcements")).json()
    by_id = {s["id"]: s for s in scheduled}
    shifted = by_id[night.json()["data"]["id"]]
    assert (shifted["publish_at"], shifted["shifted"]) == ("2026-06-21T05:00:00Z", True)
    assert by_id[critical.json()["data"]["id"]]["shifted"] is False
    assert (await call(w, "GET", "/announcements")).json()["total"] == 0  # henüz yayında değil

    cancel = await call(w, "POST", f"/scheduled-announcements/{shifted['id']}/cancel")
    assert cancel.status_code == 200, cancel.text
    again = await call(w, "POST", f"/scheduled-announcements/{shifted['id']}/cancel")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "invalid_status")
    missing = await call(w, "POST", f"/scheduled-announcements/{uuid.uuid4()}/cancel")
    assert missing.status_code == 404

    [result] = await ann_svc.run_jobs(w.factory, now=datetime(2026, 6, 20, 21, 0, tzinfo=UTC))
    assert (result.published, result.fallbacks) == (1, 0)
    listed = (await call(w, "GET", "/announcements")).json()["items"]
    assert [x["importance"] for x in listed] == ["critical"]
    assert (await call(w, "GET", "/scheduled-announcements")).json() == []
    # Son onay süresi (1 saat) doldu → onaylamayana bir kez SMS yedeği (sağlayıcı yok: gönderilmez)
    [late] = await ann_svc.run_jobs(w.factory, now=datetime(2026, 6, 20, 23, 0, tzinfo=UTC))
    assert late.fallbacks == 1
    assert await ann_svc.run_jobs(w.factory, now=datetime(2026, 6, 21, 0, 0, tzinfo=UTC)) == []
    report = (await call(w, "GET", f"/announcements/{listed[0]['id']}/read-report")).json()
    assert report["sms_fallback"]["count"] == 1
    assert report["sms_fallback"]["sent"] is False
    assert "P16c" in report["sms_fallback"]["note"]


# --- 72 Şablonlar --------------------------------------------------------------------------


async def test_bildirim_sablonlari_surum_ve_dogrulama(comm: World) -> None:
    w = comm
    templates = (await call(w, "GET", "/notification-templates")).json()
    dues = next(t for t in templates if t["code"] == "dues_reminder")
    assert (dues["version"], dues["customized"], dues["channel"]) == (1, False, "sms")
    assert dues["sms"][0]["encoding"] == "Unicode"  # "Sayın" → Türkçe karakter
    assert any(t["channel"] == "announcement" for t in templates)

    url = "/notification-templates/dues_reminder"
    no_tr = await call(
        w, "PUT", url, json={"variants": [{"lang": "en", "body": "{{tutar}} {{vade}}"}]}
    )
    assert fields(no_tr) == {"variants": "Türkçe metin zorunlu."}
    missing = await call(
        w, "PUT", url, json={"variants": [{"lang": "tr", "body": "Aidat {{vade}}"}]}
    )
    assert fields(missing) == {"variants.tr": "Zorunlu değişken eksik: {{tutar}}"}
    unknown = await call(
        w, "PUT", url, json={"variants": [{"lang": "tr", "body": "{{tutar}} {{vade}} {{iban}}"}]}
    )
    assert fields(unknown) == {"variants.tr": "Tanımsız değişken: {{iban}}"}
    long = await call(
        w,
        "PUT",
        url,
        json={"variants": [{"lang": "tr", "body": "{{tutar}} {{vade}} " + "ş" * 300}]},
    )
    assert fields(long) == {"variants.tr": "SMS en fazla 3 parça olabilir."}

    saved = await call(
        w, "PUT", url, json={"variants": [{"lang": "tr", "body": "Aidat {{tutar}}, vade {{vade}}"}]}
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()["data"]
    assert (body["version"], body["customized"], len(body["history"])) == (2, True, 2)
    assert body["sms"] == [{"lang": "tr", "encoding": "GSM-7", "chars": 30, "segments": 1}]
    reset = (await call(w, "POST", f"{url}/reset")).json()["data"]
    assert (reset["version"], reset["customized"]) == (3, False)
    assert (
        await call(w, "PUT", "/notification-templates/yok", json={"variants": []})
    ).status_code == 404

    with site_scope(w.site_id):
        async with w.factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text("DELETE FROM notification_template_versions"))
    tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await call(w, "GET", "/notification-templates", tech)).status_code == 403


# --- 74 Acil bilgi kartı -------------------------------------------------------------------


CARD = {
    "muster_points": [{"name": "Otopark girişi", "directions": "Ana kapının karşısı", "map": PNG}],
    "phones": [
        {"label": "Acil çağrı", "number": "112"},
        {"label": "Güvenlik", "number": "0212 555 00 00"},
    ],
    "instructions": {"earthquake": "Çök, kapan, tutun."},
    "block_notes": [],
}


async def test_acil_bilgi_karti_ve_etag(comm: World) -> None:
    w = comm
    default = (await call(w, "GET", "/emergency-info-card")).json()
    assert (default["version"], default["phones"][0]["number"]) == (0, "112")
    empty = await call(w, "PUT", "/emergency-info-card", json={**CARD, "muster_points": []})
    assert fields(empty) == {"muster_points": "En az bir toplanma alanı tanımlayın."}
    bad = await call(
        w, "PUT", "/emergency-info-card",
        json={**CARD, "phones": [{"label": "X", "number": "12"}],
              "muster_points": [{"name": "Bahçe", "map": "data:image/svg+xml;base64,PHN2Zz4="}],
              "block_notes": [{"block_id": str(uuid.uuid4()), "note": "Kapı B"}]},
    )  # fmt: skip
    assert set(fields(bad)) == {"phones.0.number", "muster_points.0.map", "block_notes.0.block_id"}
    note = {"block_id": w.lookups["block"], "note": "B kapısından çıkın"}
    published = await call(w, "PUT", "/emergency-info-card", json={**CARD, "block_notes": [note]})
    assert published.status_code == 200, published.text
    card = published.json()["data"]
    assert (card["version"], card["phones"][1]["number"]) == (1, "02125550000")
    assert card["block_notes"][0]["block_name"] == "A"
    assert card["instructions"]["fire"]  # verilmeyen talimat varsayılan kalır

    sakin = await ayse(w)
    mine = await call(w, "GET", "/resident/emergency-info-card", sakin)
    assert mine.status_code == 200
    assert mine.json()["muster_points"][0]["name"] == "Otopark girişi"
    etag = mine.headers["etag"]
    cached = await call(w, "GET", "/resident/emergency-info-card", {**sakin, "If-None-Match": etag})
    assert cached.status_code == 304
    assert (await call(w, "GET", "/emergency-info-card", sakin)).status_code == 403
    guard = await staff(w, "guvenlik@test.local", "Güvenlik")
    assert (await call(w, "GET", "/emergency-info-card", guard)).status_code == 200
    assert (await call(w, "PUT", "/emergency-info-card", guard, json=CARD)).status_code == 403


# --- 73 Acil durum ----------------------------------------------------------------------------


async def test_acil_durum_pano_yoklama_ve_bitis(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    await call(w, "PUT", "/emergency-info-card", json=CARD)
    bad = await call(
        w, "POST", "/emergencies", json={"kind": "x", "scope": "blocks", "message": "a"}
    )
    assert set(fields(bad)) == {"kind", "message", "block_ids"}
    guard = await staff(w, "guvenlik@test.local", "Güvenlik")
    started = await call(
        w, "POST", "/emergencies", guard, json={"kind": "earthquake", "message": "Binayı boşaltın."}
    )
    assert started.status_code == 201, started.text
    e = started.json()["data"]
    assert "1 bölüm kapsamda, 1 kişiye" in started.json()["message"]
    again = await call(w, "POST", "/emergencies", json={"kind": "fire", "message": "Yangın var!"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_active")
    tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await call(w, "GET", "/emergencies/active", tech)).status_code == 403

    board = (await call(w, "GET", "/emergencies/active")).json()
    assert board["totals"] == {"total": 1, "safe": 0, "muster": 0, "help": 0, "none": 1}
    notes = (await call(w, "GET", "/notifications", sakin)).json()
    assert any(n["kind"] == "emergency.started" for n in notes["items"])

    mine = (await call(w, "GET", "/resident/emergency", sakin)).json()
    assert (mine["kind"], mine["units"][0]["unit_name"], mine["units"][0]["status"]) == (
        "earthquake", "A-1", None,
    )  # fmt: skip
    assert mine["muster_points"] == ["Otopark girişi"]
    no_point = await call(w, "POST", "/resident/emergency/status", sakin, json={"status": "muster"})
    assert fields(no_point) == {"muster_point": "Toplanma alanını seçin."}
    helped = await call(
        w, "POST", "/resident/emergency/status", sakin,
        json={"status": "help", "note": "Kat 4, yaşlı komşu"},
    )  # fmt: skip
    assert helped.json()["message"].startswith("Yardım isteğiniz")
    rows = (await call(w, "GET", "/emergencies/active")).json()["rows"]
    assert (rows[0]["status"], rows[0]["note"], rows[0]["by"]) == (
        "help", "Kat 4, yaşlı komşu", "Ayşe YILMAZ",
    )  # fmt: skip
    unit = rows[0]["unit_id"]
    skipped = await call(
        w, "POST", f"/emergencies/{e['id']}/muster-checks", guard,
        json={"unit_ids": [unit], "muster_point": "Otopark girişi"},
    )  # fmt: skip
    assert skipped.json()["data"] == {"marked": 0, "skipped_help": 1}
    wrong = await call(
        w, "POST", f"/emergencies/{e['id']}/muster-checks",
        json={"unit_ids": [unit], "muster_point": "Havuz"},
    )  # fmt: skip
    assert fields(wrong) == {"muster_point": "Toplanma alanı acil bilgi kartında yok."}
    await call(w, "POST", "/resident/emergency/status", sakin, json={"status": "safe"})
    marked = await call(
        w, "POST", f"/emergencies/{e['id']}/muster-checks",
        json={"unit_ids": [unit], "muster_point": "Otopark girişi"},
    )  # fmt: skip
    assert marked.json()["data"] == {"marked": 1, "skipped_help": 0}

    ended = await call(w, "POST", f"/emergencies/{e['id']}/end")
    assert ended.status_code == 200, ended.text
    data = ended.json()["data"]
    assert data["summary"] == {"total": 1, "safe": 0, "muster": 1, "help": 0, "none": 0}
    assert data["purge_on"] == "2026-07-20"
    assert [a["action"] for a in data["audit"]] == ["Acil durum başlatıldı", "Acil durum bitirildi"]
    assert (await call(w, "POST", f"/emergencies/{e['id']}/end")).status_code == 409
    assert (await call(w, "GET", "/emergencies/active")).json() is None
    assert (await call(w, "GET", "/resident/emergency", sakin)).json() is None
    late = await call(w, "POST", "/resident/emergency/status", sakin, json={"status": "safe"})
    assert (late.status_code, late.json()["error"]["code"]) == (409, "not_active")
    history = (await call(w, "GET", "/emergencies")).json()
    assert [h["id"] for h in history] == [e["id"]]
    other = await w.api.get(w.url("/emergencies", "yildiz-sitesi"), headers=w.headers)
    assert other.status_code == 404  # Yıldız'da modül kapalı


async def test_acil_durum_verisi_saklama_isi(comm: World) -> None:
    w = comm
    settings = (await call(w, "GET", "/emergency-settings")).json()
    assert (settings["retention_days"], settings["auto_purge"]) == (30, False)  # K8 / A-04
    e = (
        await call(w, "POST", "/emergencies", json={"kind": "gas", "message": "Gaz kaçağı!"})
    ).json()
    await call(w, "POST", f"/emergencies/{e['data']['id']}/end")
    later = NOW + timedelta(days=40)
    assert await em_svc.run_purge(w.factory, now=later) == []  # otomatik silme kapalı
    saved = await call(
        w, "PUT", "/emergency-settings", json={"retention_days": 30, "auto_purge": True}
    )
    assert saved.status_code == 200, saved.text
    [result] = await em_svc.run_purge(w.factory, now=later)
    assert result.purged == 1
    with site_scope(w.site_id):
        async with w.factory() as session:
            assert list(await session.scalars(select(EmergencyUnit))) == []
    [past] = (await call(w, "GET", "/emergencies")).json()
    assert (past["purged"], past["summary"]["total"]) == (True, 1)  # özet kalır
    tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (
        await call(w, "PUT", "/emergency-settings", tech, json=saved.json()["data"])
    ).status_code == 403


# --- K31 Anket gizliliği ve çoklu soru ------------------------------------------------------


def survey(**changes: Any) -> dict[str, Any]:
    return {
        "question": "Site memnuniyet anketi",
        "audience": "all",
        "ends_on": "2026-06-30",
        "questions": [
            {"text": "Havuz açılsın mı?", "kind": "single", "options": ["Evet", "Hayır"]},
            {"text": "Hangi günler?", "kind": "multiple", "options": ["Cmt", "Paz", "Pzt"]},
            {"text": "Temizliği puanlayın", "kind": "rating"},
            {"text": "Öneriniz", "kind": "text", "required": False},
        ],
        **changes,
    }


async def test_isimli_coklu_soru_anketi(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    bad = await call(
        w, "POST", "/polls",
        json=survey(questions=[{"text": "?", "kind": "single", "options": ["Tek"]},
                               {"text": "Bir soru", "kind": "zz"}]),
    )  # fmt: skip
    assert fields(bad) == {
        "questions.0.text": "Soruyu yazın (3–300 karakter).",
        "questions.0.options": "2 ile 8 arasında seçenek yazın.",
        "questions.1.kind": "Soru tipini seçin.",
    }
    created = await call(w, "POST", "/polls", json=survey(is_anonymous=False, one_per_unit=False))
    assert created.status_code == 201, created.text
    poll = created.json()["data"]
    assert "isimli" in created.json()["message"]
    assert "resmî oylama" in poll["notice"]
    q1, q2, q3, q4 = poll["questions"]
    missing = await call(
        w, "POST", f"/resident/polls/{poll['id']}/vote", sakin,
        json={"answers": [{"question_id": q1["id"], "option_ids": [q1["options"][0]["id"]]}]},
    )  # fmt: skip
    assert set(fields(missing)) == {f"answers.{q2['id']}", f"answers.{q3['id']}"}
    answers = [
        {"question_id": q1["id"], "option_ids": [q1["options"][0]["id"]]},
        {"question_id": q2["id"], "option_ids": [o["id"] for o in q2["options"][:2]]},
        {"question_id": q3["id"], "rating": 4},
        {"question_id": q4["id"], "text": "Akşam saatleri uzasın"},
    ]
    voted = await call(
        w, "POST", f"/resident/polls/{poll['id']}/vote", sakin, json={"answers": answers}
    )
    assert voted.status_code == 200, voted.text
    again = await call(
        w, "POST", f"/resident/polls/{poll['id']}/vote", sakin, json={"answers": answers}
    )
    assert again.json()["error"]["message"] == "Bu ankete zaten yanıt verdiniz."

    [mine] = (await call(w, "GET", "/resident/polls", sakin)).json()
    assert (mine["responded"], mine["can_respond"], mine["my_votes"]) == (True, False, [])
    assert mine["questions"][3]["texts"] is None  # metin yanıtları sakine dönmez
    [staff_view] = (await call(w, "GET", "/polls")).json()["items"]
    _, sq2, sq3, sq4 = staff_view["questions"]
    assert [o["votes"] for o in sq2["options"]] == [1, 1, 0]
    assert sq3["rating"] == {"average": "4.00", "counts": [0, 0, 0, 1, 0]}
    assert sq4["texts"] == ["Akşam saatleri uzasın"]
    [row] = (await call(w, "GET", f"/polls/{poll['id']}/responses")).json()
    assert (row["person_name"], row["unit_name"], len(row["answers"])) == ("Ayşe YILMAZ", None, 4)
    tech = await staff(w, "teknik@test.local", "Teknik Personel")
    assert (await call(w, "GET", f"/polls/{poll['id']}/responses", tech)).status_code == 403
    [tech_view] = (await call(w, "GET", "/polls", tech)).json()["items"]
    assert tech_view["questions"][3]["texts"] is None  # yalnız anket yöneticisi


async def test_anonim_anket_kisi_tutmaz(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    poll = (
        await call(w, "POST", "/polls", json=survey(one_per_unit=False, questions=None,
                                                    options=["Evet", "Hayır"]))
    ).json()["data"]  # fmt: skip
    assert (poll["is_anonymous"], poll["one_per_unit"]) == (True, False)
    yes = poll["options"][0]["id"]
    voted = await call(
        w, "POST", f"/resident/polls/{poll['id']}/vote", sakin, json={"option_id": yes}
    )
    assert voted.json() == {"data": {"option_id": yes}, "message": "Oyunuz kaydedildi."}
    again = await call(
        w, "POST", f"/resident/polls/{poll['id']}/vote", sakin, json={"option_id": yes}
    )
    assert again.status_code == 409
    with site_scope(w.site_id):
        async with w.factory() as session:
            [ballot] = list(await session.scalars(select(PollBallot)))
            assert (ballot.person_id, ballot.unit_id) == (None, None)
            assert len(list(await session.scalars(select(PollParticipant)))) == 1
    named = await call(w, "GET", f"/polls/{poll['id']}/responses")
    assert (named.status_code, named.json()["error"]["code"]) == (409, "anonymous_poll")
    # Başka bloğa hedeflenen anket bu sakine açık değil
    other_block = (await call(w, "POST", "/blocks", json={"name": "B"})).json()["data"]["id"]
    targeted = (
        await call(w, "POST", "/polls", json=survey(block_ids=[other_block], questions=None,
                                                    options=["Evet", "Hayır"]))
    ).json()["data"]  # fmt: skip
    denied = await call(
        w, "POST", f"/resident/polls/{targeted['id']}/vote", sakin,
        json={"unit_id": w.unit_id, "option_id": targeted["options"][0]["id"]},
    )  # fmt: skip
    assert (denied.status_code, denied.json()["error"]["code"]) == (403, "not_eligible")


# --- K32 Şeffaflık raporu ------------------------------------------------------------------


async def expense(w: World, amount: str) -> None:
    response = await w.api.post(
        w.url("/expenses"),
        data={"expense_category_id": w.lookups["category"], "description": "Asansör bakımı",
              "amount": amount, "date": "2026-06-10", "vendor": "Gizli Asansör Ltd."},
        headers=w.headers,
    )  # fmt: skip
    assert response.status_code == 201, response.text


async def test_seffaflik_raporu_dogrudan_yayin(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    await expense(w, "500.00")
    future = await call(w, "POST", "/transparency-reports", json={"year": 2026, "month": 7})
    assert fields(future) == {"month": "Gelecek bir dönem için rapor hazırlanamaz."}
    accounting = await staff(w, "muhasebe@test.local", "Muhasebe")
    created = await call(
        w, "POST", "/transparency-reports", accounting,
        json={"year": 2026, "month": 6, "note": "Haziran özeti",
              "scope": {"include_categories": True, "include_collection": True,
                        "include_cash_balance": True}},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    report = created.json()["data"]
    assert report["status"] == "draft"
    content = report["content"]
    assert (content["period"], content["expense"], content["income"]) == (
        "06/2026",
        "500.00",
        "0.00",
    )
    assert content["categories"][0]["name"] == "İşletme Giderleri"
    assert "Gizli Asansör" not in created.text
    assert "Asansör bakımı" not in created.text
    dup = await call(w, "POST", "/transparency-reports", json={"year": 2026, "month": 6})
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "exists")

    assert (await call(w, "GET", "/resident/transparency-reports", sakin)).json()["total"] == 0
    denied = await call(w, "POST", f"/transparency-reports/{report['id']}/publish", accounting)
    assert denied.status_code == 403  # kural yok → yönetici yayınlar
    published = await call(w, "POST", f"/transparency-reports/{report['id']}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["data"]["status"] == "published"
    [mine] = (await call(w, "GET", "/resident/transparency-reports", sakin)).json()["items"]
    assert (mine["period"], mine["content"]["expense"]) == ("06/2026", "500.00")
    assert (
        await call(w, "GET", f"/resident/transparency-reports/{mine['id']}", sakin)
    ).status_code == 200
    assert (await call(w, "GET", "/transparency-reports", sakin)).status_code == 403
    again = await call(w, "POST", f"/transparency-reports/{report['id']}/publish")
    assert again.status_code == 409
    gone = await call(w, "DELETE", f"/transparency-reports/{report['id']}")
    assert gone.status_code == 409  # yayınlanmış rapor silinmez
    other = await w.api.get(
        w.url(f"/transparency-reports/{report['id']}", "yildiz-sitesi"), headers=w.headers
    )
    assert other.status_code == 404


async def test_seffaflik_raporu_onay_kuralindan_gecer(comm: World) -> None:
    w = comm
    sakin = await ayse(w)
    board = await staff(w, "yk@test.local", "Yönetim Kurulu Üyesi")
    rule = await call(
        w, "POST", "/approval-rules",
        json={"action_type": "transparency_report", "effective_from": "2026-06-20",
              "remind_after_days": 2, "escalate_after_days": 5,
              "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}]},
    )  # fmt: skip
    assert rule.status_code in (200, 201), rule.text
    may = (await call(w, "POST", "/transparency-reports", json={"year": 2026, "month": 5})).json()
    sent = await call(w, "POST", f"/transparency-reports/{may['data']['id']}/publish")
    assert sent.status_code == 202, sent.text
    request_id = sent.json()["data"]["approval_request_id"]
    assert sent.json()["data"]["report"]["status"] == "pending"
    rejected = await call(
        w, "POST", f"/approvals/{request_id}/reject", board, json={"comment": "Not eksik"}
    )
    assert rejected.status_code == 200, rejected.text
    after = (await call(w, "GET", f"/transparency-reports/{may['data']['id']}")).json()
    assert (after["status"], after["rejection_reason"]) == ("rejected", "Not eksik")

    redo = (await call(w, "POST", "/transparency-reports", json={"year": 2026, "month": 5})).json()
    request_id = (
        await call(w, "POST", f"/transparency-reports/{redo['data']['id']}/publish")
    ).json()["data"]["approval_request_id"]
    approved = await call(w, "POST", f"/approvals/{request_id}/approve", board, json={})
    assert approved.status_code == 200, approved.text
    final = (await call(w, "GET", f"/transparency-reports/{redo['data']['id']}")).json()
    assert (final["status"], final["published_by"]) == ("published", "Kerem YILDIRIM")
    items = (await call(w, "GET", "/resident/transparency-reports", sakin)).json()["items"]
    assert [i["period"] for i in items] == ["05/2026"]
    assert date.fromisoformat(items[0]["published_at"][:10]) == NOW.date()
