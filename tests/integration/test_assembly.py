"""Genel kurul — servis istekleri 75–77 (Yazılım Dokümanı TOP-01, TOP-03, TOP-04), gerçek PG.

Kurgu `finance_world` + A blokta 6 bölüm, 5 kişi (arsa payı toplamı 10/10):
A-1 Ayşe (2) · A-2 Ali (2) · A-3 Ali (1, ikinci bölümü) · A-4 Can + Deniz hisseli (2) ·
A-5 Ece (1) · A-6 Fatma (2).
"""

import uuid
from typing import Any

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Announcement, Assembly, AuditLog, Plan, Site
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

MODULES = ["finance", "general-assembly"]
MEETING = {
    "kind": "general_ordinary",
    "title": "2027 olağan genel kurulu",
    "scheduled_at": "2027-01-15T11:00:00Z",
    "location": "Sosyal tesis salonu",
    "agenda": ["Divan seçimi", "Çatı yalıtımı (4/5)", "Dilek ve temenniler"],
}


@pytest.fixture
async def gk(world: World) -> World:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Geniş", max_units=150, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    assert (await world.post("/modules/general-assembly/toggle")).status_code == 200
    block = world.lookups["block"]
    patched = await world.api.patch(
        world.url(f"/units/{world.unit_id}"),
        json={"land_share_numerator": 2, "land_share_denominator": 10},
        headers=world.headers,
    )
    assert patched.status_code == 200, patched.text
    ali = can = None
    for number, share, owners in (
        ("2", 2, [("Ali", "Kaya")]),
        ("3", 1, ["ali"]),
        ("4", 2, [("Can", "Demir"), ("Deniz", "Ak")]),
        ("5", 1, [("Ece", "Tan")]),
        ("6", 2, [("Fatma", "Er")]),
    ):
        unit = await world.post(
            "/units",
            {
                "block_id": block,
                "number": number,
                "land_share_numerator": share,
                "land_share_denominator": 10,
            },
        )
        assert unit.status_code == 201, unit.text
        uid = unit.json()["data"]["id"]
        world.lookups[f"A-{number}"] = uid
        for owner in owners:
            body: dict[str, Any] = {"role": "owner", "start_date": "2020-01-01"}
            if owner == "ali":
                body["person_id"] = ali
            elif isinstance(owner, tuple):
                first, last = owner
                body["person"] = {"first_name": first, "last_name": last}
            if len(owners) > 1:
                body["share_percent"] = "50"
            party = await world.post(f"/units/{uid}/parties", body)
            assert party.status_code == 201, party.text
            person = party.json()["data"]["party"]["person"]["id"]
            if owner == ("Ali", "Kaya"):
                ali = person
            if owner == ("Can", "Demir"):
                can = person
    assert ali
    assert can
    world.lookups["A-1"] = world.unit_id
    return world


async def put(w: World, path: str, body: object, headers: dict[str, str] | None = None) -> Any:
    return await w.api.put(w.url(path), json=body, headers=headers or w.headers)


def code(r: httpx2.Response) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def fields(r: httpx2.Response) -> dict[str, str]:
    assert r.status_code == 422, r.text
    return dict(r.json()["error"]["fields"])


async def meeting(w: World, **overrides: object) -> dict[str, Any]:
    created = await w.post("/meetings", {**MEETING, **overrides})
    assert created.status_code == 201, created.text
    data: dict[str, Any] = created.json()["data"]
    return data


async def staff(w: World, email: str, role: str) -> dict[str, str]:
    user = await create_user(w.factory, email)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return await login_headers(w.api, email)


