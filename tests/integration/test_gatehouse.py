"""Kapılar ve ayarlar, ön davet, habersiz ziyaretçi, plaka/ihlal, teslim yetkilisi, uzun bekleyen
kargo ve gece işi — servis istekleri 40–44 (gerçek PostgreSQL). Kurgu: `gatehouse_world`.
"""

import hashlib
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, Visitor, VisitorInvite
from site_yonetim.services import security_jobs
from tests.integration.gatehouse_world import TZ, GateWorld

SETTINGS = {
    "unannounced_method": "info",
    "approval_timeout_seconds": 90,
    "gate_methods": {},
    "parcel_method": "both",
    "visitor_fields": {"plate": True, "phone": False, "id_seen": False},
    "retention_months": 6,
    "auto_anonymize": False,
    "parcel_long_wait_days": 5,
    "guest_parking_capacity": 10,
}


async def _settings(gw: GateWorld, **changes: object) -> dict[str, object]:
    response = await gw.put("/settings/security", gw.manager, {**SETTINGS, **changes})
    assert response.status_code == 200, response.text
    data: dict[str, object] = response.json()["data"]
    return data


# --- 40: kapılar ve ayarlar ------------------------------------------------------------------


async def test_kapilar_ve_guvenlik_ayarlari(gw: GateWorld) -> None:
    got = await gw.get("/settings/security", gw.guard)
    assert got.status_code == 200, got.text
    defaults = got.json()
    assert (defaults["version"], defaults["retention_months"], defaults["auto_anonymize"]) == (
        0,
        6,
        False,
    )
    assert defaults["approval_timeout_seconds"] == 90

    created = await gw.post("/gates", gw.manager, {"name": " Ana  kapı ", "kind": "vehicle"})
    assert created.status_code == 201, created.text
    gate = created.json()["data"]
    assert (gate["name"], gate["block_ids"], gate["always_open"], gate["is_active"]) == (
        "Ana kapı",
        [],
        True,
        True,
    )
    dup = await gw.post("/gates", gw.manager, {"name": "ANA KAPI", "kind": "x"})
    assert dup.status_code == 422
    assert dup.json()["error"]["fields"] == {
        "name": "Bu adla kapı var.",
        "kind": "Kapı tipini seçin.",
    }
    assert dup.json()["error"]["message"] == "Formda düzeltilmesi gereken alanlar var."
    bad_block = await gw.post(
        "/gates",
        gw.manager,
        {"name": "Yan", "kind": "pedestrian", "block_ids": [str(uuid.uuid4())]},
    )
    assert bad_block.json()["error"]["fields"] == {"block_ids": "Seçilen blok bulunamadı."}
    side = await gw.post(
        "/gates",
        gw.manager,
        {"name": "Yan kapı", "kind": "pedestrian", "block_ids": [gw.w.lookups["block"]]},
    )
    assert side.json()["data"]["block_ids"] == [gw.w.lookups["block"]]
    off = await gw.patch(
        f"/gates/{gate['id']}",
        gw.manager,
        {"name": "Ana kapı", "kind": "vehicle", "is_active": False},
    )
    assert off.json()["data"]["is_active"] is False
    assert [g["name"] for g in (await gw.get("/gates", gw.guard)).json()] == [
        "Ana kapı",
        "Yan kapı",
    ]

    # yetki: görevli okur, yazamaz; sakin hiç göremez
    assert (await gw.post("/gates", gw.guard, {"name": "X", "kind": "vehicle"})).status_code == 403
    assert (await gw.put("/settings/security", gw.guard, SETTINGS)).status_code == 403
    assert (await gw.get("/gates", gw.resident)).status_code == 403

    too_long = await gw.put("/settings/security", gw.manager, {**SETTINGS, "retention_months": 25})
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "retention_limit"
    assert too_long.json()["error"]["message"] == "Saklama süresi en fazla 2 yıl (24 ay) olabilir."
    assert too_long.json()["error"]["fields"] == {
        "retention_months": "Saklama süresi en fazla 2 yıl (24 ay) olabilir."
    }
    bad = await gw.put(
        "/settings/security",
        gw.manager,
        {**SETTINGS, "approval_timeout_seconds": 20, "gate_methods": {str(uuid.uuid4()): "info"}},
    )
    assert set(bad.json()["error"]["fields"]) == {"approval_timeout_seconds", "gate_methods"}

    saved = await _settings(
        gw, unannounced_method="approval", gate_methods={side.json()["data"]["id"]: "info"}
    )
    assert (saved["version"], saved["updated_by"]) == (1, "Kerem YILDIRIM")
    assert (await _settings(gw))["version"] == 2

    # site izolasyonu: B'nin kapı listesi boş, A'nın kapısı B adresinden 404
    other = await gw.api.get(gw.url("/gates", "yildiz-sitesi"), headers=gw.manager)
    assert other.json() == []
    cross = await gw.api.patch(
        gw.url(f"/gates/{gate['id']}", "yildiz-sitesi"),
        headers=gw.manager,
        json={"name": "X", "kind": "vehicle"},
    )
    assert cross.status_code == 404

    # modül kapalıysa 404
    for key in ("visitors", "packages"):
        await gw.w.post(f"/modules/{key}/toggle")
    assert (await gw.get("/gates", gw.manager)).status_code == 404


