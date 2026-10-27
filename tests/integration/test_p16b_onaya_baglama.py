"""P16b (PM kararı K21): kayıt onayı, iade, tahakkuk ters kaydı ve kritik rol ataması (üç yol)
onay kuralına bağlı. Kural yoksa bugünkü davranış (mevcut testler); kural varsa 202, onayda
yürütme, retde işlem yapılmaz / eski haline döner."""

import uuid
from decimal import Decimal

import httpx2

from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.p02_helpers import call, staff
from tests.integration.test_refunds import pay, refund
from tests.integration.test_registrations import apply, approve, code, pending
from tests.integration.test_roles import added, login_new

RULE = {
    "steps": [{"roles": ["Yönetici"], "quorum": "any"}],
    "remind_after_days": 2,
    "escalate_after_days": 5,
    "effective_from": "2026-06-20",
}


async def rule(w: World, action: str) -> None:
    """Kuralı yazar; onaycı talep açılmadan önce var olmalı (adımlar talep açılırken atanır)."""
    await approver(w)
    response = await w.post("/approval-rules", {"action_type": action, **RULE})
    assert response.status_code in (200, 201), response.text


_APPROVERS: dict[uuid.UUID, dict[str, str]] = {}


async def approver(w: World) -> dict[str, str]:
    if w.site_id not in _APPROVERS:
        _APPROVERS[w.site_id] = await staff(w, "onayci@test.local", "Yönetici")
    return _APPROVERS[w.site_id]


async def decide(
    w: World, who: dict[str, str], request_id: str, action: str = "approve"
) -> httpx2.Response:
    body = {"comment": "uygun değil"} if action == "reject" else {}
    return await call(w, "POST", f"/approvals/{request_id}/{action}", body, headers=who)


# --- Kayıt başvurusu -----------------------------------------------------------------------


async def _application(w: World, **overrides: str) -> str:
    assert (await apply(w, await code(w), **overrides)).status_code == 201
    [registration] = await pending(w)
    return str(registration["id"])