async def test_genel_kurul_hazirliktan_karar_defterine(gk: World) -> None:
    w = gk
    m = await meeting(w)
    base = f"/meetings/{m['id']}/assembly"
    items = [i["id"] for i in m["agenda"]]
    u = w.lookups

    # Okuma hiçbir şey yazmaz; malik listesi canlı hesaplanır
    got = await w.get(base)
    assert got.status_code == 200, got.text
    a = got.json()
    assert (a["format"], a["legal_ack"], a["invitation"], a["roster_frozen"]) == (
        "physical",
        None,
        None,
        False,
    )
    assert [r["unit_name"] for r in a["roster"]] == ["A-1", "A-2", "A-3", "A-4", "A-5", "A-6"]
    keys = {r["unit_name"]: r["key"] for r in a["roster"]}
    assert keys["A-2"] == keys["A-3"]
    assert keys["A-1"] != keys["A-2"]
    assert next(r for r in a["roster"] if r["unit_name"] == "A-4")["owners"] == [
        "Can DEMİR",
        "Deniz AK",
    ]
    assert (a["quorum"]["persons_total"], a["quorum"]["ok"]) == (5, False)
    assert a["item_types"] == dict.fromkeys(items, "ordinary")
    assert a["settings"]["proxy_limit"] == 2
    with site_scope(w.site_id):
        async with w.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Assembly)) == 0

    # 75 — hazırlık
    assert set(fields(await put(w, f"{base}/prep", {"format": "online"}))) == {"format"}
    assert fields(await put(w, f"{base}/prep", {"item_types": {items[0]: "yok"}})) == {
        f"item_types.{items[0]}": "Karar türü seçin."
    }
    prep = await put(
        w, f"{base}/prep", {"format": "hybrid", "item_types": {items[1]: "four_fifths"}}
    )
    assert prep.status_code == 200, prep.text
    assert prep.json()["message"] == "Toplantı hazırlığı kaydedildi."
    assert prep.json()["data"]["item_types"][items[1]] == "four_fifths"
    invitation = {
        "title": "Olağan genel kurul çağrısı",
        "body": "Sayın malik, 15.01.2027 ...",
        "ack_hours": 72,
    }
    assert code(await w.post(f"{base}/invitation", invitation)) == (409, "legal_ack_required")
    ack = await w.post(f"{base}/legal-ack")
    assert ack.json()["data"]["legal_ack"]["by"] == "Kerem YILDIRIM"
    assert set(fields(await w.post(f"{base}/invitation", {**invitation, "body": "kısa"}))) == {
        "body"
    }
    assert set(fields(await w.post(f"{base}/invitation", {**invitation, "ack_hours": 0}))) == {
        "ack_hours"
    }
    sent = await w.post(f"{base}/invitation", invitation)
    assert sent.status_code == 200, sent.text
    assert sent.json()["message"].startswith("Çağrı 6 maliğe okuma onaylı duyuru olarak gönderildi")
    inv = sent.json()["data"]["invitation"]
    assert (inv["recipients"], inv["read"], inv["ack_hours"], inv["counts_as_proof"]) == (
        6,
        0,
        72,
        True,
    )
    assert sent.json()["data"]["notice"]["ok"] is True
    assert code(await w.post(f"{base}/invitation", invitation)) == (409, "already_sent")
    proof = (await w.get(f"{base}/invitation-proof")).json()
    assert proof["total"] == 6
    ali_row = next(r for r in proof["items"] if r["name"] == "Ali KAYA")
    assert (ali_row["units"], ali_row["channels"]) == (["A-2", "A-3"], ["in_app", "email", "sms"])
    with site_scope(w.site_id):
        async with w.factory() as session:
            sent_row = await session.get(Announcement, uuid.UUID(inv["announcement_id"]))
            assert sent_row is not None
            assert (sent_row.audience, sent_row.is_pinned, str(sent_row.expires_on)) == (
                "owners_only",
                True,
                "2027-01-15",
            )
    # format değişince hukuki uyarı onayı sıfırlanır
    back = await put(w, f"{base}/prep", {"format": "physical"})
    assert back.json()["data"]["legal_ack"] is None

    # 76 — hazirun ve vekâlet
    assert code(await w.post(f"{base}/items/{items[0]}/open")) == (409, "no_quorum")
    att = f"{base}/attendance"
    assert (await put(w, att, {"unit_id": u["A-1"], "mode": "self"})).status_code == 200
    assert fields(await put(w, att, {"unit_id": u["A-2"], "mode": "proxy", "proxy_name": "x"})) == {
        "proxy_name": "Vekilin adını ve soyadını yazın."
    }
    for unit, name in (("A-2", "Mehmet İlhan"), ("A-3", "mehmet  ilhan"), ("A-5", "MEHMET İLHAN")):
        r = await put(w, att, {"unit_id": u[unit], "mode": "proxy", "proxy_name": name})
        assert r.status_code == 200, r.text
    over = await put(w, att, {"unit_id": u["A-6"], "mode": "proxy", "proxy_name": "Mehmet İlhan"})
    assert code(over) == (409, "proxy_limit")
    assert over.json()["error"]["message"] == (
        "Vekâlet sınırı aşılıyor: bir kişi en fazla 2 malikin vekili olabilir."
    )
    missing = await put(w, att, {"unit_id": str(uuid.uuid4()), "mode": "self"})
    assert missing.status_code == 404
    floor = (await w.get(base)).json()
    assert floor["roster_frozen"] is True
    q = floor["quorum"]
    assert (q["persons_present"], q["persons_total"], q["share_present"], q["ok"]) == (
        3,
        5,
        "0.600000",
        True,
    )
    assert code(await put(w, f"{base}/prep", {"format": "hybrid"})) == (409, "started")

    # oylama: tek açık oylama, hazirun kilitli
    opened = await w.post(f"{base}/items/{items[0]}/open")
    assert opened.status_code == 200, opened.text
    assert code(await w.post(f"{base}/items/{items[1]}/open")) == (409, "voting_open")
    assert code(await put(w, att, {"unit_id": u["A-6"], "mode": "self"})) == (409, "voting_open")
    assert code(await w.post(f"{base}/items/{items[0]}/open")) == (409, "invalid_status")
    ballots = f"{base}/items/{items[0]}/ballots"
    assert fields(await put(w, ballots, {"ballots": {u["A-4"]: "for"}})) == {
        f"ballots.{u['A-4']}": "Hazirunda olmayan bölüm oy kullanamaz."
    }
    clash = await put(w, ballots, {"ballots": {u["A-2"]: "for", u["A-3"]: "against"}})
    assert code(clash) == (422, "owner_conflict")
    assert clash.json()["error"]["message"] == "Ali KAYA birden çok bölümünde farklı oy kullanamaz."
    entered = await put(
        w, ballots, {"ballots": {u["A-1"]: "for", u["A-2"]: "for", u["A-3"]: "for"}}
    )
    live = entered.json()["data"]["votes"][items[0]]["live"]
    assert (live["persons"]["for"], live["persons"]["base"]) == (2, 3)
    shut = await w.post(f"{base}/items/{items[0]}/close")
    assert code(shut) == (422, "missing_ballots")
    assert shut.json()["error"]["message"] == "1 katılımcının oyu girilmedi."
    await put(w, ballots, {"ballots": {u["A-5"]: "abstain"}})
    await put(w, ballots, {"ballots": {u["A-5"]: "against"}})  # düzeltme yeni olay olarak
    closed = await w.post(f"{base}/items/{items[0]}/close")
    assert closed.json()["message"] == "Oylama kapandı: KABUL."
    result = closed.json()["data"]["votes"][items[0]]["result"]
    assert result["persons"] == {"for": 2, "against": 1, "abstain": 0, "base": 3}
    assert result["shares"] == {
        "for": "0.500000",
        "against": "0.100000",
        "abstain": "0.000000",
        "base": "0.600000",
    }
    assert (result["person_ok"], result["share_ok"], result["accepted"]) == (True, True, True)

    # 4/5 (tüm malikler): katılanların hepsi kabul etse de 3/5 kişi, 6/10 pay → RET
    await w.post(f"{base}/items/{items[1]}/open")
    everyone = {u[k]: "for" for k in ("A-1", "A-2", "A-3", "A-5")}
    await put(w, f"{base}/items/{items[1]}/ballots", {"ballots": everyone})
    rejected = await w.post(f"{base}/items/{items[1]}/close")
    assert rejected.json()["message"] == "Oylama kapandı: RET."
    r2 = rejected.json()["data"]["votes"][items[1]]["result"]
    assert (r2["type_code"], r2["persons"]["base"], r2["person_ok"], r2["share_ok"]) == (
        "four_fifths",
        5,
        False,
        False,
    )

    # 77 — tutanak
    finalize = f"{base}/minutes/finalize"
    early = await w.post(finalize)
    assert code(early) == (422, "items_open")
    assert early.json()["error"]["message"] == "Oylaması kapanmamış madde: 3. Dilek ve temenniler."
    await w.post(f"{base}/items/{items[2]}/open")
    await put(w, f"{base}/items/{items[2]}/ballots", {"ballots": everyone})
    await w.post(f"{base}/items/{items[2]}/close")
    assert set(fields(await put(w, f"{base}/minutes", {"notes": {"x": "y"}}))) == {"notes.x"}
    draft = await put(
        w,
        f"{base}/minutes",
        {
            "notes": {items[0]: "Divan oybirliğine yakın seçildi."},
            "decisions": {items[0]: "Divan başkanlığına Ayşe Yılmaz seçildi."},
            "opening": "Toplantı 14:05'te açıldı.",
        },
    )
    assert draft.json()["data"]["minutes"]["opening"] == "Toplantı 14:05'te açıldı."
    assert code(await w.post(f"{base}/minutes/signed", files={"file": ("t.pdf", b"%PDF-1.4")})) == (
        409,
        "not_finalized",
    )
    done = await w.post(finalize)
    assert done.status_code == 200, done.text
    assert done.json()["message"] == "Tutanak kesinleşti; 2 karar karar defterine işlendi (no 1–2)."
    minutes = done.json()["data"]["minutes"]
    assert minutes["finalized_by"] == "Kerem YILDIRIM"
    assert minutes["attendance_sentence"] == (
        "5 malikten 3'i (arsa payının %60'i) katıldı; 3 bölüm vekille temsil edildi. "
        "Toplantı nisabı sağlandı."
    )
    assert minutes["summary_draft"]["title"] == "2027 olağan genel kurulu: karar özeti"
    held = (await w.get(f"/meetings/{m['id']}")).json()
    assert held["status"] == "held"
    assert [(i["result"], i["votes_for"]) for i in held["agenda"]] == [
        ("accepted", 2),
        ("rejected", 3),
        ("accepted", 3),
    ]
    assert held["agenda"][0]["decision"] == "Divan başkanlığına Ayşe Yılmaz seçildi."
    assert code(await w.post(finalize)) == (409, "finalized")
    assert code(await put(w, f"{base}/minutes", {"opening": "x"})) == (409, "finalized")
    assert code(await put(w, att, {"unit_id": u["A-6"], "mode": "self"})) == (409, "finalized")

    book = (await w.get("/decision-book")).json()
    assert book["total"] == 2
    assert [(e["no"], e["item_title"], e["result"]) for e in book["items"]] == [
        (3 - 1, "Dilek ve temenniler", "accepted"),
        (1, "Divan seçimi", "accepted"),
    ]
    assert book["items"][1]["decision"] == "Divan başkanlığına Ayşe Yılmaz seçildi."
    assert book["items"][1]["date"] == "2027-01-15"

    # İmzalı tutanak belge arşivine
    signed = await w.api.post(
        w.url(f"{base}/minutes/signed"),
        files={"file": ("tutanak.pdf", b"%PDF-1.4 imzali", "application/pdf")},
        headers=w.headers,
    )
    assert signed.status_code == 200, signed.text
    doc = signed.json()["data"]["minutes"]["signed_document"]
    assert doc["title"] == "Toplantı 1 — 2027 olağan genel kurulu: İmzalı tutanak"
    linked = await w.get(
        "/subject-documents", params={"subject_type": "meetings", "subject_id": m["id"]}
    )
    assert [d["id"] for d in linked.json()] == [doc["id"]]

    # Değişmez kayıtlar: oy olayı, tutanak, karar defteri, malik listesi
    with site_scope(w.site_id):
        for table in (
            "assembly_ballots",
            "assembly_minutes",
            "decision_book_entries",
            "assembly_roster",
        ):
            async with w.factory() as session:
                with pytest.raises(DBAPIError, match="değiştirilemez"):
                    await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        async with w.factory() as session:
            events = await session.scalar(text("SELECT count(*) FROM assembly_ballots"))
            assert events == 5 + 4 + 4  # madde 1: 3 + 2 (düzeltme), madde 2 ve 3: 4 er
            entities = set(await session.scalars(select(AuditLog.entity)))
            assert {
                "assemblies",
                "assembly_attendance",
                "assembly_ballots",
                "decision_book_entries",
                "assembly_minutes",
            } <= entities

    # İkinci genel kurul: defter numarası sürer
    m2 = await meeting(
        w,
        title="2027 olağanüstü genel kurulu",
        kind="general_extraordinary",
        agenda=["Güvenlik firması"],
    )
    b2 = f"/meetings/{m2['id']}/assembly"
    for unit in ("A-1", "A-2", "A-5", "A-6"):
        await put(w, f"{b2}/attendance", {"unit_id": u[unit], "mode": "self"})
    item = m2["agenda"][0]["id"]
    assert (await w.post(f"{b2}/items/{item}/open")).status_code == 200
    await put(w, f"{b2}/items/{item}/ballots", {"ballots": {u[k]: "for" for k in ("A-1", "A-2")}})
    assert code(await w.post(f"{b2}/items/{item}/close")) == (422, "missing_ballots")
    await put(w, f"{b2}/items/{item}/ballots", {"ballots": {u["A-5"]: "for", u["A-6"]: "against"}})
    await w.post(f"{b2}/items/{item}/close")
    assert (await w.post(f"{b2}/minutes/finalize")).json()["message"] == (
        "Tutanak kesinleşti; 1 karar karar defterine işlendi (no 3)."
    )