async def test_kapali_ziyaretci_alani_kaydedilmez(gw: GateWorld) -> None:
    await _settings(gw, visitor_fields={"plate": False, "phone": True, "id_seen": False})
    visitor = await gw.post(
        "/visitors",
        gw.guard,
        {
            "unit_id": gw.w.unit_id,
            "full_name": "Can Er",
            "plate_number": "34 ABC 12",
            "phone": "555",
        },
    )
    data = visitor.json()["data"]
    assert (data["plate_number"], data["phone"]) == (None, "555")


# --- 41: ön davet ve kodla giriş ---------------------------------------------------------------


def _invite(**over: object) -> dict[str, object]:
    return {
        "visitor_name": "Test Deneme",
        "type": "once",
        "date_from": "2026-06-20",
        "plate": "34 msf 77",
        **over,
    }


async def test_on_davet_tek_kullanimlik_kodla_giris(gw: GateWorld) -> None:
    created = await gw.post("/resident/visitor-invites", gw.resident, _invite(note="Akşam yemeği"))
    assert created.status_code == 201, created.text
    invite = created.json()["data"]
    code = invite["code"]
    assert len(code) == 6 and code.isdigit()  # noqa: PT018
    assert code in created.json()["message"]
    assert (invite["unit_name"], invite["date_to"], invite["plate"], invite["status"]) == (
        "A-1",
        "2026-06-20",
        "34 MSF 77",
        "active",
    )
    assert invite["valid_now"] is True

    # kod saklanmaz: liste kodu döndürmez; veritabanında yalnız özet (düz SHA-256 de değil)
    [listed] = (await gw.get("/resident/visitor-invites", gw.resident)).json()
    assert listed["code"] is None
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            stored = await session.scalar(select(VisitorInvite.code_hash))
    assert stored is not None and code not in stored  # noqa: PT018
    assert stored != hashlib.sha256(code.encode()).hexdigest()

    expected = (await gw.get("/visitor-invites/expected", gw.guard)).json()
    assert expected == [
        {
            "id": invite["id"],
            "unit_name": "A-1",
            "visitor_name": "Test Deneme",
            "type": "once",
            "time_from": None,
            "time_to": None,
            "plate": "34 MSF 77",
        }
    ]

    missing = await gw.post("/visitor-invites/check-in", gw.guard, {"code": "12a"})
    assert missing.json()["error"]["fields"] == {"code": "6 haneli davet kodunu girin."}
    wrong = "000000" if code != "000000" else "000001"
    unknown = await gw.post("/visitor-invites/check-in", gw.guard, {"code": wrong})
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "invite_not_found"

    entered = await gw.post("/visitor-invites/check-in", gw.guard, {"code": code})
    assert entered.status_code == 201, entered.text
    assert entered.json()["data"]["visitor_name"] == "Test Deneme"
    assert entered.json()["message"].startswith("Test Deneme (A-1) içeri alındı")
    visitors = (await gw.get("/visitors", gw.guard)).json()["items"]
    assert [(v["full_name"], v["status"], v["plate_number"]) for v in visitors] == [
        ("Test Deneme", "entered", "34MSF77")
    ]
    assert "visitor.invite_used" in await gw.kinds(gw.resident)

    again = await gw.post("/visitor-invites/check-in", gw.guard, {"code": code})
    assert again.status_code == 409
    assert again.json()["error"] == {
        **again.json()["error"],
        "code": "invite_not_valid",
        "message": "Bu tek kullanımlık davet kullanılmış.",
    }
    # bugün kullanılan davet plakası gün boyu "misafir"
    plate = (await gw.get("/plates/lookup", gw.guard, params={"plate": "34msf77"})).json()
    assert (plate["result"], plate["unit_name"], plate["plate_display"]) == (
        "guest",
        "A-1",
        "34 MSF 77",
    )


