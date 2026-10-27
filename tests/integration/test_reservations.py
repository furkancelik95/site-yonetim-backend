"""Rezervasyon — REZ-01..03, servis istekleri 59–61, K11 (gerçek PostgreSQL, RLS'e tabi rol).

Alan kuralları, sakin ve sakin adına rezervasyon, çakışma (eşzamanlı istek), bekleme listesi ve
teklif süresi, onay (alan kuralı + onay mekanizması), ücret ve iptalde ters kayıt, kullanım
doğrulama, no-show kısıtı, borçlu bölüm kısıtı (K11), rapor; yetki, modül ve site izolasyonu.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim import cli
from site_yonetim.api.deps import get_now, get_today
from site_yonetim.core.config import get_settings
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.modules import ModuleKey
from site_yonetim.models import (
    HouseholdMember,
    LedgerAccount,
    LedgerEntry,
    Notification,
    Plan,
    Reservation,
    Site,
)
from site_yonetim.services import ledger
from site_yonetim.services import reservations as svc
from site_yonetim.services.charging import BUSINESS_TZ
from site_yonetim.services.provisioning import provision_site
from site_yonetim.services.sites import set_module_enabled
from tests.integration.conftest import DatabaseUrls
from tests.integration.helpers import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
MODULES = ["finance", "announcements", "requests", "reservations"]
# Perşembe 01.10.2026 08:00 (İstanbul)
START = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
TOMORROW = "2026-10-02"


@dataclass
class Rz:
    api: httpx2.AsyncClient
    app: FastAPI
    factory: Factory
    site_id: uuid.UUID
    other_site_id: uuid.UUID
    manager: dict[str, str]
    ids: dict[str, str] = field(default_factory=dict)
    now: list[datetime] = field(default_factory=lambda: [START])

    def url(self, path: str, slug: str = "aksu-konaklari") -> str:
        return f"/api/v1/sites/{slug}{path}"

    async def get(self, path: str, headers: dict[str, str] | None = None, **kw: Any) -> Any:
        return await self.api.get(self.url(path), headers=headers or self.manager, **kw)

    async def post(
        self, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return await self.api.post(self.url(path), json=body, headers=headers or self.manager)

    async def put(
        self, path: str, body: object, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return await self.api.put(self.url(path), json=body, headers=headers or self.manager)

    async def user(self, email: str, role: str, **fields: Any) -> dict[str, str]:
        kind = fields.pop("kind", None)
        user = await create_user(self.factory, email, **({"kind": kind} if kind else {}))
        await add_site_membership(self.factory, self.site_id, user.id, role, **fields)
        # Giriş gerçek saatle (jeton süresi gerçek saate göre doğrulanır).
        frozen = self.app.dependency_overrides.pop(get_now, None)
        try:
            return await login_headers(self.api, email)
        finally:
            if frozen is not None:
                self.app.dependency_overrides[get_now] = frozen

    async def resident(self, person: str, email: str) -> dict[str, str]:
        return await self.user(
            email, "Sakin", kind="resident", person_id=uuid.UUID(self.ids[person])
        )


async def _party(rz: Rz, unit: str, role: str, first: str, last: str) -> str:
    response = await rz.post(
        f"/units/{unit}/parties",
        {
            "role": role,
            "start_date": "2020-01-01",
            "person": {"first_name": first, "last_name": last},
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["party"]["person"]["id"])


@pytest.fixture
async def rz(
    api: httpx2.AsyncClient, api_app: FastAPI, session_factory: Factory, admin_engine: object
) -> Rz:
    async with session_factory() as session, session.begin():
        plan = Plan(name="Pro", max_units=30, allowed_modules=MODULES)
        session.add(plan)
        await session.flush()
        plan_id = plan.id
    site_ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            site = await provision_site(session, name=name, plan_id=plan_id)
            with site_scope(site.id):
                await set_module_enabled(session, site, ModuleKey.RESERVATIONS, enable=True)
            site_ids.append(site.id)
    manager = await create_user(session_factory, "yonetici@test.local", full_name="Kerem YILDIRIM")
    for site_id in site_ids:
        await add_site_membership(session_factory, site_id, manager.id, "Yönetici")
    o = Rz(
        api,
        api_app,
        session_factory,
        site_ids[0],
        site_ids[1],
        await login_headers(api, "yonetici@test.local"),
    )
    api_app.dependency_overrides[get_now] = lambda: o.now[0]
    api_app.dependency_overrides[get_today] = lambda: o.now[0].astimezone(BUSINESS_TZ).date()
    for name in ("A", "B"):
        o.ids[f"block_{name}"] = (await o.post("/blocks", {"name": name})).json()["data"]["id"]
        unit = await o.post("/units", {"block_id": o.ids[f"block_{name}"], "number": "1"})
        o.ids[f"unit_{name}"] = unit.json()["data"]["id"]
    o.ids["ayse"] = await _party(o, o.ids["unit_A"], "owner", "Ayşe", "Yılmaz")
    o.ids["elif"] = await _party(o, o.ids["unit_A"], "tenant", "Elif", "Demir")
    o.ids["mehmet"] = await _party(o, o.ids["unit_B"], "owner", "Mehmet", "Kaya")
    for key, name, kind, reservable in (
        ("gym", "Spor salonu", "gym", True),
        ("hall", "Toplantı salonu", "hall", True),
        ("pool", "Havuz", "pool", False),
    ):
        created = await o.post(
            "/common-areas", {"name": name, "kind": kind, "is_reservable": reservable}
        )
        assert created.status_code == 201, created.text
        o.ids[key] = created.json()["data"]["id"]
    return o


def rules(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "enabled": True,
        "open_from": "09:00",
        "open_to": "22:00",
        "slot_minutes": 60,
        "capacity": 1,
        "max_people": 10,
        "max_guests": 5,
        "weekly_limit": 2,
        "cancel_hours": 24,
        "late_cancel_refund": False,
        "fee_type": "free",
        "fee_amount": "0.00",
        "channel": "both",
        "approval": "auto",
        "verify": "code",
        "waitlist": True,
        "waitlist_minutes": 30,
        "noshow": {"enabled": False, "months": 3, "count": 2, "block_days": 14},
    }
    body.update(over)
    return body


async def book(rz: Rz, headers: dict[str, str], area: str = "gym", **body: Any) -> httpx2.Response:
    payload: dict[str, Any] = {
        "area_id": rz.ids[area],
        "date": TOMORROW,
        "start": "10:00",
        "people": 2,
        "guests": 0,
    }
    payload.update(body)
    return await rz.post("/resident/reservations", payload, headers)


async def entries(rz: Rz) -> list[LedgerEntry]:
    with site_scope(rz.site_id):
        async with rz.factory() as session:
            rows = await session.scalars(
                select(LedgerEntry)
                .where(LedgerEntry.source == "reservation")
                .order_by(LedgerEntry.created_at)
            )
            return list(rows)


async def notifications(rz: Rz, kind: str) -> list[Notification]:
    with site_scope(rz.site_id):
        async with rz.factory() as session:
            return list(
                await session.scalars(select(Notification).where(Notification.kind == kind))
            )


async def run_jobs(rz: Rz, now: datetime) -> svc.JobResult:
    with site_scope(rz.site_id):
        async with rz.factory() as session, session.begin():
            return await svc.run_jobs(session, now=now)


# --- 59 Alanlar ve kurallar -------------------------------------------------------------------


async def test_alanlar_varsayilan_kural_ve_dogrulama(rz: Rz) -> None:
    listed = (await rz.get("/reservable-areas")).json()
    assert [a["name"] for a in listed] == ["Spor salonu", "Toplantı salonu"]  # havuz işaretsiz
    assert listed[0]["rules"] == rules()  # kaydedilmemiş alan varsayılanla döner
    bad = await rz.put(
        f"/reservable-areas/{rz.ids['gym']}/rules",
        rules(
            open_to="08:00",
            capacity=0,
            fee_type="fixed",
            fee_amount=None,
            waitlist_minutes=1,
            verify="none",
            noshow={"enabled": True, "months": 3, "count": 2, "block_days": 14},
        ),
    )
    assert bad.status_code == 422
    assert bad.json()["error"]["fields"] == {
        "open_to": "Kapanış açılıştan sonra olmalı (SS:DD).",
        "capacity": "Kapasite 1–200.",
        "fee_amount": "Ücreti girin (sıfırdan büyük, en çok iki ondalık).",
        "waitlist_minutes": "Teklif süresi 5–1440 dakika.",
        "noshow": "No-show kuralı için kullanım doğrulaması gerekir.",
    }
    tight = await rz.put(f"/reservable-areas/{rz.ids['gym']}/rules", rules(open_to="09:30"))
    assert "slot_minutes" in tight.json()["error"]["fields"]
    ok = await rz.put(
        f"/reservable-areas/{rz.ids['hall']}/rules",
        rules(fee_type="hourly", fee_amount="200.00", slot_minutes=90, enabled=False),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["message"] == "Toplantı salonu kuralları kaydedildi. Alan rezervasyona kapalı."
    assert ok.json()["data"]["rules"]["fee_amount"] == "200.00"
    missing = await rz.put(f"/reservable-areas/{rz.ids['pool']}/rules", rules())
    assert missing.status_code == 404
    assert "Ortak alanlar" in missing.json()["error"]["message"]
    # Sakin kapalı alanı görmez.
    sakin = await rz.resident("elif", "elif@test.local")
    seen = (await rz.get("/resident/reservable-areas", sakin)).json()
    assert [a["name"] for a in seen] == ["Spor salonu"]


# --- 60 Rezervasyon, çakışma, bekleme listesi -------------------------------------------------


async def test_sakin_rezervasyonu_ucret_iptal_ve_ters_kayit(rz: Rz) -> None:
    await rz.put(
        f"/reservable-areas/{rz.ids['hall']}/rules",
        rules(fee_type="per_guest", fee_amount="50.00", channel="both"),
    )
    elif_ = await rz.resident("elif", "elif@test.local")
    created = await book(rz, elif_, area="hall", guests=3)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["status"] == "approved"
    r = data["reservation"]
    assert (r["unit_name"], r["start"], r["end"], r["fee"], r["channel"]) == (
        "A-1",
        "10:00",
        "11:00",
        "150.00",
        "resident",
    )
    assert "code" not in r  # personel çıktısında giriş kodu yok
    assert created.json()["message"] == "Rezervasyon onaylandı. Ücret 150,00 TL borca yansıdı."
    [charge] = await entries(rz)
    assert (charge.debit, charge.description) == (
        Decimal("150.00"),
        "Rezervasyon — Toplantı salonu 02.10.2026 10:00–11:00",
    )
    with site_scope(rz.site_id):
        async with rz.factory() as session:
            account = await session.get(LedgerAccount, charge.account_id)
            assert account is not None
            assert account.kind == "occupant"  # kiracı/oturan hesabı
    mine = (await rz.get("/resident/reservations", elif_)).json()
    [own] = mine["reservations"]
    assert len(own["code"]) == 6
    assert (own["verify"], own["cancel_hours"]) == ("code", 24)
    slots = (
        await rz.get(
            f"/resident/reservable-areas/{rz.ids['hall']}/availability",
            elif_,
            params={"date": TOMORROW},
        )
    ).json()
    ten = next(s for s in slots["slots"] if s["start"] == "10:00")
    assert (ten["taken"], ten["mine"]) == (1, True)
    assert (slots["blocked_until"], slots["debt_block"]) == (None, None)
    # Son iptal süresinden önce: ücret ters kayıtla kapanır.
    cancelled = await rz.post(f"/resident/reservations/{r['id']}/cancel", {}, elif_)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert cancelled.json()["data"]["fee_refunded"] is True
    assert cancelled.json()["message"] == "Rezervasyon iptal edildi. Ücret ters kayıtla kapatıldı."
    _, refund = await entries(rz)
    assert (refund.credit, refund.reversal_of_entry_id) == (Decimal("150.00"), charge.id)
    again = await rz.post(f"/resident/reservations/{r['id']}/cancel", {}, elif_)
    assert again.json()["error"]["code"] == "invalid_status"
    # Geç iptal: iade yok.
    late = await book(rz, elif_, area="hall", date="2026-10-01", start="12:00", guests=1)
    rid = late.json()["data"]["reservation"]["id"]
    out = await rz.post(f"/resident/reservations/{rid}/cancel", {}, elif_)
    assert out.json()["data"]["cancelled_late"] is True
    assert out.json()["message"] == (
        "Rezervasyon iptal edildi. Son iptal süresi geçtiği için ücret iade edilmez."
    )
    assert len(await entries(rz)) == 3


async def test_kurallar_kanal_sinir_ve_alan_hatalari(rz: Rz) -> None:
    elif_ = await rz.resident("elif", "elif@test.local")
    past = await book(rz, elif_, date="2026-10-01", start="08:00")
    assert past.json()["error"]["fields"] == {"start": "Saat seçin."}  # açılıştan önce
    bad = await book(rz, elif_, date="2026-09-30", people=11, guests=6)
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"date", "people", "guests"}
    rz.now[0] = START + timedelta(hours=3)  # 11:00
    gone = await book(rz, elif_, date="2026-10-01", start="10:00")
    assert gone.json()["error"]["fields"] == {"start": "Geçmiş saat seçilemez."}
    rz.now[0] = START
    for start in ("10:00", "11:00"):
        assert (await book(rz, elif_, start=start)).status_code == 201
    dup = await book(rz, elif_, start="11:00")
    assert dup.json()["error"]["code"] in {"limit", "duplicate"}
    limit = await book(rz, elif_, start="12:00")
    assert (limit.status_code, limit.json()["error"]["code"]) == (409, "limit")
    assert "haftada 2" in limit.json()["error"]["message"]
    await rz.put(f"/reservable-areas/{rz.ids['hall']}/rules", rules(channel="staff"))
    staff_only = await book(rz, elif_, area="hall")
    assert staff_only.json()["error"] == {
        "code": "channel",
        "fields": None,
        "request_id": staff_only.json()["error"]["request_id"],
        "message": "Toplantı salonu için rezervasyonu yönetim açar; lütfen yönetimi arayın.",
    }
    assert [a["name"] for a in (await rz.get("/resident/reservable-areas", elif_)).json()] == [
        "Spor salonu"
    ]
    await rz.put(f"/reservable-areas/{rz.ids['hall']}/rules", rules(enabled=False))
    closed = await rz.post(
        "/reservations",
        {
            "unit_id": rz.ids["unit_B"],
            "area_id": rz.ids["hall"],
            "date": TOMORROW,
            "start": "10:00",
        },
    )
    assert closed.json()["error"]["code"] == "area_closed"
    not_mine = await book(rz, elif_, unit_id=rz.ids["unit_B"])
    assert not_mine.json()["error"]["code"] == "unit_not_yours"


async def test_esazamanli_iki_istekten_biri_kazanir(rz: Rz) -> None:
    """REZ-02 S1: aynı slot için iki eşzamanlı istek → yalnızca biri onaylanır."""
    elif_ = await rz.resident("elif", "elif@test.local")
    mehmet = await rz.resident("mehmet", "mehmet@test.local")
    first, second = await asyncio.gather(book(rz, elif_), book(rz, mehmet))
    codes = sorted([first.status_code, second.status_code])
    assert codes == [201, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "slot_full"
    assert "Boş saatler: 09:00, 11:00, 12:00, 13:00." in loser.json()["error"]["message"]
    with site_scope(rz.site_id):
        async with rz.factory() as session:
            live = await session.scalars(
                select(Reservation).where(Reservation.status == "approved")
            )
            assert len(list(live)) == 1


async def test_veritabani_kisiti_cakisani_reddeder(rz: Rz) -> None:
    """Servis atlanırsa bile aynı alanda aynı yer + başlangıç ikinci canlı kayıt olamaz."""
    elif_ = await rz.resident("elif", "elif@test.local")
    made = (await book(rz, elif_)).json()["data"]["reservation"]
    with site_scope(rz.site_id):
        async with rz.factory() as session:
            original = await session.get(Reservation, uuid.UUID(made["id"]))
            assert original is not None
            clone = Reservation(
                area_id=original.area_id,
                unit_id=uuid.UUID(rz.ids["unit_B"]),
                person_name="X",
                date=original.date,
                start_time=original.start_time,
                end_time=original.end_time,
                starts_at=original.starts_at,
                ends_at=original.ends_at,
                seat=1,
                people=1,
                guests=0,
                status="pending",
                check_in_code="000001",
                channel="staff",
                created_by_name="Test",
                fee=Decimal(0),
            )
            session.add(clone)
            with pytest.raises(IntegrityError):
                await session.flush()


async def test_bekleme_listesi_teklif_kabul_ve_sure_dolumu(rz: Rz) -> None:
    """REZ-02 S2: iptal + bekleme listesinde 2 kişi; ilk kişi süre içinde kabul etmez → teklif
    ikinci kişiye geçer."""
    elif_ = await rz.resident("elif", "elif@test.local")
    mehmet = await rz.resident("mehmet", "mehmet@test.local")
    ayse = await rz.resident("ayse", "ayse@test.local")
    await rz.put(f"/reservable-areas/{rz.ids['gym']}/rules", rules(weekly_limit=0))
    holder = (await book(rz, ayse)).json()["data"]["reservation"]
    full = await book(rz, mehmet)
    assert full.json()["error"]["message"].endswith("Bekleme listesine girebilirsiniz.")
    w1 = await book(rz, mehmet, waitlist=True)
    assert (w1.status_code, w1.json()["data"]["status"], w1.json()["data"]["position"]) == (
        201,
        "waitlisted",
        1,
    )
    assert (await book(rz, mehmet, waitlist=True)).json()["error"]["code"] == "already_waiting"
    staff_wait = (await rz.get("/waitlist")).json()
    assert [w["unit_name"] for w in staff_wait["items"]] == ["B-1"]
    slots = (
        await rz.get(f"/reservable-areas/{rz.ids['gym']}/availability", params={"date": TOMORROW})
    ).json()
    assert next(s for s in slots if s["start"] == "10:00")["waiting"] == 1
    # Ayşe iptal eder → Mehmet'e 30 dakikalık teklif.
    out = await rz.post(f"/resident/reservations/{holder['id']}/cancel", {}, ayse)
    assert out.json()["message"].endswith("Bekleme listesindeki B-1 bölümüne teklif gitti.")
    [offer] = (await rz.get("/resident/reservations", mehmet)).json()["waitlist"]
    assert offer["status"] == "offered"
    assert offer["offered_until"] is not None
    assert len(await notifications(rz, "reservation.waitlist_offer")) == 1
    # Teklif yeri tutar: Elif bu sırada alamaz.
    taken = await book(rz, elif_)
    assert taken.json()["error"]["code"] == "slot_full"
    accepted = await rz.post(f"/resident/waitlist/{offer['id']}/accept", {}, mehmet)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["data"]["reservation"]["unit_name"] == "B-1"
    assert (await rz.get("/resident/reservations", mehmet)).json()["waitlist"] == []

    # S2: süre dolumu — iki kişi sırada, ilki kabul etmez.
    second_slot = {"start": "15:00"}
    holder2 = (await book(rz, mehmet, **second_slot)).json()["data"]["reservation"]
    first = (await book(rz, ayse, waitlist=True, **second_slot)).json()["data"]["waitlist"]
    # İkinci bekleyen: yeni bölüm B-2.
    unit_c = (await rz.post("/units", {"block_id": rz.ids["block_B"], "number": "2"})).json()
    rz.ids["zeynep"] = await _party(rz, unit_c["data"]["id"], "owner", "Zeynep", "Ak")
    zeynep = await rz.resident("zeynep", "zeynep@test.local")
    second = (await book(rz, zeynep, waitlist=True, **second_slot)).json()["data"]["waitlist"]
    await rz.post(f"/resident/reservations/{holder2['id']}/cancel", {}, mehmet)
    later = START + timedelta(minutes=31)
    result = await run_jobs(rz, later)
    assert (result.expired_offers, result.new_offers) == (1, 1)
    rz.now[0] = later
    expired = await rz.post(f"/resident/waitlist/{first['id']}/accept", {}, ayse)
    assert expired.json()["error"] == {
        "code": "expired",
        "fields": None,
        "request_id": expired.json()["error"]["request_id"],
        "message": "Teklifin süresi doldu; sıradaki kişiye geçti.",
    }
    [z] = (await rz.get("/resident/reservations", zeynep)).json()["waitlist"]
    assert (z["id"], z["status"]) == (second["id"], "offered")
    left = await rz.post(f"/resident/waitlist/{second['id']}/leave", {}, zeynep)
    assert left.json()["data"]["status"] == "left"


# --- Onay ------------------------------------------------------------------------------------


async def test_onayli_alan_yonetici_onayi_ve_onay_mekanizmasi(rz: Rz) -> None:
    await rz.put(
        f"/reservable-areas/{rz.ids['hall']}/rules",
        rules(approval="manual", fee_type="fixed", fee_amount="300.00", weekly_limit=0),
    )
    elif_ = await rz.resident("elif", "elif@test.local")
    first = await book(rz, elif_, area="hall")
    assert first.status_code == 201
    data = first.json()["data"]
    assert (data["status"], data["reservation"]["status"]) == ("pending", "pending")
    assert first.json()["message"] == "Rezervasyon alındı; yönetici onayı bekleniyor."
    assert await entries(rz) == []  # onaysız ücret yok
    assert len(await notifications(rz, "reservation.pending")) == 1
    rid = data["reservation"]["id"]
    pending = (await rz.get("/reservations", params={"status": "pending"})).json()
    assert [p["id"] for p in pending["items"]] == [rid]
    no_reason = await rz.post(f"/reservations/{rid}/reject", {"reason": "yok"})
    assert no_reason.json()["error"]["fields"] == {"reason": "Ret gerekçesi yazın."}
    ok = await rz.post(f"/reservations/{rid}/approve", {})
    assert ok.status_code == 200, ok.text
    assert ok.json()["message"] == "Rezervasyon onaylandı. Ücret 300,00 TL borca yansıdı."
    assert ok.json()["data"]["decided_by"] == "Kerem YILDIRIM"
    assert len(await entries(rz)) == 1
    twice = await rz.post(f"/reservations/{rid}/approve", {})
    assert twice.json()["error"]["code"] == "invalid_status"

    # Onay kuralı "Rezervasyon" tanımlıysa onaya gider (202) ve ONY'den yürütülür.
    board = await rz.user("yk@test.local", "Yönetim Kurulu Üyesi")
    rule = await rz.post(
        "/approval-rules",
        {
            "action_type": "reservation",
            "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": "2026-10-01",
        },
    )
    assert rule.status_code == 201, rule.text
    sent = await book(rz, elif_, area="hall", start="12:00")
    assert sent.status_code == 202, sent.text
    body = sent.json()["data"]
    assert body["status"] == "approval_pending"
    assert body["approval_request_id"]
    rid2 = body["reservation"]["id"]
    blocked = await rz.post(f"/reservations/{rid2}/approve", {})
    assert blocked.json()["error"]["code"] == "approval_pending"
    decided = await rz.post(f"/approvals/{body['approval_request_id']}/approve", {}, board)
    assert decided.json()["data"]["status"] == "executed", decided.text
    listed = (
        await rz.get("/reservations", params={"date": TOMORROW, "area_id": rz.ids["hall"]})
    ).json()
    row = next(r for r in listed["items"] if r["id"] == rid2)
    assert (row["status"], row["approval_status"], row["fee"]) == ("approved", "executed", "300.00")
    assert len(await entries(rz)) == 2
    # Ret ONY'den: rezervasyon reddedilir, ücret oluşmaz.
    third = (await book(rz, elif_, area="hall", start="14:00", date="2026-10-03")).json()["data"]
    refused = await rz.post(
        f"/approvals/{third['approval_request_id']}/reject", {"comment": "Salon bakımda"}, board
    )
    assert refused.status_code == 200, refused.text
    mine = (await rz.get("/resident/reservations", elif_)).json()["reservations"]
    r3 = next(r for r in mine if r["id"] == third["reservation"]["id"])
    assert (r3["status"], r3["reject_reason"]) == ("rejected", "Salon bakımda")
    assert len(await entries(rz)) == 2


async def test_sakin_adina_rezervasyon_ve_bildirim(rz: Rz) -> None:
    await rz.resident("elif", "elif@test.local")
    made = await rz.post(
        "/reservations",
        {
            "unit_id": rz.ids["unit_A"],
            "area_id": rz.ids["gym"],
            "date": TOMORROW,
            "start": "18:00",
            "people": 3,
        },
    )
    assert made.status_code == 201, made.text
    r = made.json()["data"]["reservation"]
    assert (r["channel"], r["created_by"], r["person_name"]) == (
        "staff",
        "Kerem YILDIRIM",
        "Elif DEMİR",
    )
    assert made.json()["message"] == "Rezervasyon onaylandı. Sakine bildirim gitti."
    [note] = await notifications(rz, "reservation.created")
    assert note.title == "Adınıza rezervasyon açıldı: Spor salonu"
    missing = await rz.post(
        "/reservations",
        {
            "unit_id": str(uuid.uuid4()),
            "area_id": rz.ids["gym"],
            "date": TOMORROW,
            "start": "19:00",
        },
    )
    assert missing.json()["error"]["fields"] == {"unit_id": "Bölüm bulunamadı."}
    cancelled = await rz.post(f"/reservations/{r['id']}/cancel", {"reason": "Bakım"})
    assert cancelled.json()["data"]["cancel_reason"] == "Bakım"
    assert len(await notifications(rz, "reservation.cancelled")) == 1


# --- 61 Doğrulama, no-show, rapor --------------------------------------------------------------


async def test_kodla_giris_noshow_ve_kisit(rz: Rz) -> None:
    await rz.put(
        f"/reservable-areas/{rz.ids['gym']}/rules",
        rules(weekly_limit=0, noshow={"enabled": True, "months": 3, "count": 1, "block_days": 14}),
    )
    elif_ = await rz.resident("elif", "elif@test.local")
    today = await book(rz, elif_, date="2026-10-01", start="10:00")
    code = next(
        r["code"] for r in (await rz.get("/resident/reservations", elif_)).json()["reservations"]
    )
    assert (await rz.post("/reservations/check-in-code", {"code": "12"})).json()["error"][
        "fields"
    ] == {"code": "6 haneli kodu girin."}
    wrong = "000000" if code != "000000" else "111111"
    nf = await rz.post("/reservations/check-in-code", {"code": wrong})
    assert (nf.status_code, nf.json()["error"]["message"]) == (
        404,
        "Bugün için bu kodla rezervasyon yok.",
    )
    security = await rz.user("guvenlik@test.local", "Güvenlik")
    ok = await rz.post("/reservations/check-in-code", {"code": f"SY-REZ:{code}"}, security)
    assert ok.status_code == 200, ok.text
    assert ok.json()["message"].startswith("Doğrulandı: A-1 · Elif DEMİR — Spor salonu 10:00–11:00")
    again = await rz.post("/reservations/check-in-code", {"code": code}, security)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already")
    # Yarınki rezervasyona bugün "geldi" denemez.
    future = (await book(rz, elif_, start="12:00")).json()["data"]["reservation"]
    nt = await rz.post(f"/reservations/{future['id']}/check-in", {}, security)
    assert nt.json()["error"]["code"] == "not_today"
    # Yarın 13:00 biter, giriş yok → no-show; kural 1 kez → 14 gün kısıt + bildirim.
    result = await run_jobs(rz, datetime(2026, 10, 2, 10, 0, tzinfo=UTC))
    assert (result.no_show, result.restricted) == (1, 1)
    [blocked_note] = await notifications(rz, "reservation.noshow_blocked")
    assert "16.10.2026" in (blocked_note.body or "")
    rz.now[0] = datetime(2026, 10, 3, 5, 0, tzinfo=UTC)
    blocked = await book(rz, elif_, date="2026-10-05")
    assert blocked.json()["error"] == {
        "code": "noshow_blocked",
        "fields": None,
        "request_id": blocked.json()["error"]["request_id"],
        "message": "Gelinmeyen rezervasyonlar nedeniyle 16.10.2026 tarihine kadar Spor salonu "
        "rezervasyonu yapamazsınız.",
    }
    av = (await rz.get(f"/resident/reservable-areas/{rz.ids['gym']}/availability", elif_)).json()
    assert av["blocked_until"] == "2026-10-16"
    assert today.status_code == 201

    report = (await rz.get("/reservations/report", params={"to": "2026-10-03"})).json()
    assert report["from"] == "2026-09-04"
    gym = next(r for r in report["rows"] if r["area_name"] == "Spor salonu")
    assert (gym["total"], gym["checked_in"], gym["no_show"], gym["no_show_rate"]) == (2, 1, 1, 50)
    assert gym["occupancy"] == "0.5"  # 2 / (13 saat × 1 × 30 gün)


async def test_dogrulama_yoksa_tamamlandi_sayilir(rz: Rz) -> None:
    await rz.put(f"/reservable-areas/{rz.ids['gym']}/rules", rules(verify="none"))
    elif_ = await rz.resident("elif", "elif@test.local")
    await book(rz, elif_)
    result = await run_jobs(rz, datetime(2026, 10, 3, 0, 0, tzinfo=UTC))
    assert (result.completed, result.no_show) == (1, 0)
    report = (await rz.get("/reservations/report", params={"to": "2026-10-03"})).json()
    gym = next(r for r in report["rows"] if r["area_name"] == "Spor salonu")
    assert gym["no_show_rate"] is None


# --- K11 borçlu bölüm kısıtı --------------------------------------------------------------


async def test_k11_borclu_bolum_kisiti(rz: Rz) -> None:
    default = (await rz.get("/reservation-settings")).json()
    assert default == {"debt_block_enabled": False, "debt_min_amount": None, "debt_min_days": None}
    bad = await rz.put("/reservation-settings", {"debt_block_enabled": True})
    assert set(bad.json()["error"]["fields"]) == {"debt_min_amount", "debt_min_days"}
    elif_ = await rz.resident("elif", "elif@test.local")
    with site_scope(rz.site_id):
        async with rz.factory() as session, session.begin():
            account = await session.scalar(
                select(LedgerAccount).where(
                    LedgerAccount.unit_id == uuid.UUID(rz.ids["unit_A"]),
                    LedgerAccount.kind == "occupant",
                )
            )
            assert account is not None
            await ledger.lock_accounts(session, [account.id])
            session.add(
                LedgerEntry(
                    account_id=account.id,
                    date=date(2026, 8, 1),
                    due_date=date(2026, 8, 15),
                    debit=Decimal("2500.00"),
                    source="adjustment",
                    description="Eski borç",
                )
            )
            await session.flush()
            await ledger.refresh_balances(session, [account.id])
    # Kapalıyken borç engellemez (doküman §14.2 varsayılanı).
    assert (await book(rz, elif_, start="09:00")).status_code == 201
    saved = await rz.put(
        "/reservation-settings",
        {"debt_block_enabled": True, "debt_min_amount": "1000.00", "debt_min_days": 30},
    )
    assert saved.status_code == 200, saved.text
    blocked = await book(rz, elif_, start="11:00")
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "debt_restricted")
    assert "2.500,00 TL" in blocked.json()["error"]["message"]
    av = (await rz.get(f"/resident/reservable-areas/{rz.ids['gym']}/availability", elif_)).json()
    assert av["debt_block"] is not None
    # Eşik gün dolmadıysa engel yok.
    await rz.put(
        "/reservation-settings",
        {"debt_block_enabled": True, "debt_min_amount": "1000.00", "debt_min_days": 60},
    )
    assert (await book(rz, elif_, start="11:00")).status_code == 201
    mehmet = await rz.resident("mehmet", "mehmet@test.local")
    await rz.put(
        "/reservation-settings",
        {"debt_block_enabled": True, "debt_min_amount": "1000.00", "debt_min_days": 30},
    )
    assert (await book(rz, mehmet, start="13:00")).status_code == 201  # borcu yok


# --- Yetki, hane üyesi, modül, izolasyon ------------------------------------------------------


async def test_yetki_hane_uyesi_modul_ve_izolasyon(rz: Rz) -> None:
    security = await rz.user("guvenlik@test.local", "Güvenlik")
    assert (await rz.get("/reservations", security)).status_code == 200
    assert (await rz.get("/reservable-areas", security)).status_code == 200
    assert (
        await rz.put(f"/reservable-areas/{rz.ids['gym']}/rules", rules(), security)
    ).status_code == 403
    on_behalf = await rz.post(
        "/reservations",
        {"unit_id": rz.ids["unit_A"], "area_id": rz.ids["gym"], "date": TOMORROW, "start": "10:00"},
        security,
    )
    assert on_behalf.status_code == 403
    assert (await rz.get("/reservations/report", security)).status_code == 403
    accounting = await rz.user("muhasebe@test.local", "Muhasebe")
    assert (await rz.get("/reservations", accounting)).status_code == 403
    elif_ = await rz.resident("elif", "elif@test.local")
    assert (await rz.get("/reservations", elif_)).status_code == 403
    staff_only = await rz.get("/resident/reservations", security)
    assert staff_only.status_code == 403

    # Hane üyesi: "Rezervasyon" yetkisi kapalıysa 403 (varsayılan kapalı).
    person = await _party(rz, rz.ids["unit_A"], "resident", "Can", "Demir")
    with site_scope(rz.site_id):
        async with rz.factory() as session, session.begin():
            session.add(
                HouseholdMember(
                    unit_id=uuid.UUID(rz.ids["unit_A"]),
                    person_id=uuid.UUID(person),
                    full_name="Can Demir",
                    phone="+905321112233",
                    status="active",
                    can_reservations=False,
                )
            )
    rz.ids["can"] = person
    can = await rz.resident("can", "can@test.local")
    denied = await book(rz, can)
    assert denied.status_code == 403
    with site_scope(rz.site_id):
        async with rz.factory() as session, session.begin():
            member = await session.scalar(
                select(HouseholdMember).where(HouseholdMember.person_id == uuid.UUID(person))
            )
            assert member is not None
            member.can_reservations = True
    assert (await book(rz, can)).status_code == 201

    # Başka site: aynı yönetici kendi diğer sitesinde bu alanı göremez.
    yildiz = rz.url(f"/reservable-areas/{rz.ids['gym']}/rules", "yildiz-sitesi")
    assert (await rz.api.put(yildiz, json=rules(), headers=rz.manager)).status_code == 404
    other = (await rz.api.get(rz.url("/reservations", "yildiz-sitesi"), headers=rz.manager)).json()
    assert other["total"] == 0
    assert (
        await rz.api.get(rz.url("/reservable-areas", "yildiz-sitesi"), headers=rz.manager)
    ).json() == []
    # Modül kapalı → 404; veri yerinde.
    async with rz.factory() as session, session.begin():
        site = await session.get(Site, rz.site_id)
        assert site is not None
        with site_scope(site.id):
            await set_module_enabled(session, site, ModuleKey.RESERVATIONS, enable=False)
    assert (await rz.get("/reservations")).status_code == 404
    assert (await rz.get("/resident/reservations", elif_)).status_code == 404


async def test_komut_satiri_rezervasyon_isleri(
    rz: Rz,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_urls: DatabaseUrls,
) -> None:
    elif_ = await rz.resident("elif", "elif@test.local")
    assert (await book(rz, elif_)).status_code == 201  # 02.10.2026 — gerçek saate göre geçmiş
    monkeypatch.setenv("DATABASE_URL", database_urls.app)
    monkeypatch.setenv("LOG_LEVEL", "CRITICAL")
    get_settings.cache_clear()
    try:  # komut kendi olay döngüsünü açar: ayrı iş parçacığında
        assert await asyncio.to_thread(cli.main, ["run-reservation-jobs"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "gelinmeyen 1" in out
    assert "1 sitede rezervasyon işleri çalıştı." in out