async def test_ikinci_toplanti_ve_elle_karar_kilidi(gk: World) -> None:
    w = gk
    m = await meeting(w)
    base = f"/meetings/{m['id']}/assembly"
    item = m["agenda"][0]["id"]
    await put(w, f"{base}/attendance", {"unit_id": w.lookups["A-6"], "mode": "self"})
    # elle karar girişi hazirun başlayınca kapalı
    manual = await w.post(
        f"/meetings/{m['id']}/decisions",
        {"attendance_note": "x", "items": [{"id": i["id"], "result": "info"} for i in m["agenda"]]},
    )
    assert code(manual) == (409, "assembly_in_progress")
    blocked = await w.post(f"{base}/items/{item}/open")
    assert code(blocked) == (409, "no_quorum")
    assert blocked.json()["error"]["message"] == (
        "Toplantı nisabı sağlanamadı (kişi 1/5 ✗, arsa payı %20 ✗). "
        "İkinci toplantı kuralını uygulayın."
    )
    second = await put(w, f"{base}/prep", {"second_call": True})
    assert second.status_code == 200, second.text
    q = second.json()["data"]["quorum"]
    assert (q["required"], q["ok"], q["second_call"]) == (False, True, True)
    assert (await w.post(f"{base}/items/{item}/open")).status_code == 200
    assert code(await put(w, f"{base}/prep", {"second_call": False})) == (409, "started")
    # ikinci toplantıda da nisap aranacaksa (parametre) oylama açılmaz
    settings = await put(
        w,
        "/assembly-settings",
        {
            "second_call_quorum": {
                "num": 1,
                "den": 2,
                "strict": True,
                "person": True,
                "share": False,
            }
        },
    )
    assert settings.status_code == 200, settings.text
    assert (await w.get(base)).json()["quorum"]["ok"] is False