async def test_kayit_kurali_varsa_onaya_duser_onayda_yurutulur(world: World) -> None:
    await rule(world, "registration")
    reg_id = await _application(world)

    response = await approve(world, reg_id)

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["status"] == "approval_pending"
    request_id = data["approval_request_id"]
    [row] = (await world.get("/registrations")).json()["items"]
    assert (row["status"], row["approval_request_id"]) == ("pending", request_id)
    # Talep sonuçlanana kadar ne onay ne ret ne ikinci talep.
    again = await approve(world, reg_id)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "approval_pending")
    rejected = await world.post(f"/registrations/{reg_id}/reject", {"reason": "uygun değil"})
    assert rejected.status_code == 409

    who = await approver(world)
    own = await decide(world, world.headers, request_id)
    assert (own.status_code, own.json()["error"]["code"]) == (403, "own_request")
    decided = await decide(world, who, request_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    [row] = (await world.get("/registrations")).json()["items"]
    assert (row["status"], row["approval_request_id"]) == ("approved", None)


async def test_kayit_talebi_reddedilirse_basvuru_reddedilir(world: World) -> None:
    await rule(world, "registration")
    reg_id = await _application(world)
    request_id = (await approve(world, reg_id)).json()["data"]["approval_request_id"]
    who = await approver(world)

    assert (await decide(world, who, request_id, "reject")).status_code == 200

    [row] = (await world.get("/registrations")).json()["items"]
    assert row["status"] == "rejected"
    assert row["reject_reason"].startswith("Onay talebi reddedildi")


async def test_kayit_talebi_geri_cekilirse_basvuru_bekler(world: World) -> None:
    await rule(world, "registration")
    reg_id = await _application(world)
    request_id = (await approve(world, reg_id)).json()["data"]["approval_request_id"]

    withdrawn = await world.post(f"/approvals/{request_id}/withdraw", {})

    assert withdrawn.status_code == 200, withdrawn.text
    [row] = await pending(world)
    assert (row["status"], row["approval_request_id"]) == ("pending", None)
    assert (await approve(world, reg_id)).status_code == 202  # yeniden gönderilebilir


# --- İade ----------------------------------------------------------------------------------


async def _credit(w: World) -> None:
    await finalized_plan(w)
    await post_run(w)
    await pay(w, "1250.00")
    assert await w.balance() == Decimal("-250.00")


async def test_iade_kurali_varsa_onaya_duser_onayda_yazilir(world: World) -> None:
    await _credit(world)
    await rule(world, "refund")
    key = str(uuid.uuid4())

    response = await refund(world, "100.00", key=key)

    assert response.status_code == 202, response.text
    request_id = response.json()["data"]["approval_request_id"]
    assert await world.balance() == Decimal("-250.00")  # onaylanana kadar değişmez
    replay = await refund(world, "100.00", key=key)  # aynı anahtar: aynı talep
    assert replay.json()["data"]["approval_request_id"] == request_id
    over = await refund(world, "999.00")  # denetimler talepten önce: talep açılmaz
    assert over.status_code == 422

    who = await approver(world)
    decided = await decide(world, who, request_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    assert await world.balance() == Decimal("-150.00")


async def test_iade_talebi_reddedilirse_hic_yazilmaz(world: World) -> None:
    await _credit(world)
    await rule(world, "refund")
    request_id = (await refund(world, "100.00")).json()["data"]["approval_request_id"]
    who = await approver(world)

    assert (await decide(world, who, request_id, "reject")).status_code == 200

    assert await world.balance() == Decimal("-250.00")


async def test_iade_onaydan_once_alacak_biterse_yurutulemez(world: World) -> None:
    await _credit(world)
    await rule(world, "refund")
    request_id = (await refund(world, "250.00")).json()["data"]["approval_request_id"]
    await pay(world, "50.00", day="2026-06-19")  # alacak 300 olur: iade yine geçerli
    await rule(world, "payment_plan")  # ilgisiz kural iadeyi etkilemez
    who = await approver(world)

    decided = await decide(world, who, request_id)

    assert decided.json()["data"]["status"] == "executed"
    assert await world.balance() == Decimal("-50.00")


# --- Tahakkuk ters kaydı (tahakkuk kaydı onayı taslak akışında: test_finance_engine) --------


async def test_tahakkuk_ters_kaydi_onaya_duser_onayda_yazilir(world: World) -> None:
    await finalized_plan(world)
    run = (await post_run(world)).json()["data"]
    await rule(world, "charge_run_reversal")

    response = await world.post(f"/charge-runs/{run['id']}/reverse", {"reason": "hatalı tutar"})

    assert response.status_code == 202, response.text
    request_id = response.json()["data"]["approval_request_id"]
    assert await world.balance() == Decimal("1000.00")
    dup = await world.post(f"/charge-runs/{run['id']}/reverse", {"reason": "hatalı tutar"})
    assert (dup.status_code, dup.json()["error"]["code"]) == (409, "approval_pending")

    who = await approver(world)
    decided = await decide(world, who, request_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    assert await world.balance() == Decimal("0.00")
    assert (await world.get(f"/charge-runs/{run['id']}")).json()["status"] == "reversed"


async def test_tahakkuk_ters_kaydi_reddedilirse_kosu_yerinde_kalir(world: World) -> None:
    await finalized_plan(world)
    run = (await post_run(world)).json()["data"]
    await rule(world, "charge_run_reversal")
    reverse = await world.post(f"/charge-runs/{run['id']}/reverse", {"reason": "hatalı tutar"})
    who = await approver(world)

    rejected = await decide(world, who, reverse.json()["data"]["approval_request_id"], "reject")

    assert rejected.status_code == 200
    assert (await world.get(f"/charge-runs/{run['id']}")).json()["status"] == "posted"
    assert await world.balance() == Decimal("1000.00")


# --- Kritik rol ataması --------------------------------------------------------------------


async def _member(w: World, email: str, role_key: str) -> str:
    body = await added(w, email, "Selin Arı", role_key)
    await login_new(w, body)
    return str(body["member"]["id"])


async def _role_keys(w: World, member_id: str) -> set[str]:
    members = (await w.get("/members")).json()
    return {r["role_key"] for m in members if m["id"] == member_id for r in m["roles"]}


async def test_kritik_rol_atamasi_onaya_duser_kritik_olmayan_dogrudan(world: World) -> None:
    member_id = await _member(world, "selin@test.local", "security")
    await rule(world, "role_change")

    plain = await world.post(f"/members/{member_id}/role-assignments", {"role_key": "technical"})
    critical = await world.post(
        f"/members/{member_id}/role-assignments", {"role_key": "accounting"}
    )

    assert plain.status_code == 201, plain.text  # kritik izin yok: kural işlemez
    assert critical.status_code == 202, critical.text
    request_id = critical.json()["data"]["approval_request_id"]
    assert await _role_keys(world, member_id) == {"security", "technical"}

    who = await approver(world)
    decided = await decide(world, who, request_id)
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    assert await _role_keys(world, member_id) == {"security", "technical", "accounting"}


async def test_kritik_rol_talebi_reddedilirse_atanmaz(world: World) -> None:
    member_id = await _member(world, "selin@test.local", "security")
    await rule(world, "role_change")
    response = await world.post(
        f"/members/{member_id}/role-assignments", {"role_key": "accounting"}
    )
    who = await approver(world)

    rejected = await decide(world, who, response.json()["data"]["approval_request_id"], "reject")

    assert rejected.status_code == 200
    assert await _role_keys(world, member_id) == {"security"}
    bad = await world.post(f"/members/{member_id}/role-assignments", {"role_key": "yok"})
    assert bad.status_code == 422  # geçersiz rol talep açmaz


async def test_yeni_uyeye_kritik_rol_onaya_duser(world: World) -> None:
    await rule(world, "role_change")

    plain = await world.post(
        "/members", {"full_name": "Deniz Er", "email": "deniz@test.local", "role_key": "security"}
    )
    critical = await world.post(
        "/members",
        {"full_name": "Selin Arı", "email": "selin@test.local", "role_key": "accounting"},
    )

    assert plain.status_code == 201, plain.text  # kritik izin yok: kural işlemez
    assert critical.status_code == 202, critical.text
    assert critical.json()["data"]["approval_request_id"]
    members = {m["email"]: m for m in (await world.get("/members")).json()}
    assert members["selin@test.local"]["roles"] == []  # kullanıcı açıldı, rol onay bekliyor

    who = await approver(world)
    decided = await decide(world, who, critical.json()["data"]["approval_request_id"])
    assert decided.status_code == 200, decided.text
    assert decided.json()["data"]["status"] == "executed"
    member_id = members["selin@test.local"]["id"]
    assert await _role_keys(world, member_id) == {"accounting"}


async def test_yeni_uyeye_kritik_rol_kural_yoksa_dogrudan(world: World) -> None:
    response = await world.post(
        "/members",
        {"full_name": "Selin Arı", "email": "selin@test.local", "role_key": "accounting"},
    )
    assert response.status_code == 201, response.text
    assert [r["role_key"] for r in response.json()["data"]["member"]["roles"]] == ["accounting"]


async def test_ana_rolu_kritik_role_cevirme_onaya_duser(world: World) -> None:
    member_id = await _member(world, "selin@test.local", "security")
    await rule(world, "role_change")

    plain = await _patch(world, member_id, {"role_key": "technical"})
    critical = await _patch(world, member_id, {"role_key": "accounting"})

    assert plain.status_code == 200, plain.text
    assert critical.status_code == 202, critical.text
    assert await _role_keys(world, member_id) == {"technical"}  # onaya kadar eski ana rol

    who = await approver(world)
    decided = await decide(world, who, critical.json()["data"]["approval_request_id"])
    assert decided.json()["data"]["status"] == "executed"
    assert await _role_keys(world, member_id) == {"accounting"}  # ana rol değişti, eskisi kapandı


async def test_ana_rol_degisikligi_reddedilirse_eski_rol_kalir(world: World) -> None:
    member_id = await _member(world, "selin@test.local", "security")
    await rule(world, "role_change")
    response = await _patch(world, member_id, {"role_key": "accounting"})
    who = await approver(world)

    await decide(world, who, response.json()["data"]["approval_request_id"], "reject")

    assert await _role_keys(world, member_id) == {"security"}
    bad = await _patch(world, member_id, {"role_key": "yok"})
    assert bad.status_code == 422  # geçersiz rol talep açmaz


async def _patch(w: World, member_id: str, body: dict[str, str]) -> httpx2.Response:
    return await w.api.patch(w.url(f"/members/{member_id}"), json=body, headers=w.headers)