async def test_sureli_kalici_davet_gun_ve_saatte_gecerli(gw: GateWorld) -> None:
    created = await gw.post(
        "/resident/visitor-invites",
        gw.resident,
        _invite(
            visitor_name="Bakıcı Hanım",
            type="recurring",
            date_to="2026-12-31",
            weekdays=[0, 1, 2, 3, 4],
            time_from="08:00",
            time_to="18:00",
            plate=None,
        ),
    )
    assert created.status_code == 201, created.text
    code = created.json()["data"]["code"]
    saturday = await gw.post("/visitor-invites/check-in", gw.guard, {"code": code})
    assert saturday.status_code == 409
    assert "geçerli değil" in saturday.json()["error"]["message"]
    assert (await gw.get("/visitor-invites/expected", gw.guard)).json() == []

    gw.at(9, day=date(2026, 6, 22))  # pazartesi
    assert len((await gw.get("/visitor-invites/expected", gw.guard)).json()) == 1
    assert (await gw.post("/visitor-invites/check-in", gw.guard, {"code": code})).status_code == 201
    # süreli kalıcı davet kullanılınca bitmez; saat dışında geçersiz
    gw.at(19, day=date(2026, 6, 22))
    evening = await gw.post("/visitor-invites/check-in", gw.guard, {"code": code})
    assert evening.status_code == 409


