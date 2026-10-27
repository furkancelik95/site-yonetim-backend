"""Dönem kapanışı ve kilidi (FIN-13, servis isteği 27; D-11).

Kilitli/kapanıştaki döneme hiçbir finansal kayıt eklenemez/değişemez: her yazma yolu 409
`period_locked`. Kapanış iş akışı: kontrol listesi, gerekçeli istisna, iki kişi kuralı, süreli
açma (onaylı ya da onaysız), süre dolunca yeniden kilit.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx2
from sqlalchemy import select, update

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, Period
from site_yonetim.services import finance_jobs
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


async def set_period(w: World, year: int, month: int, status: str, **extra: Any) -> None:
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            row = await session.scalar(
                select(Period).where(Period.year == year, Period.month == month)
            )
            if row is None:
                session.add(Period(year=year, month=month, status=status, **extra))
            else:
                await session.execute(
                    update(Period).where(Period.id == row.id).values(status=status, **extra)
                )


async def cash_ids(w: World) -> dict[str, str]:
    items = (await w.get("/cash-accounts")).json()["items"]
    return {a["name"]: a["id"] for a in items}


def locked(response: httpx2.Response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "period_locked", response.text


async def expense(w: World, day: str, *, paid: bool = False, account: str | None = None) -> Any:
    form = {
        "expense_category_id": w.lookups["category"],
        "description": "Asansör bakımı",
        "amount": "100.00",
        "date": day,
        "paid": "true" if paid else "false",
    }
    if paid:
        form["paid_on"] = day
        form["cash_account_id"] = account or ""
    return await w.api.post(w.url("/expenses"), data=form, headers=w.headers)


async def test_kilitli_doneme_hicbir_finansal_kayit_yazilamaz(world: World) -> None:
    w = world
    cash = await cash_ids(w)
    kasa, banka = cash["Kasa"], cash["Banka Hesabı"]
    # Mayıs'ta kayıtlar (dönem açıkken), sonra Mayıs kilitlenir.
    opening = await w.post(
        f"/accounts/{w.account_id}/opening-balance",
        {"amount": "500.00", "direction": "credit", "date": "2026-05-02"},
    )
    assert opening.status_code == 201, opening.text
    movement = await w.post(
        "/cash-movements",
        {
            "cash_account_id": kasa,
            "date": "2026-05-05",
            "direction": "in",
            "amount": "50.00",
            "description": "Faiz geliri",
        },
    )
    assert movement.status_code == 201, movement.text
    paid = await expense(w, "2026-05-06", paid=True, account=banka)
    assert paid.status_code == 201, paid.text
    unpaid = await expense(w, "2026-05-07")
    await set_period(w, 2026, 5, "locked")

    day = "2026-05-20"
    # tahsilat
    locked(
        await w.post(
            "/payments",
            {"ledger_account_id": w.account_id, "amount": "10.00", "date": day, "method": "cash"},
        )
    )
    # iade (alacak bakiyesi var: 500 avans)
    locked(
        await w.post(
            "/refunds",
            {
                "ledger_account_id": w.account_id,
                "amount": "10.00",
                "date": day,
                "cash_account_id": kasa,
                "reason": "Fazla ödeme iadesi",
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
    )
    # gider: kayıt, ödeme (Mayıs tarihli), düzeltme (Mayıs'a ait gider)
    locked(await expense(w, day))
    locked(
        await w.post(
            f"/expenses/{unpaid.json()['data']['id']}/pay",
            {"cash_account_id": banka, "paid_on": day},
        )
    )
    locked(await w.post(f"/expenses/{paid.json()['data']['id']}/reverse", {"reason": "Hatalı"}))
    # kasa: elle hareket, aktarım, geri alma (Mayıs hareketi)
    locked(
        await w.post(
            "/cash-movements",
            {
                "cash_account_id": kasa,
                "date": day,
                "direction": "out",
                "amount": "5.00",
                "description": "Banka masrafı",
            },
        )
    )
    locked(
        await w.post(
            "/cash-transfers", {"from_id": banka, "to_id": kasa, "date": day, "amount": "5.00"}
        )
    )
    reverse = await w.post(
        f"/cash-movements/{movement.json()['data']['id']}/reverse", {"reason": "Yanlış hesap"}
    )
    locked(reverse)
    assert "ters kaydedilemez" in reverse.json()["error"]["message"]
    # yeni kasa hesabının Mayıs tarihli açılış hareketi
    locked(
        await w.post(
            "/cash-accounts",
            {
                "name": "Ofis kasası",
                "kind": "cash",
                "opening_balance": "100.00",
                "opening_date": day,
            },
        )
    )
    # tahakkuk: Mayıs tarihli koşu
    await finalized_plan(w)
    locked(await post_run(w, "2026-05-01"))
    # devir bakiye (başka hesapta yok; aynı hesapta zaten var → önce kilit kontrolü)
    locked(
        await w.post(
            f"/accounts/{w.account_id}/opening-balance",
            {"amount": "1.00", "direction": "debit", "date": day},
        )
    )
    # mesaj en erken açık dönemi söyler
    response = await w.post(
        "/payments",
        {"ledger_account_id": w.account_id, "amount": "10.00", "date": day, "method": "cash"},
    )
    assert response.json()["error"]["message"] == (
        "Tarih kilitli döneme düşüyor (Mayıs 2026); kayıt yazılamaz — en erken Haziran 2026 "
        "(01.06.2026)."
    )
    # açık dönem (Haziran) serbest
    ok = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": "10.00",
            "date": "2026-06-10",
            "method": "cash",
        },
    )
    assert ok.status_code == 201, ok.text


async def test_kilitli_donemin_tahakkuku_ters_kaydedilemez(world: World) -> None:
    w = world
    await finalized_plan(w)
    run = await post_run(w, "2026-06-01")
    assert run.status_code == 201, run.text
    await set_period(w, 2026, 6, "locked")
    w.set_today(date(2026, 7, 3))
    reverse = await w.post(f"/charge-runs/{run.json()['data']['id']}/reverse", {"reason": "Hata"})
    locked(reverse)
    # Temmuz tahakkuku kilitli Haziran'ı atlar; Temmuz açık
    assert (await post_run(w, "2026-07-01")).status_code == 201


async def test_ek_ucret_ve_odeme_plani_kilitli_donemde_yazilamaz(world: World) -> None:
    w = world
    await w.post(
        f"/accounts/{w.account_id}/opening-balance",
        {"amount": "900.00", "direction": "debit", "date": "2026-06-01"},
    )
    charge = await w.post(
        "/ad-hoc-charges",
        {
            "unit_id": w.unit_id,
            "account_kind": "occupant",
            "source": "penalty",
            "description": "Gürültü cezası",
            "amount": "100.00",
        },
    )
    assert charge.status_code == 201, charge.text
    await set_period(w, 2026, 6, "locked")
    locked(
        await w.post(
            "/ad-hoc-charges",
            {
                "unit_id": w.unit_id,
                "account_kind": "occupant",
                "source": "other",
                "description": "Anahtar",
                "amount": "10.00",
            },
        )
    )
    locked(
        await w.post(
            f"/ad-hoc-charges/{charge.json()['data']['id']}/cancel", {"reason": "Yanlış bölüm"}
        )
    )
    locked(
        await w.post(
            f"/accounts/{w.account_id}/payment-plans",
            {"amount": "300.00", "installment_count": 3, "first_due_date": "2026-07-10"},
        )
    )


async def test_sureli_acik_donemde_yazilir_etiketlenir_ve_sure_dolunca_kilitlenir(
    world: World,
) -> None:
    w = world
    later = datetime.now(UTC) + timedelta(hours=4)
    await set_period(w, 2026, 5, "temp_open", unlock_until=later)
    ok = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": "10.00",
            "date": "2026-05-20",
            "method": "cash",
        },
    )
    assert ok.status_code == 201, ok.text
    with site_scope(w.site_id):
        async with w.factory() as session:
            reasons = list(
                await session.scalars(
                    select(AuditLog.reason).where(AuditLog.reason == "period_unlocked")
                )
            )
    assert reasons == ["period_unlocked"]
    # süre doldu: okurken kilitli, iş geçmişe yazar
    await set_period(w, 2026, 5, "temp_open", unlock_until=datetime.now(UTC) - timedelta(minutes=1))
    locked(
        await w.post(
            "/payments",
            {
                "ledger_account_id": w.account_id,
                "amount": "10.00",
                "date": "2026-05-21",
                "method": "cash",
            },
        )
    )
    results = await finance_jobs.run_all(w.factory, now=datetime.now(UTC), today=w.today[0])
    assert dict(results)[w.site_id].relocked == 1
    may = next(p for p in (await w.get("/fiscal-periods")).json() if p["period"] == "2026-05")
    assert may["status"] == "locked"
    assert may["history"][-1]["action"] == "relocked"


async def second_manager(w: World) -> dict[str, str]:
    user = await create_user(w.factory, "ikinci@test.local", full_name="Selin KAYA")
    await add_site_membership(w.factory, w.site_id, user.id, "Yönetici")
    return await login_headers(w.api, "ikinci@test.local")


async def test_kapanis_kontrol_listesi_iki_kisi_ve_sureli_acma(world: World) -> None:
    w = world
    other = await second_manager(w)
    cash = await cash_ids(w)
    await w.post(
        "/cash-movements",
        {
            "cash_account_id": cash["Kasa"],
            "date": "2026-05-05",
            "direction": "in",
            "amount": "50.00",
            "description": "Faiz geliri",
        },
    )
    periods = (await w.get("/fiscal-periods", params={"year": 2026})).json()
    assert [p["period"] for p in periods][:2] == ["2026-06", "2026-05"]
    assert len(periods) == 6
    assert periods[0]["is_past"] is False

    checklist = (await w.get("/fiscal-periods/2026-05/close-checklist")).json()
    items = {i["key"]: i for i in checklist["items"]}
    assert items["charge"]["ok"] is False
    assert items["charge"]["required"] is True
    assert items["cash_days"]["ok"] is False
    assert "05.05.2026" in items["cash_days"]["detail"]
    assert items["approvals"]["ok"] is True
    assert items["accounting"]["ok"] is True  # kasa elle hareketi fiş kaynağı değil

    current = await w.post("/fiscal-periods/2026-06/start-close", {})
    assert current.json()["error"]["code"] == "period_not_ended"
    missing = await w.post(
        "/fiscal-periods/2026-05/start-close", {"exceptions": {"charge": "kısa"}}
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "checklist_incomplete"
    started = await w.post(
        "/fiscal-periods/2026-05/start-close",
        {
            "exceptions": {
                "charge": "Mayıs aidatı eski sistemde kesildi",
                "cash_days": "Sayım tutanağı kâğıt üzerinde imzalandı",
            }
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["data"]["status"] == "closing"
    # kapanıştaki döneme yazma yeni döneme yönlendirilir
    closing = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": "10.00",
            "date": "2026-05-25",
            "method": "cash",
        },
    )
    locked(closing)
    assert "kapanışta" in closing.json()["error"]["message"]
    # iki kişi kuralı
    same = await w.post("/fiscal-periods/2026-05/lock")
    assert same.json()["error"]["code"] == "four_eyes"
    second = await w.api.post(w.url("/fiscal-periods/2026-05/lock"), headers=other)
    assert second.status_code == 200, second.text
    assert second.json()["data"]["status"] == "locked"
    assert (await w.post("/fiscal-periods/2026-05/reopen")).json()["error"]["code"] == (
        "invalid_status"
    )
    # süreli açma: gerekçe ≥ 20, süre 4/24/72
    short = await w.post("/fiscal-periods/2026-05/unlock-requests", {"reason": "kısa", "hours": 4})
    assert short.status_code == 422
    assert set(short.json()["error"]["fields"]) == {"reason"}
    opened = await w.post(
        "/fiscal-periods/2026-05/unlock-requests",
        {"reason": "Banka masrafı Mayıs ekstresinde sonradan görüldü", "hours": 4},
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["data"]["status"] == "temp_open"
    ok = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": "10.00",
            "date": "2026-05-25",
            "method": "cash",
        },
    )
    assert ok.status_code == 201
    relock = await w.post("/fiscal-periods/2026-05/lock")  # süreli açık → herkes kilitler
    assert relock.json()["data"]["status"] == "locked"
    actions = [h["action"] for h in relock.json()["data"]["history"]]
    assert actions == ["start_close", "lock", "unlock", "lock"]


async def test_kilit_acma_onay_kuralina_baglanir(world: World) -> None:
    w = world
    other = await second_manager(w)
    rule = await w.post(
        "/approval-rules",
        {
            "action_type": "period_unlock",
            "steps": [{"roles": ["Yönetici"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": "2026-06-20",
        },
    )
    assert rule.status_code == 201, rule.text
    await set_period(w, 2026, 5, "locked")
    body = {"reason": "Banka masrafı Mayıs ekstresinde sonradan görüldü", "hours": 24}
    pending = await w.post("/fiscal-periods/2026-05/unlock-requests", body)
    assert pending.status_code == 202, pending.text
    data = pending.json()["data"]
    assert data["status"] == "locked"
    assert data["unlock_request_id"]
    again = await w.post("/fiscal-periods/2026-05/unlock-requests", body)
    assert again.json()["error"]["code"] == "unlock_pending"
    approved = await w.api.post(
        w.url(f"/approvals/{data['unlock_request_id']}/approve"), json={}, headers=other
    )
    assert approved.status_code == 200, approved.text
    may = next(p for p in (await w.get("/fiscal-periods")).json() if p["period"] == "2026-05")
    assert may["status"] == "temp_open"
    assert may["unlock_until"]
    assert may["unlock_request_id"] is None


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    w = world
    board = await create_user(w.factory, "yk@test.local", full_name="Ayşe YK")
    await add_site_membership(w.factory, w.site_id, board.id, "Yönetim Kurulu Üyesi")
    headers = await login_headers(w.api, "yk@test.local")
    assert (await w.api.get(w.url("/fiscal-periods"), headers=headers)).status_code == 200
    forbidden = await w.api.post(
        w.url("/fiscal-periods/2026-05/start-close"), json={}, headers=headers
    )
    assert forbidden.status_code == 403
    await set_period(w, 2026, 5, "locked")
    # diğer sitede aynı dönem açık
    other = await w.api.get(w.url("/fiscal-periods", "yildiz-sitesi"), headers=w.headers)
    may = next(p for p in other.json() if p["period"] == "2026-05")
    assert may["status"] == "open"
    stranger = await create_user(w.factory, "yabanci@test.local")
    await add_site_membership(w.factory, w.other_site_id, stranger.id, "Yönetici")
    foreign = await login_headers(w.api, "yabanci@test.local")
    assert (await w.api.get(w.url("/fiscal-periods"), headers=foreign)).status_code == 404
    bad = await w.get("/fiscal-periods/2026-13/close-checklist")
    assert bad.status_code == 422