async def test_ayarlar_ve_hukuki_bayraklar(gk: World) -> None:
    w = gk
    got = (await w.get("/assembly-settings")).json()
    assert [t["code"] for t in got["decision_types"]] == [
        "ordinary",
        "all_majority",
        "four_fifths",
        "unanimous",
    ]
    assert got["quorum"] == {"num": 1, "den": 2, "strict": True, "person": True, "share": True}
    assert (got["second_call_quorum"], got["notice_days"], got["person_rule"]) == (
        None,
        15,
        "owner_group",
    )
    assert (got["remote_voting"], got["digital_proxy"], got["sms_verification_available"]) == (
        False,
        False,
        False,
    )
    assert fields(await put(w, "/assembly-settings", {"proxy_limit": 0})) == {
        "proxy_limit": "Kişi başı vekâlet 1–20."
    }
    saved = await put(w, "/assembly-settings", {"proxy_limit": 3, "notice_days": 30})
    assert saved.json()["message"] == "Genel kurul ayarları kaydedildi."
    assert (saved.json()["data"]["proxy_limit"], saved.json()["data"]["updated_by"]) == (
        3,
        "Kerem YILDIRIM",
    )
    assert set(fields(await put(w, "/assembly-settings", {"remote_voting": True}))) == {
        "legal_basis_document_id"
    }
    assert set(
        fields(await put(w, "/assembly-settings", {"legal_basis_document_id": str(uuid.uuid4())}))
    ) == {"legal_basis_document_id"}
    bad_quorum = await put(w, "/assembly-settings", {"quorum": {"num": 3, "den": 2}})
    assert set(fields(bad_quorum)) == {"quorum"}
    # kullanılan karar türü kaldırılamaz
    m = await meeting(w)
    await put(w, f"/meetings/{m['id']}/assembly/prep", {"item_types": {}})
    only_one = [
        {
            "code": "custom",
            "name": "Özel çoğunluk",
            "base": "present",
            "num": 2,
            "den": 3,
            "strict": False,
            "person": True,
            "share": True,
        }
    ]
    removed = await put(w, "/assembly-settings", {"decision_types": only_one})
    assert fields(removed)["decision_types"].startswith("Kesinleşmemiş bir genel kurulda")
    added = await put(
        w,
        "/assembly-settings",
        {"decision_types": [*got["decision_types"], only_one[0]]},
    )
    assert [t["code"] for t in added.json()["data"]["decision_types"]][-1] == "custom"