async def test_davet_dogrulama_park_kapasitesi_iptal_ve_yeni_kod(gw: GateWorld) -> None:
    bad = await gw.post(
        "/resident/visitor-invites",
        gw.resident,
        {
            "visitor_name": "X1",
            "type": "recurring",
            "date_from": "2026-06-19",
            "date_to": "2026-06-30",
            "weekdays": [],
            "time_from": "18:00",
            "time_to": "08:00",
            "plate": "ABC",
        },
    )
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {
        "visitor_name",
        "date_from",
        "weekdays",
        "time_to",
        "plate",
    }
    assert bad.json()["error"]["fields"]["plate"] == "Plakayı 34 ABC 123 biçiminde yazın."

    await _settings(gw, guest_parking_capacity=1)
    first = await gw.post(
        "/resident/visitor-invites",
        gw.resident,
        _invite(type="range", date_from="2026-06-21", date_to="2026-06-25"),
    )
    assert first.status_code == 201
    full = await gw.post(
        "/resident/visitor-invites",
        gw.neighbour,
        _invite(date_from="2026-06-22", plate="06 AB 123"),
    )
    assert full.status_code == 409
    assert full.json()["error"]["message"] == (
        "Misafir parkı bu tarihte dolu (1). Plakasız davet oluşturabilirsiniz."
    )
    no_plate = await gw.post(
        "/resident/visitor-invites", gw.neighbour, _invite(date_from="2026-06-22", plate=None)
    )
    assert no_plate.status_code == 201

    invite = first.json()["data"]
    # komşu Ayşe'nin davetini göremez / iptal edemez
    assert [i["id"] for i in (await gw.get("/resident/visitor-invites", gw.neighbour)).json()] == [
        no_plate.json()["data"]["id"]
    ]
    foreign = await gw.post(f"/resident/visitor-invites/{invite['id']}/cancel", gw.neighbour)
    assert foreign.status_code == 404

    gw.at(9, day=date(2026, 6, 22))
    old_code = invite["code"]
    renewed = await gw.post(f"/resident/visitor-invites/{invite['id']}/new-code", gw.resident)
    assert renewed.status_code == 200, renewed.text
    new_code = renewed.json()["data"]["code"]
    assert new_code != old_code
    old = await gw.post("/visitor-invites/check-in", gw.guard, {"code": old_code})
    assert old.status_code == 404
    assert (
        await gw.post("/visitor-invites/check-in", gw.guard, {"code": new_code})
    ).status_code == 201

    cancelled = await gw.post(f"/resident/visitor-invites/{invite['id']}/cancel", gw.resident)
    assert cancelled.json()["data"]["status"] == "cancelled"
    twice = await gw.post(f"/resident/visitor-invites/{invite['id']}/cancel", gw.resident)
    assert twice.status_code == 409
    after = await gw.post("/visitor-invites/check-in", gw.guard, {"code": new_code})
    assert after.status_code == 404

    # sakin uçları sakin içindir; görevli uçları sakine kapalı
    assert (await gw.get("/resident/visitor-invites", gw.guard)).status_code == 403
    assert (await gw.get("/visitor-invites/expected", gw.resident)).status_code == 403


async def test_kodla_giris_hiz_siniri(gw: GateWorld) -> None:
    statuses = [
        (await gw.post("/visitor-invites/check-in", gw.guard2, {"code": f"{n:06d}"})).status_code
        for n in range(31)
    ]
    assert statuses[:30] == [404] * 30
    assert statuses[30] == 429
    gw.later(minutes=6)
    assert (
        await gw.post("/visitor-invites/check-in", gw.guard2, {"code": "999999"})
    ).status_code == 404


# --- 42: habersiz ziyaretçi ------------------------------------------------------------------


