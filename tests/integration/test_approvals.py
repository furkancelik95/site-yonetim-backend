"""Onay mekanizması — Yazılım Dokümanı ONY-01..03 (gerçek PostgreSQL).

Kurgu `finance_world`: Kerem YILDIRIM iki sitede Yönetici; bugün 20.06.2026. Talepleri modül
yerine test açar (`request_approval`) ve "satın alma talebi" için sahte bir yürütücü kaydeder:
mevcut akışlar henüz onaya bağlı değil (K21).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.approvals import ActionType
from site_yonetim.models import ApprovalRequest, AuditLog, Notification, Site
from site_yonetim.services import approvals as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

TODAY = date(2026, 6, 20)
NOW = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
EXECUTED: list[uuid.UUID] = []


async def _purchase(session: Any, request: ApprovalRequest) -> None:
    if (request.payload or {}).get("fail"):
        raise svc.ExecutionError("Tedarikçi pasifleştirildi.")
    EXECUTED.append(request.id)


@pytest.fixture(autouse=True)
def purchase_executor() -> Any:
    EXECUTED.clear()
    svc.register_executor(ActionType.PURCHASE_REQUEST, _purchase)
    yield
    svc._EXECUTORS.pop(ActionType.PURCHASE_REQUEST, None)


@dataclass
class Member:
    id: uuid.UUID
    name: str
    headers: dict[str, str]

    @property
    def person(self) -> svc.Person:
        return svc.Person(self.id, self.name)


async def member(
    w: World, email: str, name: str, role: str, site: uuid.UUID | None = None
) -> Member:
    user = await create_user(w.factory, email, full_name=name)
    await add_site_membership(w.factory, site or w.site_id, user.id, role)
    return Member(user.id, name, await login_headers(w.api, email))


async def kerem(w: World) -> Member:
    me = (await w.api.get("/api/v1/me", headers=w.headers)).json()
    return Member(uuid.UUID(me["user_id"]), me["full_name"], w.headers)


@pytest.fixture
async def board(world: World) -> list[Member]:
    """Beş YK üyesi: Ayşe, Burak, Cem, Deniz, Ece."""
    names = ["Ayşe", "Burak", "Cem", "Deniz", "Ece"]
    return [
        await member(
            world, f"{n.lower()}@test.local".replace("ş", "s"), f"{n} YK", "Yönetim Kurulu Üyesi"
        )
        for n in names
    ]


RULE: dict[str, Any] = {
    "action_type": "purchase_request",
    "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
    "remind_after_days": 2,
    "escalate_after_days": 5,
    "effective_from": "2026-06-20",
}


async def add_rule(w: World, **changes: Any) -> httpx2.Response:
    return await w.post("/approval-rules", {**RULE, **changes})


async def open_request(
    w: World, requester: Member, *, amount: str = "42500.00", payload: dict[str, Any] | None = None
) -> str:
    with site_scope(w.site_id):
        async with w.factory() as session, session.begin():
            site = await session.get(Site, w.site_id)
            assert site is not None
            request = await svc.request_approval(
                session,
                site,
                svc.NewRequest(
                    ActionType.PURCHASE_REQUEST,
                    f"Satın alma — Asansör halatı — {amount} TL",
                    summary={"vendor": "Halat A.Ş."},
                    payload=payload,
                    amount=Decimal(amount),
                ),
                requester=requester.person,
                today=TODAY,
                now=NOW,
            )
            return str(request.id)


async def act(
    w: World, who: Member, request_id: str, action: str, comment: str | None = None
) -> httpx2.Response:
    body = {"comment": comment} if comment is not None else {}
    return await w.api.post(
        w.url(f"/approvals/{request_id}/{action}"), json=body, headers=who.headers
    )


async def detail(w: World, who: Member, request_id: str) -> dict[str, Any]:
    response = await w.api.get(w.url(f"/approvals/{request_id}"), headers=who.headers)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def tab(w: World, who: Member, name: str) -> list[str]:
    response = await w.api.get(w.url("/approvals"), params={"tab": name}, headers=who.headers)
    assert response.status_code == 200, response.text
    return [r["id"] for r in response.json()["items"]]


async def kinds(w: World, user: uuid.UUID) -> list[str]:
    with site_scope(w.site_id):
        async with w.factory() as session:
            rows = await session.scalars(
                select(Notification.kind)
                .where(Notification.user_id == user)
                .order_by(Notification.created_at)
            )
            return list(rows)


# --- ONY-01 Kurallar ------------------------------------------------------------------


async def test_kural_cakisma_bosluk_versiyon_simulasyon(world: World, board: list[Member]) -> None:
    w = world
    created = await add_rule(
        w, amount_max="10000.00", steps=[{"roles": ["Yönetici"], "quorum": "any"}]
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["data"]["status"] == "saved"
    assert body["data"]["warnings"] == [
        "10.000,00 TL üstü için kural yok; varsayılan kural uygulanır."
    ]
    rule = body["data"]["rule"]
    assert (rule["current"]["version"], rule["in_force_version"], rule["applicable"]) == (
        1,
        1,
        True,
    )
    assert rule["current"]["steps"][0] == {
        "roles": ["Yönetici"], "quorum": "any", "count": None, "eligible": 1, "required": 1,
    }  # fmt: skip

    overlap = await add_rule(w, amount_min="5000.00", amount_max="15000.00")
    assert overlap.status_code == 422
    assert overlap.json()["error"]["fields"] == {
        "amount_min": "Bu tutar aralığı aynı işlem tipindeki başka bir kuralla çakışıyor."
    }
    above = await add_rule(w, amount_min="10000.00")  # bitişik: çakışmaz, boşluk kapanır
    assert above.status_code == 201, above.text
    assert above.json()["data"]["warnings"] == []

    edited = await w.api.put(
        w.url(f"/approval-rules/{rule['id']}"),
        json={**RULE, "amount_max": "10000.00", "steps": [{"roles": ["Yönetici"], "quorum": "any"}],
              "remind_after_days": 3},
        headers=w.headers,
    )  # fmt: skip
    assert edited.status_code == 200, edited.text
    assert edited.json()["message"] == (
        "Kural kaydedildi (versiyon 2); bekleyen talepler eski versiyonla sürer."
    )

    small = await w.post(
        "/approval-rules/simulate", {"action_type": "purchase_request", "amount": "5000.00"}
    )
    assert small.json()["rule_id"] == rule["id"]
    assert small.json()["version"] == 2
    assert small.json()["steps"][0]["approvers"] == ["Kerem YILDIRIM"]
    other = await w.post("/approval-rules/simulate", {"action_type": "refund"})
    assert other.json()["rule_id"] is None  # varsayılan: site yöneticisi 1/1
    assert other.json()["steps"][0]["roles"] == ["Yönetici"]

    archived = await w.post(f"/approval-rules/{rule['id']}/archive")
    assert archived.json()["data"]["rule"]["is_active"] is False
    assert [
        r["id"] for r in (await w.get("/approval-rules", params={"archived": "true"})).json()
    ] == [rule["id"]]
    assert len((await w.get("/approval-rules")).json()) == 1


async def test_kural_dogrulama_ve_yetki(world: World, board: list[Member]) -> None:
    w = world
    nobody = await add_rule(w, steps=[{"roles": ["Muhasebe"], "quorum": "any"}])
    assert nobody.json()["error"]["fields"] == {"steps.1": "1. adımın rolünde aktif kullanıcı yok."}
    too_many = await add_rule(
        w, steps=[{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "count", "count": 6}]
    )
    assert too_many.json()["error"]["fields"] == {
        "steps.1": "1. adımda 5 onaycı var; 6 onay istenemez."
    }
    timing = await add_rule(
        w, remind_after_days=5, escalate_after_days=5, effective_from="2026-06-19"
    )
    assert timing.json()["error"]["fields"] == {
        "escalate_after_days": "Eskalasyon günü hatırlatma gününden sonra olmalı.",
        "effective_from": "Yürürlük tarihi bugün ya da ileri bir tarih olmalı.",
    }
    same = await add_rule(w, escalation_role="Yönetim Kurulu Üyesi")
    assert same.json()["error"]["fields"] == {
        "escalation_role": "Eskalasyon hedefi adımların onaycı rolünden farklı olmalı."
    }

    ayse = board[0]
    assert (await w.api.get(w.url("/approval-rules"), headers=ayse.headers)).status_code == 200
    assert (
        await w.api.post(w.url("/approval-rules"), json=RULE, headers=ayse.headers)
    ).status_code == 403
    tech = await member(w, "teknik@test.local", "Teknik Kişi", "Teknik Personel")
    assert (await w.api.get(w.url("/approval-rules"), headers=tech.headers)).status_code == 403


async def test_hazir_sablonlar(world: World, board: list[Member]) -> None:
    w = world
    restored = await w.post("/approval-rules/restore-defaults")
    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    types = sorted(r["action_type"] for r in data["created"])
    # tahakkuk şablonu Muhasebe ister; sitede muhasebeci yok → kurulmaz, gerekçesi döner
    assert types == [
        "charge_rule",
        "purchase_request",
        "purchase_request",
        "purchase_request",
        "refund",
        "registration",
    ]
    assert data["skipped"] == ["Tahakkuk: 1. adımın rolünde aktif kullanıcı yok."]
    assert all(r["is_template"] for r in data["created"])
    again = await w.post("/approval-rules/restore-defaults")
    assert again.json()["message"] == (
        "Kurulacak şablon yok: şablonu olan işlem tiplerinin hepsinde zaten kural var."
    )
    middle = await w.post(
        "/approval-rules/simulate", {"action_type": "purchase_request", "amount": "42500.00"}
    )
    assert [s["roles"] for s in middle.json()["steps"]] == [["Yönetici"], ["Yönetim Kurulu Üyesi"]]


# --- ONY-02 Talepler ----------------------------------------------------------------


async def test_m_of_n_talep_eden_cikar_ret_sayilir(world: World, board: list[Member]) -> None:
    """ONY-01 S2: 3/5 kuralında YK üyesi Ayşe talep açınca 3/4. ONY-02 S1: 2 onay + 1 ret
    ("hesaba sayılır") varken 4. üye onaylayınca talep onaylanır ve yürütülür."""
    w = world
    ayse, burak, cem, deniz, ece = board
    await add_rule(
        w,
        steps=[{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "count", "count": 3}],
        reject_behavior="counts",
    )
    request_id = await open_request(w, ayse)
    shown = await detail(w, ayse, request_id)
    step = shown["steps"][0]
    assert (step["required"], step["eligible"], shown["progress"]) == (3, 4, "0/3 onay")
    assert ayse.name not in [a["name"] for a in step["approvers"]]
    assert shown["can_decide"] is False
    own = await act(w, ayse, request_id, "approve")
    assert (own.status_code, own.json()["error"]["code"]) == (403, "own_request")

    assert request_id in await tab(w, burak, "pending")
    assert (await act(w, burak, request_id, "approve")).status_code == 200
    twice = await act(w, burak, request_id, "approve")
    assert (twice.status_code, twice.json()["error"]["code"]) == (403, "not_an_approver")
    assert request_id not in await tab(w, burak, "pending")
    await act(w, cem, request_id, "approve")
    rejected = await act(w, deniz, request_id, "reject", "Fiyat yüksek")
    assert rejected.json()["data"]["status"] == "pending"
    assert rejected.json()["data"]["progress"] == "2/3 onay"
    done = await act(w, ece, request_id, "approve")
    assert done.status_code == 200, done.text
    assert done.json()["message"] == "Onaylandı ve işlem yürütüldü."
    assert done.json()["data"]["status"] == "executed"
    assert [uuid.UUID(request_id)] == EXECUTED

    assert request_id in await tab(w, ayse, "mine")
    assert request_id in await tab(w, deniz, "history")
    assert "approval.approved" in await kinds(w, ayse.id)
    assert "approval.requested" in await kinds(w, burak.id)


async def test_tek_ret_reddeder_gerekce_zorunlu(world: World, board: list[Member]) -> None:
    w = world
    me = await kerem(w)
    burak, cem = board[1], board[2]
    await add_rule(w, steps=[{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "majority"}])
    request_id = await open_request(w, me)
    missing = await act(w, burak, request_id, "reject", "  ")
    assert missing.json()["error"]["fields"] == {"comment": "Gerekçe yazın."}
    rejected = await act(w, burak, request_id, "reject", "Bütçede yok")
    assert rejected.json()["data"]["status"] == "rejected"
    assert rejected.json()["message"] == "Talep reddedildi; talep edene gerekçeyle bildirildi."
    late = await act(w, cem, request_id, "approve")
    assert (late.status_code, late.json()["error"]["code"]) == (409, "not_pending")
    assert request_id not in await tab(w, cem, "pending")
    assert EXECUTED == []
    assert "approval.rejected" in await kinds(w, me.id)


async def test_sirali_ve_paralel_adimlar(world: World, board: list[Member]) -> None:
    w = world
    me = await kerem(w)
    ayse, burak = board[0], board[1]
    steps = [
        {"roles": ["Yönetici"], "quorum": "any"},
        {"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"},
    ]
    await add_rule(w, steps=steps)
    request_id = await open_request(w, ayse)
    assert request_id in await tab(w, me, "pending")
    assert request_id not in await tab(w, burak, "pending")  # 2. adım henüz açılmadı
    first = await act(w, me, request_id, "approve")
    assert [s["status"] for s in first.json()["data"]["steps"]] == ["approved", "open"]
    assert request_id in await tab(w, burak, "pending")
    assert (await act(w, burak, request_id, "approve")).json()["data"]["status"] == "executed"

    rules = (await w.get("/approval-rules")).json()
    parallel_rule = {**RULE, "steps": steps, "mode": "parallel"}
    switched = await w.api.put(
        w.url(f"/approval-rules/{rules[0]['id']}"), json=parallel_rule, headers=w.headers
    )
    assert switched.status_code == 200, switched.text
    parallel = await open_request(w, ayse)
    assert parallel in await tab(w, me, "pending")
    assert parallel in await tab(w, burak, "pending")  # aynı anda
    await act(w, burak, parallel, "approve")
    assert (await detail(w, me, parallel))["status"] == "pending"
    assert (await act(w, me, parallel, "approve")).json()["data"]["status"] == "executed"


async def test_revizyon_yeniden_gonderim_ve_geri_cekme(world: World, board: list[Member]) -> None:
    w = world
    me = await kerem(w)
    burak = board[1]
    await add_rule(w)
    request_id = await open_request(w, me)
    revised = await act(w, burak, request_id, "request-revision", "Üç teklif ekleyin")
    assert revised.json()["data"]["status"] == "revision_requested"
    assert revised.json()["data"]["can_resubmit"] is False  # Burak için
    assert (await detail(w, me, request_id))["can_resubmit"] is True
    other = await w.api.post(
        w.url(f"/approvals/{request_id}/resubmit"), json={}, headers=burak.headers
    )
    assert other.status_code == 403
    again = await w.post(f"/approvals/{request_id}/resubmit", {"title": "Satın alma — 3 teklifli"})
    assert again.status_code == 200, again.text
    body = again.json()["data"]
    assert (body["revision"], body["status"], body["title"]) == (
        2,
        "pending",
        "Satın alma — 3 teklifli",
    )
    approved = await act(w, burak, request_id, "approve")  # yeni sürümde yeniden karar verebilir
    assert approved.json()["data"]["status"] == "executed"
    history = approved.json()["data"]["history"]
    assert [(h["revision"], h["decision"], h["comment"]) for h in history] == [
        (1, "revision", "Üç teklif ekleyin"),
        (2, "approve", None),
    ]

    second = await open_request(w, me)
    assert (await act(w, burak, second, "withdraw")).status_code == 403
    withdrawn = await w.post(f"/approvals/{second}/withdraw")
    assert withdrawn.json()["data"]["status"] == "withdrawn"
    assert (await act(w, burak, second, "approve")).status_code == 409


async def test_onaylandi_yurutulemedi(world: World, board: list[Member]) -> None:
    """ONY-02 S4: yürütme anında veri geçersizse "onaylandı – yürütülemedi"."""
    w = world
    me = await kerem(w)
    await add_rule(w)
    request_id = await open_request(w, me, payload={"fail": True})
    response = await act(w, board[1], request_id, "approve")
    data = response.json()["data"]
    assert (data["status"], data["failure_reason"]) == (
        "execution_failed",
        "Tedarikçi pasifleştirildi.",
    )
    assert response.json()["message"] == "Onaylandı ama işlem yürütülemedi; talep edene bildirildi."
    assert "approval.execution_failed" in await kinds(w, me.id)


async def test_gorunurluk_ve_site_izolasyonu(world: World, board: list[Member]) -> None:
    w = world
    me = await kerem(w)
    ayse = board[0]
    await add_rule(w)
    request_id = await open_request(w, ayse)
    tech = await member(w, "teknik@test.local", "Teknik Kişi", "Teknik Personel")
    hidden = await w.api.get(w.url(f"/approvals/{request_id}"), headers=tech.headers)
    assert hidden.status_code == 404  # ilgisiz personel: talep "yok"
    assert (await detail(w, me, request_id))["title"].startswith("Satın alma")  # gözetim
    other_site = await w.api.get(
        w.url(f"/approvals/{request_id}", "yildiz-sitesi"), headers=w.headers
    )
    assert other_site.status_code == 404
    resident = await create_user(w.factory, "sakin@test.local")
    await add_site_membership(
        w.factory, w.site_id, resident.id, "Sakin", person_id=uuid.UUID(w.person_id)
    )
    sakin = await login_headers(w.api, "sakin@test.local")
    assert (await w.api.get(w.url("/approvals"), headers=sakin)).status_code == 403


async def test_kural_degisikligi_onaya_gider(world: World, board: list[Member]) -> None:
    w = world
    burak = board[1]
    guard = await add_rule(w, action_type="approval_rule_change")
    assert guard.status_code == 201, guard.text
    pending = await add_rule(
        w, amount_max="10000.00", steps=[{"roles": ["Yönetici"], "quorum": "any"}]
    )
    assert pending.status_code == 202, pending.text
    assert (
        pending.json()["message"]
        == "Kural değişikliği onaya gönderildi; onaylanınca yürürlüğe girer."
    )
    request_id = pending.json()["data"]["approval_request_id"]
    assert [r["action_type"] for r in (await w.get("/approval-rules")).json()] == [
        "approval_rule_change"
    ]
    approved = await act(w, burak, request_id, "approve")
    assert approved.json()["data"]["status"] == "executed", approved.text
    types = sorted(r["action_type"] for r in (await w.get("/approval-rules")).json())
    assert types == ["approval_rule_change", "purchase_request"]


async def test_hatirlatma_ve_eskalasyon(world: World, board: list[Member]) -> None:
    """ONY-02 S3: X=2, Y=5 gün; Y dolunca eskalasyon hedefi de karar verebilir; otomatik
    onay yok. İş tekrar güvenli."""
    w = world
    me = await kerem(w)
    ayse, burak = board[0], board[1]
    await add_rule(w, escalation_role="Yönetici")
    request_id = await open_request(w, ayse)

    async def run(days: int) -> svc.TimerResult:
        moment = NOW + timedelta(days=days)
        with site_scope(w.site_id):
            async with w.factory() as session, session.begin():
                site = await session.get(Site, w.site_id)
                assert site is not None
                return await svc.run_timers(session, site, now=moment, today=moment.date())

    assert (await run(1)).reminded == 0
    reminded = await run(2)
    assert (reminded.reminded, reminded.escalated) == (1, 0)
    assert (await run(2)).reminded == 0  # tekrar güvenli
    assert "approval.reminder" in await kinds(w, burak.id)
    assert request_id not in await tab(w, me, "pending")
    escalated = await run(5)
    assert escalated.escalated == 1
    shown = await detail(w, me, request_id)
    assert (shown["status"], shown["escalated"]) == ("pending", True)  # otomatik onay yok
    assert any(
        a["source"] == "escalation" and a["name"] == "Kerem YILDIRIM"
        for a in shown["steps"][0]["approvers"]
    )
    assert "approval.escalated" in await kinds(w, me.id)
    assert (await act(w, me, request_id, "approve")).json()["data"]["status"] == "executed"


# --- ONY-03 Yetki devri -------------------------------------------------------------


def period(start_days: int, end_days: int) -> dict[str, str]:
    base = datetime.now(UTC)
    return {
        "starts_at": (base + timedelta(days=start_days)).isoformat(),
        "ends_at": (base + timedelta(days=end_days)).isoformat(),
    }


async def test_yetki_devri_kurallari(world: World, board: list[Member]) -> None:
    w = world
    ayse = board[0]
    accountant = await member(w, "murat@test.local", "Murat Muhasebe", "Muhasebe")
    wrong_role = await w.post(
        "/delegations", {"to_user_id": str(accountant.id), "roles": ["Yönetici"], **period(0, 3)}
    )
    assert wrong_role.json()["error"]["fields"] == {
        "to_user_id": "Devralan aynı role sahip olmalı."
    }
    not_mine = await w.post(
        "/delegations",
        {"to_user_id": str(ayse.id), "roles": ["Yönetim Kurulu Üyesi"], **period(0, 3)},
    )
    assert not_mine.json()["error"]["fields"]["roles"] == "Yalnız kendi rolünüzü devredebilirsiniz."

    burak = board[1]
    created = await w.api.post(
        w.url("/delegations"),
        json={"to_user_id": str(burak.id), "roles": ["Yönetim Kurulu Üyesi"], **period(-1, 3)},
        headers=ayse.headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["state"] == "active"
    assert "delegation.started" in await kinds(w, burak.id)
    overlap = await w.api.post(
        w.url("/delegations"),
        json={"to_user_id": str(board[2].id), "roles": ["Yönetim Kurulu Üyesi"], **period(1, 2)},
        headers=ayse.headers,
    )
    assert (overlap.status_code, overlap.json()["error"]["code"]) == (409, "delegation_overlaps")
    received = await w.api.get(w.url("/delegations"), headers=burak.headers)
    assert [d["direction"] for d in received.json()] == ["received"]
    delegation_id = created.json()["data"]["id"]
    assert (
        await w.api.post(w.url(f"/delegations/{delegation_id}/end"), headers=burak.headers)
    ).status_code == 404
    ended = await w.api.post(w.url(f"/delegations/{delegation_id}/end"), headers=ayse.headers)
    assert ended.json()["data"]["state"] == "ended"
    again = await w.api.post(w.url(f"/delegations/{delegation_id}/end"), headers=ayse.headers)
    assert again.status_code == 409
    candidates = await w.api.get(w.url("/delegations/candidates"), headers=ayse.headers)
    assert sorted(c["name"] for c in candidates.json()) == [
        "Burak YK",
        "Cem YK",
        "Deniz YK",
        "Ece YK",
    ]


async def test_vekaleten_karar_ve_kendi_talebi(world: World, board: list[Member]) -> None:
    """Devralan, adım açıldıktan sonra role katıldığı için kendi hakkı yok; vekâleten karar
    verir (ONY-03 S2). Devredenin kendi talebini vekâleten onaylayamaz."""
    w = world
    me = await kerem(w)
    ayse = board[0]
    await add_rule(w, steps=[{"roles": ["Yönetici"], "quorum": "any"}])
    request_id = await open_request(w, ayse)
    own_request = await open_request(w, me)  # Kerem'in kendi talebi: onaycısı yok (N=0)
    mert = await member(w, "mert@test.local", "Mert YÖNETİCİ", "Yönetici")
    created = await w.post(
        "/delegations", {"to_user_id": str(mert.id), "roles": ["Yönetici"], **period(-1, 3)}
    )
    assert created.status_code == 201, created.text

    assert request_id in await tab(w, mert, "pending")
    shown = await detail(w, mert, request_id)
    assert (shown["can_decide"], shown["acting_for"]) == (True, "Kerem YILDIRIM")
    decided = await act(w, mert, request_id, "approve")
    assert decided.json()["data"]["status"] == "executed"
    [entry] = decided.json()["data"]["history"]
    assert (entry["user_name"], entry["on_behalf_of"]) == ("Mert YÖNETİCİ", "Kerem YILDIRIM")
    with site_scope(w.site_id):
        async with w.factory() as session:
            actor_type = await session.scalar(
                select(AuditLog.actor_type).where(AuditLog.entity == "approval_decisions")
            )
    assert actor_type == "delegate"

    blocked = await act(w, mert, own_request, "approve")
    assert blocked.status_code in (403, 404)
    unreachable = (await detail(w, me, own_request))["steps"][0]
    assert (unreachable["eligible"], unreachable["quorum_unreachable"]) == (0, True)