async def test_yetki_modul_ve_site_izolasyonu(gk: World) -> None:
    w = gk
    m = await meeting(w)
    base = f"/meetings/{m['id']}/assembly"
    board = await meeting(w, kind="board", title="Yönetim kurulu toplantısı")
    not_general = await w.get(f"/meetings/{board['id']}/assembly")
    assert code(not_general) == (409, "not_general")
    assert not_general.json()["error"]["message"] == (
        "Genel kurul işlemleri yalnız genel kurul toplantılarında."
    )
    auditor = await staff(w, "denetci@test.local", "Denetçi")
    assert (await w.api.get(w.url(base), headers=auditor)).status_code == 200
    assert (await w.api.get(w.url("/decision-book"), headers=auditor)).status_code == 200
    assert (await put(w, f"{base}/prep", {"second_call": True}, auditor)).status_code == 403
    assert (await put(w, "/assembly-settings", {"proxy_limit": 3}, auditor)).status_code == 403
    security = await staff(w, "guvenlik@test.local", "Güvenlik")
    assert (await w.api.get(w.url(base), headers=security)).status_code == 403

    # Başka sitenin kullanıcısı bu toplantıyı göremez; diğer sitede aynı kimlik 404
    other = "yildiz-sitesi"
    assert (await w.api.get(w.url(base, other), headers=w.headers)).status_code == 404
    assert (await w.api.get(w.url("/decision-book", other), headers=w.headers)).status_code == 404
    stranger = await create_user(w.factory, "yabanci@test.local")
    await add_site_membership(w.factory, w.other_site_id, stranger.id, "Yönetici")
    foreign = await login_headers(w.api, "yabanci@test.local")
    assert (await w.api.get(w.url(base), headers=foreign)).status_code == 404

    # Modül kapalı → 404
    assert (await w.post("/modules/general-assembly/toggle")).status_code == 200
    assert (await w.get(base)).status_code == 404
    assert (await w.get("/assembly-settings")).status_code == 404