async def test_bilgi_yontemi_hemen_iceri_alir(gw: GateWorld) -> None:
    created = await gw.post(
        "/visit-requests",
        gw.guard,
        {
            "unit_id": gw.w.unit_id,
            "full_name": "Ahmet Demir",
            "kind": "guest",
            "plate": "34 abc 123",
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert (data["method"], data["status"], data["plate"], data["timed_out"]) == (
        "info",
        "allowed",
        "34 ABC 123",
        False,
    )
    assert data["visitor_id"] is not None
    assert created.json()["message"] == "Ahmet Demir içeri alındı; A-1 sakinlerine bilgi verildi."
    assert "visitor.arrived" in await gw.kinds(gw.resident)
    assert "visitor.arrived" not in await gw.kinds(gw.neighbour)
    listed = (await gw.get("/visit-requests", gw.guard)).json()
    assert [r["full_name"] for r in listed] == ["Ahmet Demir"]
    assert (await gw.get("/resident/visit-approvals", gw.resident)).json() == []  # bilgi: onay yok
    bad = await gw.post(
        "/visit-requests", gw.guard, {"unit_id": str(uuid.uuid4()), "full_name": "A"}
    )
    assert bad.status_code == 422


async def test_sakin_onayi_zaman_asimi_ve_gorevli_karari(gw: GateWorld) -> None:
    gate = (await gw.post("/gates", gw.manager, {"name": "Servis", "kind": "service"})).json()[
        "data"
    ]
    await _settings(gw, unannounced_method="approval", gate_methods={gate["id"]: "info"})
    body = {"unit_id": gw.w.unit_id, "full_name": "Zeynep Kurt", "kind": "guest"}

    first = (await gw.post("/visit-requests", gw.guard, body)).json()["data"]
    assert (first["status"], first["seconds_left"], first["timed_out"]) == ("awaiting", 90, False)
    assert "visitor.approval_requested" in await gw.kinds(gw.resident)
    early = await gw.post(
        f"/visit-requests/{first['id']}/decide",
        gw.guard,
        {"decision": "allow", "reason": "Diafon onay"},
    )
    assert early.status_code == 409
    assert early.json()["error"]["code"] == "awaiting_resident"

    pending = (await gw.get("/resident/visit-approvals", gw.resident)).json()
    assert [(p["full_name"], p["status"]) for p in pending] == [("Zeynep Kurt", "awaiting")]
    foreign = await gw.post(
        f"/resident/visit-approvals/{first['id']}", gw.neighbour, {"decision": "allow"}
    )
    assert foreign.status_code == 404
    gw.later(seconds=30)
    allowed = await gw.post(
        f"/resident/visit-approvals/{first['id']}", gw.resident, {"decision": "allow"}
    )
    assert allowed.status_code == 200, allowed.text
    data = allowed.json()["data"]
    assert (data["status"], data["decision_source"], data["decided_by"]) == (
        "allowed",
        "resident",
        "Ayşe YILMAZ",
    )
    assert data["visitor_id"] is not None
    twice = await gw.post(
        f"/resident/visit-approvals/{first['id']}", gw.resident, {"decision": "deny"}
    )
    assert twice.json()["error"]["code"] == "already_decided"

    second = (await gw.post("/visit-requests", gw.guard, {**body, "full_name": "Kemal Su"})).json()[
        "data"
    ]
    gw.later(seconds=91)
    late = await gw.post(
        f"/resident/visit-approvals/{second['id']}", gw.resident, {"decision": "allow"}
    )
    assert late.status_code == 409
    assert late.json()["error"]["message"] == (
        "Süre doldu; görevli sizinle diafon/telefonla iletişime geçecek."
    )
    listed = {r["id"]: r for r in (await gw.get("/visit-requests", gw.guard)).json()}
    assert listed[second["id"]]["timed_out"] is True
    no_reason = await gw.post(
        f"/visit-requests/{second['id']}/decide", gw.guard, {"decision": "deny"}
    )
    assert no_reason.status_code == 422
    assert set(no_reason.json()["error"]["fields"]) == {"reason"}
    denied = await gw.post(
        f"/visit-requests/{second['id']}/decide",
        gw.guard,
        {"decision": "deny", "reason": "Diafonla ulaşıldı, sakin istemedi"},
    )
    assert denied.status_code == 200, denied.text
    assert (denied.json()["data"]["status"], denied.json()["data"]["decision_source"]) == (
        "denied",
        "guard",
    )
    assert denied.json()["data"]["visitor_id"] is None
    assert "visitor.denied" in await gw.kinds(gw.resident)

    # kapı istisnası: servis kapısında bilgi yöntemi
    via_gate = await gw.post(
        "/visit-requests", gw.guard, {**body, "full_name": "Usta Ali", "gate_id": gate["id"]}
    )
    assert via_gate.json()["data"]["status"] == "allowed"

    # her karar denetim kaydında — kişisel veri olmadan
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            details: list[dict[str, Any] | None] = list(
                await session.scalars(
                    select(AuditLog.after).where(AuditLog.entity == "visit_requests")
                )
            )
    assert sorted(d["decision_source"] for d in details if d) == ["guard", "resident"]
    assert all("Zeynep" not in str(d) and "Kemal" not in str(d) for d in details)

    # izolasyon: B sitesi talebi görmez
    other = await gw.api.get(gw.url("/visit-requests", "yildiz-sitesi"), headers=gw.manager)
    assert other.json() == []


# --- 43: plaka, giriş-çıkış, ihlal -----------------------------------------------------------


async def test_plaka_kaydi_ve_otopark_ihlali(gw: GateWorld) -> None:
    unknown = (await gw.get("/plates/lookup", gw.guard, params={"plate": "34 xyz 99"})).json()
    assert unknown == {
        "plate": "34XYZ99",
        "plate_display": "34 XYZ 99",
        "result": "unknown",
        "unit_name": None,
        "owner_name": None,
        "valid_until": None,
        "detail": None,
    }
    short = await gw.get("/plates/lookup", gw.guard, params={"plate": "34"})
    assert short.json()["error"]["fields"] == {"plate": "Plakayı 34 ABC 123 biçiminde yazın."}

    for direction in ("in", "out"):
        logged = await gw.post("/gate-logs", gw.guard, {"plate": "34xyz99", "direction": direction})
        assert logged.status_code == 201, logged.text
    assert logged.json()["message"] == "34 XYZ 99 çıkışı kaydedildi."
    logs = (await gw.get("/gate-logs", gw.guard)).json()
    assert logs["total"] == 2
    assert [(i["direction"], i["result"], i["by"]) for i in logs["items"]] == [
        ("out", "unknown", "Ali GÖREVLİ"),
        ("in", "unknown", "Ali GÖREVLİ"),
    ]
    assert (await gw.get("/gate-logs", gw.guard, params={"date": "2026-06-19"})).json()[
        "total"
    ] == 0

    missing = await gw.post("/parking-violations", gw.guard, {"plate": "ABC", "location": ""})
    assert set(missing.json()["error"]["fields"]) == {"plate", "location"}
    stranger = await gw.post(
        "/parking-violations", gw.guard, {"plate": "34 xyz 99", "location": "B blok önü"}
    )
    assert stranger.status_code == 201, stranger.text
    assert stranger.json()["data"]["notified"] == "management"
    assert "parking.violation" in await gw.kinds(gw.manager)

    # davetli (misafir) plaka yetkili: not zorunlu, bildirim o bölümün sakinine
    await gw.post(
        "/resident/visitor-invites",
        gw.resident,
        _invite(type="range", date_to="2026-06-22", plate="06 ab 123"),
    )
    authorized = await gw.post(
        "/parking-violations", gw.guard, {"plate": "06AB123", "location": "Engelli park yeri"}
    )
    assert authorized.status_code == 409
    assert authorized.json()["error"]["code"] == "plate_authorized"
    assert authorized.json()["error"]["message"].startswith("06 AB 123 yetkili (A-1 misafiri")
    noted = await gw.post(
        "/parking-violations",
        gw.guard,
        {"plate": "06AB123", "location": "Engelli park yeri", "note": "Yanlış yere park"},
    )
    assert noted.json()["data"]["notified"] == "resident"
    assert noted.json()["data"]["unit_name"] == "A-1"
    assert "parking.violation" in await gw.kinds(gw.resident)
    assert (await gw.get("/parking-violations", gw.manager)).json()["total"] == 2

    # yetki: sakin ve denetçi plaka sorgulayamaz; yönetim kurulu (units.read) okur ama yazamaz
    assert (
        await gw.get("/plates/lookup", gw.resident, params={"plate": "34XYZ99"})
    ).status_code == 403
    board = await gw.member("yk@test.local", "Yönetim Kurulu Üyesi", "Yk ÜYE")
    assert (await gw.get("/gate-logs", board)).status_code == 200
    assert (await gw.post("/gate-logs", board, {"plate": "34XYZ99"})).status_code == 403
    other = await gw.api.get(gw.url("/parking-violations", "yildiz-sitesi"), headers=gw.manager)
    assert other.json()["total"] == 0


# --- 44: teslim yetkilisi ve uzun bekleyen kargo ---------------------------------------------


async def test_teslim_yetkilisi_ve_uzun_bekleyen_kargo(gw: GateWorld) -> None:
    for name in ("Komşu Selin", "Ali Veli", "Ayşe Teyze"):
        added = await gw.post("/resident/parcel-delegates", gw.resident, {"name": name})
        assert added.status_code == 201, added.text
    fourth = await gw.post("/resident/parcel-delegates", gw.resident, {"name": "Dördüncü Kişi"})
    assert fourth.status_code == 409
    assert fourth.json()["error"]["message"] == "En fazla 3 teslim yetkilisi tanımlanabilir."
    bad = await gw.post("/resident/parcel-delegates", gw.neighbour, {"name": "1"})
    assert bad.json()["error"]["fields"] == {"name": "Yetkili kişinin adını yazın."}
    mine = (await gw.get("/resident/parcel-delegates", gw.resident)).json()
    assert (mine[0]["name"], mine[0]["unit_name"]) == ("Komşu Selin", "A-1")
    assert (await gw.get("/resident/parcel-delegates", gw.neighbour)).json() == []

    removed_id = mine[1]["id"]
    foreign = await gw.post(f"/resident/parcel-delegates/{removed_id}/remove", gw.neighbour)
    assert foreign.status_code == 404
    removed = await gw.post(f"/resident/parcel-delegates/{removed_id}/remove", gw.resident)
    assert removed.json()["data"]["is_active"] is False
    names = (await gw.get(f"/units/{gw.w.unit_id}/parcel-delegates", gw.guard)).json()
    assert [d["name"] for d in names] == ["Komşu Selin", "Ayşe Teyze"]
    assert set(names[0]) == {"id", "name"}

    package = await gw.post("/packages", gw.guard, {"unit_id": gw.w.unit_id, "carrier": "Aras"})
    assert package.status_code == 201
    assert (await gw.get("/packages/long-waiting", gw.guard)).json() == {"days": 5, "items": []}
    gw.later(days=6)
    waiting = (await gw.get("/packages/long-waiting", gw.guard)).json()
    assert waiting["days"] == 5
    [item] = waiting["items"]
    assert (item["unit_name"], item["carrier"], item["waiting_days"], item["delegates"]) == (
        "A-1",
        "Aras",
        6,
        ["Komşu Selin", "Ayşe Teyze"],
    )
    assert (await gw.get("/packages/long-waiting", gw.resident)).status_code == 403


# --- Gece işi: hatırlatma ve saklama --------------------------------------------------------


async def test_gece_isi_hatirlatma_ve_anonimlestirme_varsayilan_kapali(gw: GateWorld) -> None:
    await gw.post("/packages", gw.guard, {"unit_id": gw.w.unit_id, "carrier": "Yurtiçi"})
    await gw.post(
        "/visitors",
        gw.guard,
        {"unit_id": gw.w.unit_id, "full_name": "Eski Misafir", "plate_number": "34 ABC 12"},
    )
    await gw.post("/gate-logs", gw.guard, {"plate": "34ABC12"})

    later = datetime(2026, 8, 25, 3, 0, tzinfo=TZ)
    results = await security_jobs.run_daily(gw.w.factory, now=later)
    mine = next(r for r in results if r.site_id == gw.w.site_id)
    assert (mine.reminded, mine.anonymized) == (1, 0)  # anonimleştirme kapalı (K8)
    assert "package.long_waiting" in await gw.kinds(gw.resident)
    assert "package.long_waiting_report" in await gw.kinds(gw.manager)
    again = await security_jobs.run_daily(gw.w.factory, now=later)
    assert next(r for r in again if r.site_id == gw.w.site_id).reminded == 0  # günde bir

    await _settings(gw, auto_anonymize=True, retention_months=1)
    results = await security_jobs.run_daily(gw.w.factory, now=later)
    assert next(r for r in results if r.site_id == gw.w.site_id).anonymized == 2
    with site_scope(gw.w.site_id):
        async with gw.w.factory() as session:
            visitor = await session.scalar(select(Visitor))
    assert visitor is not None
    assert (visitor.full_name, visitor.plate_number, visitor.anonymized_at is not None) == (
        "Anonim",
        None,
        True,
    )
    logs = (await gw.get("/gate-logs", gw.guard, params={"date": "2026-06-20"})).json()["items"]
    assert (logs[0]["plate"], logs[0]["plate_display"]) == (None, "—")
