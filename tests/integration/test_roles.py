"""Rol ve yetki modeli — PLT-09, PLT-02, ONY-03, §6.4; PM kararları P18 (çoklu rol, siteye özel
rol) ve K20 (rol sırası veridir). Gerçek PostgreSQL.

Kurgu `finance_world`: Kerem YILDIRIM iki sitede (Aksu, Yıldız) Yönetici; A bloğu, A-1 maliki Ayşe.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Plan, Site, SiteMembership, SiteRoleAssignment
from tests.integration.conftest import DatabaseUrls
from tests.integration.finance_world import World
from tests.integration.helpers import (
    DEFAULT_PASSWORD,
    add_site_membership,
    create_user,
    login_headers,
)
from tests.integration.test_approvals import (  # noqa: F401 — fixture
    EXECUTED,
    Member,
    act,
    detail,
    open_request,
    purchase_executor,
)

ROOT = Path(__file__).resolve().parents[2]
NEW_PASSWORD = DEFAULT_PASSWORD  # geçici paroladan sonra


async def added(w: World, email: str, name: str, role_key: str, **extra: Any) -> dict[str, Any]:
    response = await w.post(
        "/members", {"full_name": name, "email": email, "role_key": role_key, **extra}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()["data"]
    return body


async def login_new(w: World, email: str, temporary: str) -> dict[str, str]:
    headers = await login_headers(w.api, email, temporary)
    changed = await w.api.post(
        "/api/v1/auth/change-password",
        json={"current_password": temporary, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert changed.status_code == 204, changed.text
    return await login_headers(w.api, email, NEW_PASSWORD)


async def staff(w: World, email: str, name: str, role_key: str) -> tuple[str, dict[str, str]]:
    """(üye kimliği, oturum başlıkları)."""
    body = await added(w, email, name, role_key)
    return body["member"]["id"], await login_new(w, email, body["temporary_password"])


async def custom_role(
    w: World, name: str, permissions: list[str] | None, *, base: str = "accounting", **extra: Any
) -> dict[str, Any]:
    response = await w.post(
        "/roles", {"base_key": base, "name": name, "permissions": permissions, **extra}
    )
    assert response.status_code == 201, response.text
    role: dict[str, Any] = response.json()["data"]
    return role


async def my_site(w: World, headers: dict[str, str]) -> dict[str, Any] | None:
    me = (await w.api.get("/api/v1/me", headers=headers)).json()
    sites: list[dict[str, Any]] = me["sites"]
    return next((s for s in sites if s["slug"] == "aksu-konaklari"), None)


# --- Çoklu rol ve izin birleşimi ------------------------------------------------------


async def test_coklu_rol_izinler_birlesir_ve_kaldirilinca_duser(world: World) -> None:
    member_id, selin = await staff(world, "selin@test.local", "Selin Arı", "accounting")
    response = await world.post(f"/members/{member_id}/role-assignments", {"role_key": "security"})
    assert response.status_code == 201, response.text
    assert response.json()["message"] == "Selin Arı kişisine Güvenlik rolü atandı."
    member = response.json()["data"]
    assert (member["role_key"], member["role_name"]) == ("accounting", "Muhasebe")  # ana rol
    assert [r["role_name"] for r in member["roles"]] == ["Muhasebe", "Güvenlik"]

    site = await my_site(world, selin)
    assert site is not None
    assert site["role"] == "Muhasebe"
    assert [r["name"] for r in site["roles"]] == ["Muhasebe", "Güvenlik"]
    assert {"finance.cash.manage", "security.visitors"} <= set(site["permissions"])
    detail_ = (await world.api.get(world.url(""), headers=selin)).json()
    assert [r["key"] for r in detail_["roles"]] == ["accounting", "security"]

    security = next(r for r in member["roles"] if r["role_key"] == "security")
    removed = await world.api.delete(
        world.url(f"/members/{member_id}/role-assignments/{security['assignment_id']}"),
        headers=world.headers,
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["message"] == "Selin Arı kişisinin Güvenlik rolü kaldırıldı."
    site = await my_site(world, selin)
    assert site is not None
    assert "security.visitors" not in site["permissions"]  # bir sonraki istekte düşer

    again = await world.api.delete(
        world.url(f"/members/{member_id}/role-assignments/{security['assignment_id']}"),
        headers=world.headers,
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "assignment_ended")
    history = (await world.get("/role-assignments")).json()
    ended = next(h for h in history["items"] if h["role_key"] == "security")
    assert (ended["assigned_by"], ended["removed_by"]) == ("Kerem YILDIRIM", "Kerem YILDIRIM")
    assert history["total"] == 3  # Kerem (yönetici), Selin muhasebe, Selin güvenlik

    duplicate = await world.post(
        f"/members/{member_id}/role-assignments", {"role_key": "accounting"}
    )
    assert (duplicate.status_code, duplicate.json()["error"]["code"]) == (409, "assignment_exists")


async def test_atama_dogrulama(world: World) -> None:
    member_id, _ = await staff(world, "selin@test.local", "Selin Arı", "accounting")
    url = f"/members/{member_id}/role-assignments"
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    bad = await world.post(url, {"role_key": "yok", "scope_kind": "block", "ends_at": past})
    assert bad.status_code == 422
    assert set(bad.json()["error"]["fields"]) == {"role_key", "block_id", "ends_at"}
    resident = await world.post(url, {"role_key": "resident"})
    assert resident.json()["error"]["fields"] == {"role_key": "Rol seçin."}
    gate = await world.post(url, {"role_key": "security", "scope_kind": "gate"})
    assert gate.json()["error"]["fields"]["scope_kind"].startswith("Kapı tanımları henüz yok")


# --- Süreli atama -----------------------------------------------------------------------


async def test_suresi_dolan_atama_yetki_vermez_ileri_tarihli_baslamaz(world: World) -> None:
    user = await create_user(world.factory, "gecici@test.local", full_name="Geçici Denetçi")
    now = datetime.now(UTC)
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            session.add(SiteMembership(user_id=user.id))
            session.add(
                SiteRoleAssignment(
                    user_id=user.id,
                    role_key="auditor",
                    starts_at=now - timedelta(days=10),
                    ends_at=now - timedelta(minutes=1),
                )
            )
    headers = await login_headers(world.api, "gecici@test.local")
    assert await my_site(world, headers) is None
    assert (await world.api.get(world.url(""), headers=headers)).status_code == 404

    [listed] = [
        m for m in (await world.get("/members")).json() if m["email"] == "gecici@test.local"
    ]
    assert listed["roles"] == []  # bitmiş atama listede değil, geçmişte
    tomorrow = (now + timedelta(days=1)).isoformat()
    scheduled = await world.post(
        f"/members/{listed['id']}/role-assignments",
        {"role_key": "auditor", "starts_at": tomorrow},
    )
    assert scheduled.status_code == 201, scheduled.text
    assert scheduled.json()["message"].endswith("(başlangıç tarihinde etkinleşir).")
    assert scheduled.json()["data"]["roles"][0]["is_current"] is False
    assert await my_site(world, headers) is None  # henüz başlamadı

    # şimdi başlayıp bir saat sonra bitecek atama: hemen etkin
    soon = await world.post(
        f"/members/{listed['id']}/role-assignments",
        {"role_key": "board", "ends_at": (now + timedelta(hours=1)).isoformat()},
    )
    assert soon.status_code == 201, soon.text
    site = await my_site(world, headers)
    assert site is not None
    assert site["role"] == "Yönetim Kurulu Üyesi"
    assert site["roles"][0]["ends_at"] is not None


# --- Kendi izninden fazlasını atayamaz (PLT-09 S1) --------------------------------------


async def test_kendi_izninden_fazlasini_atayamaz_tanimlayamaz_kaldiramaz(world: World) -> None:
    hr_like = await custom_role(
        world, "Kullanıcı Sorumlusu", ["members.manage", "people.read"], base="hr"
    )
    reader = await custom_role(world, "Kişi Okuyucu", ["people.read"], base="hr", level=5)
    _, ayse = await staff(world, "ayse.k@test.local", "Ayşe Kaya", hr_like["key"])

    more = await world.api.post(
        world.url("/members"),
        json={"full_name": "Can Muhasebe", "email": "can@test.local", "role_key": "accounting"},
        headers=ayse,
    )
    assert (more.status_code, more.json()["error"]["code"]) == (403, "exceeds_own_permissions")
    assert more.json()["error"]["message"] == (
        "Kendi yetkinizden fazla izin içeren rol atayamazsınız."
    )
    define = await world.api.post(
        world.url("/roles"),
        json={"base_key": "accounting", "name": "Gizli Muhasebe"},
        headers=ayse,
    )
    assert (define.status_code, define.json()["error"]["code"]) == (403, "exceeds_own_permissions")

    ok = await world.api.post(
        world.url("/members"),
        json={"full_name": "Can Okur", "email": "can@test.local", "role_key": reader["key"]},
        headers=ayse,
    )
    assert ok.status_code == 201, ok.text

    [kerem] = [
        m for m in (await world.get("/members")).json() if m["full_name"] == "Kerem YILDIRIM"
    ]
    remove = await world.api.delete(
        world.url(f"/members/{kerem['id']}/role-assignments/{kerem['roles'][0]['assignment_id']}"),
        headers=ayse,
    )
    assert (remove.status_code, remove.json()["error"]["code"]) == (403, "exceeds_own_permissions")


async def test_son_yonetici_kaldirilamaz(world: World) -> None:
    [kerem] = (await world.get("/members")).json()
    second_id, deniz = await staff(world, "deniz@test.local", "Deniz Ak", "manager")
    first = kerem["roles"][0]["assignment_id"]
    ok = await world.api.delete(
        world.url(f"/members/{kerem['id']}/role-assignments/{first}"), headers=deniz
    )
    assert ok.status_code == 200, ok.text
    # Deniz artık tek yönetici; kendi rolünü kaldıramaz, Kerem (rolsüz) de yönetemez
    [deniz_member] = [m for m in (await world.api.get(
        world.url("/members"), headers=deniz
    )).json() if m["id"] == second_id]  # fmt: skip
    own = await world.api.delete(
        world.url(
            f"/members/{second_id}/role-assignments/{deniz_member['roles'][0]['assignment_id']}"
        ),
        headers=deniz,
    )
    assert (own.status_code, own.json()["error"]["code"]) == (409, "self_change")
    assert (await world.get("/members")).status_code == 404  # Kerem'in bu sitede rolü kalmadı


# --- Siteye özel rol, rol düzeyi (K20) --------------------------------------------------


async def test_ozel_rol_kopyala_ozellestir_ve_duzey(world: World) -> None:
    role = await custom_role(world, "Kasa Sorumlusu", ["finance.cash.read", "finance.cash.manage"])
    assert (role["kind"], role["base_key"], role["level"]) == ("custom", "accounting", 3)
    duplicate = await world.post("/roles", {"base_key": "manager", "name": "kasa sorumlusu"})
    assert duplicate.json()["error"]["fields"]["name"].startswith("Bu adla bir rol zaten var")
    copied = await custom_role(world, "Yardımcı Yönetici", None, base="manager", level=2)
    assert "members.manage" in copied["permissions"]
    assert copied["is_critical"]

    level = await world.api.patch(
        world.url("/roles/security"), json={"level": 2}, headers=world.headers
    )
    assert level.status_code == 200, level.text
    assert level.json()["data"]["level"] == 2
    locked = await world.api.patch(
        world.url("/roles/security"), json={"permissions": ["finance.read"]}, headers=world.headers
    )
    assert (locked.status_code, locked.json()["error"]["code"]) == (409, "system_role")

    member_id, _ = await staff(world, "kasa@test.local", "Kasa Kişi", role["key"])
    in_use = await world.api.patch(
        world.url(f"/roles/{role['key']}"), json={"is_active": False}, headers=world.headers
    )
    assert (in_use.status_code, in_use.json()["error"]["code"]) == (409, "role_in_use")
    edited = await world.api.patch(
        world.url(f"/roles/{role['key']}"),
        json={"permissions": ["finance.cash.read"], "description": "Yalnız okur"},
        headers=world.headers,
    )
    assert edited.json()["data"]["permissions"] == ["finance.cash.read"]
    roles = {r["key"]: r for r in (await world.get("/roles")).json()}
    assert roles[role["key"]]["holder_count"] == 1
    assert roles["security"]["level"] == 2
    groups = (await world.api.get("/api/v1/permissions", headers=world.headers)).json()
    assert groups[0]["group"] == "Finans"
    assert member_id


async def test_rol_yonetimi_yetki_ve_site_izolasyonu(world: World) -> None:
    role = await custom_role(world, "Kasa Sorumlusu", ["finance.cash.read"])
    yildiz = await world.api.get(world.url("/roles", "yildiz-sitesi"), headers=world.headers)
    assert role["key"] not in {r["key"] for r in yildiz.json()}
    other = await world.api.patch(
        world.url(f"/roles/{role['key']}", "yildiz-sitesi"),
        json={"level": 2},
        headers=world.headers,
    )
    assert other.status_code == 404
    member_id, guard = await staff(world, "guard@test.local", "Recep Er", "security")
    assert (await world.api.get(world.url("/roles"), headers=guard)).status_code == 403
    assert (
        await world.api.post(
            world.url("/roles"), json={"base_key": "security", "name": "X Rol"}, headers=guard
        )
    ).status_code == 403
    [member] = [m for m in (await world.get("/members")).json() if m["id"] == member_id]
    cross = await world.api.delete(
        world.url(
            f"/members/{member_id}/role-assignments/{member['roles'][0]['assignment_id']}",
            "yildiz-sitesi",
        ),
        headers=world.headers,
    )
    assert cross.status_code == 404
    history = await world.api.get(
        world.url("/role-assignments", "yildiz-sitesi"), headers=world.headers
    )
    assert [h["user_name"] for h in history.json()["items"]] == ["Kerem YILDIRIM"]


# --- Onay: özel rol adımı, devir (aynı veya üst rol), eskalasyon önerisi --------------------


async def test_ozel_rol_ile_onay_adimi(world: World) -> None:
    role = await custom_role(
        world, "Site Müdürü", ["approvals.rules.read", "finance.read"], base="manager", level=1
    )
    member_id, mert_headers = await staff(world, "mert@test.local", "Mert Müdür", role["key"])
    rule = await world.post(
        "/approval-rules",
        {
            "action_type": "purchase_request",
            "steps": [{"roles": ["site müdürü"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": world.today[0].isoformat(),
        },
    )
    assert rule.status_code == 201, rule.text
    assert rule.json()["data"]["rule"]["current"]["steps"][0]["roles"] == ["Site Müdürü"]
    me = (await world.api.get("/api/v1/me", headers=world.headers)).json()
    kerem = Member(uuid.UUID(me["user_id"]), me["full_name"], world.headers)
    request_id = await open_request(world, kerem)
    mert = Member(uuid.UUID(member_id), "Mert Müdür", mert_headers)
    shown = await detail(world, mert, request_id)
    assert shown["steps"][0]["approvers"][0]["role"] == "Site Müdürü"
    decided = await act(world, mert, request_id, "approve")
    assert decided.json()["data"]["status"] == "executed", decided.text
    assert EXECUTED


async def test_devir_ust_role_ve_vekaleten_karar(world: World) -> None:
    murat_id, murat = await staff(world, "murat@test.local", "Murat Muhasebe", "accounting")
    _, ayse = await staff(world, "ayse.yk@test.local", "Ayşe YK", "board")
    await staff(world, "recep@test.local", "Recep Güvenlik", "security")
    response = await world.api.get(world.url("/delegations/candidates"), headers=murat)
    body = response.json()
    assert body["my_roles"] == ["Muhasebe"]
    assert {c["name"]: c["can_take"] for c in body["candidates"]} == {
        "Ayşe YK": ["Muhasebe"],
        "Kerem YILDIRIM": ["Muhasebe"],
    }  # güvenlik (alt düzey) aday değil
    ayse_id = next(c["user_id"] for c in body["candidates"] if c["name"] == "Ayşe YK")

    rule = await world.post(
        "/approval-rules",
        {
            "action_type": "purchase_request",
            "steps": [{"roles": ["Muhasebe"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": world.today[0].isoformat(),
        },
    )
    assert rule.status_code == 201, rule.text
    now = datetime.now(UTC)
    delegation = await world.api.post(
        world.url("/delegations"),
        json={
            "to_user_id": ayse_id,
            "roles": ["Muhasebe"],
            "starts_at": (now - timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(days=3)).isoformat(),
        },
        headers=murat,
    )
    assert delegation.status_code == 201, delegation.text
    me = (await world.api.get("/api/v1/me", headers=world.headers)).json()
    request_id = await open_request(world, Member(uuid.UUID(me["user_id"]), "Kerem", world.headers))
    decided = await act(world, Member(uuid.UUID(ayse_id), "Ayşe YK", ayse), request_id, "approve")
    assert decided.status_code == 200, decided.text
    [entry] = decided.json()["data"]["history"]
    assert (entry["user_name"], entry["on_behalf_of"]) == ("Ayşe YK", "Murat Muhasebe")
    assert murat_id


async def test_eskalasyon_onerisi_rol_sirasindan(world: World) -> None:
    await staff(world, "ayse.yk@test.local", "Ayşe YK", "board")
    suggestion = await world.get(
        "/approval-rules/escalation-suggestion", params={"roles": ["Muhasebe"]}
    )
    assert suggestion.json() == {"role": "Yönetim Kurulu Üyesi"}
    top = await world.get("/approval-rules/escalation-suggestion", params={"roles": ["Yönetici"]})
    assert top.json() == {"role": None}


# --- Blok kapsamı (PLT-09 kapsam: güvenlik yalnız kendi bloğunda) ----------------------------


async def test_blok_kapsamli_guvenlik_yalniz_kendi_blogunda(world: World) -> None:
    async with world.factory() as session, session.begin():
        plan = Plan(name="Pro", max_units=500, allowed_modules=["finance", "packages", "visitors"])
        session.add(plan)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.plan_id = plan.id
    for key in ("packages", "visitors"):
        assert (await world.post(f"/modules/{key}/toggle")).status_code == 200
    blocks = (await world.get("/blocks")).json()["items"]
    block_a = blocks[0]["id"]
    block_b = (await world.post("/blocks", {"name": "B"})).json()["data"]["id"]
    unit_b = (await world.post("/units", {"block_id": block_b, "number": "7"})).json()["data"]["id"]

    body = await added(
        world, "kapi@test.local", "Kapı Görevlisi", "security", scope_kind="block", block_id=block_a
    )
    guard = await login_new(world, "kapi@test.local", body["temporary_password"])
    assert body["member"]["roles"][0]["block_name"] == "A"
    site_ = await my_site(world, guard)
    assert site_ is not None
    assert "security.packages" in site_["permissions"]  # ekran açılır …
    assert {s["permission"] for s in site_["scoped_permissions"]} == {
        "security.packages",
        "security.visitors",
    }  # fmt: skip  … ama yalnız A bloğunda
    assert "requests.create" not in site_["permissions"]  # blok kapsamı tesis izni vermez

    inside = await world.api.post(
        world.url("/packages"), json={"unit_id": world.unit_id}, headers=guard
    )
    assert inside.status_code == 201, inside.text
    outside = await world.api.post(world.url("/packages"), json={"unit_id": unit_b}, headers=guard)
    assert outside.status_code == 403
    assert outside.json()["error"]["message"] == "Bu bölüm yetkili olduğunuz blokların dışında."
    await world.post("/packages", {"unit_id": unit_b})  # yönetici B'ye kargo kaydeder
    listed = await world.api.get(world.url("/packages"), headers=guard)
    assert [p["unit_id"] for p in listed.json()["items"]] == [world.unit_id]
    found = await world.api.get(world.url("/units/lookup"), params={"q": "7"}, headers=guard)
    assert found.json() == []
    assert len((await world.get("/units/lookup", params={"q": "7"})).json()) == 1


# --- Göç: mevcut üyelikler atamaya taşınır ------------------------------------------------


def _config(urls: DatabaseUrls) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", urls.admin.replace("%", "%%"))
    return config


async def test_goc_mevcut_uyelikleri_atamaya_tasir(
    database_urls: DatabaseUrls, admin_engine: AsyncEngine
) -> None:
    config = _config(database_urls)
    previous = ScriptDirectory.from_config(config).get_revision("0041").down_revision
    assert isinstance(previous, str)
    await asyncio.to_thread(command.downgrade, config, previous)
    try:
        site_id, manager, guard, resident = (uuid.uuid4() for _ in range(4))
        async with admin_engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.all_sites', 'on', true)"))
            await connection.execute(
                text(
                    "INSERT INTO sites (id, name, name_key, slug, property_kind, "
                    "fiscal_year_start_month) VALUES (:id, 'Göç', 'göç', 'goc', 'residential', 1)"
                ),
                {"id": site_id},
            )
            for user_id, role, active in (
                (manager, "Yönetici", True),
                (guard, "Güvenlik", False),
                (resident, "Sakin", True),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, full_name, kind, "
                        "is_active, is_platform_admin, failed_login_count, must_change_password) "
                        "VALUES (:id, :email, 'x', 'Göç Kişi', 'staff', true, false, 0, false)"
                    ),
                    {"id": user_id, "email": f"{user_id}@goc.local"},
                )
                await connection.execute(
                    text(
                        "INSERT INTO site_memberships (id, site_id, user_id, role, is_active) "
                        "VALUES (gen_random_uuid(), :site, :user, :role, :active)"
                    ),
                    {"site": site_id, "user": user_id, "role": role, "active": active},
                )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    async with admin_engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.all_sites', 'on', true)"))
        rows = (
            await connection.execute(
                text(
                    "SELECT user_id, role_key, scope_kind, starts_at, ends_at "
                    "FROM site_role_assignments WHERE site_id = :site"
                ),
                {"site": site_id},
            )
        ).all()
        statuses: dict[uuid.UUID, bool] = dict(
            (
                await connection.execute(
                    text("SELECT user_id, is_active FROM site_memberships WHERE site_id = :site"),
                    {"site": site_id},
                )
            ).all()
        )
    assert {(r.user_id, r.role_key, r.scope_kind, r.starts_at, r.ends_at) for r in rows} == {
        (manager, "manager", "site", None, None),
        (guard, "security", "site", None, None),
        (resident, "resident", "site", None, None),
    }
    assert statuses == {manager: True, guard: False, resident: True}  # üyelik durumu korunur


async def test_yardimci_uyelik_sablon_adiyla(world: World) -> None:
    """Testlerin ortak yardımcısı şablon adıyla da çalışır (paralel paketlerin testleri)."""
    user = await create_user(world.factory, "aux@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Teknik Personel")
    headers = await login_headers(world.api, "aux@test.local")
    site = await my_site(world, headers)
    assert site is not None
    assert site["role"] == "Teknik Personel"
