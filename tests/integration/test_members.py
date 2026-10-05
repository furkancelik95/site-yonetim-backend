"""Site kullanıcıları ve roller — frontend servis isteği 12, issue #40 (gerçek PostgreSQL).

Kurgu `finance_world`: Kerem YILDIRIM iki sitede (Aksu, Yıldız) Yönetici.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import Organization, OrganizationMembership, Site
from site_yonetim.services import members as svc
from tests.integration.finance_world import World
from tests.integration.helpers import add_site_membership, create_user, login_headers

ACCOUNTING = {"full_name": "Selin Arı", "email": "Selin@Ornek.local", "role_key": "accounting"}


async def add(
    w: World, body: dict[str, Any] = ACCOUNTING, slug: str = "aksu-konaklari"
) -> httpx2.Response:
    return await w.api.post(w.url("/members", slug), json=body, headers=w.headers)


async def patch(
    w: World, member_id: str, body: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx2.Response:
    return await w.api.patch(
        w.url(f"/members/{member_id}"), json=body, headers=headers or w.headers
    )


async def members(w: World) -> list[dict[str, Any]]:
    response = await w.get("/members")
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()
    return items


async def test_roller_sabit_liste(world: World) -> None:
    roles = (await world.get("/roles")).json()
    assert [r["key"] for r in roles] == [
        "manager",
        "board",
        "auditor",
        "accounting",
        "security",
        "technical",
    ]
    assert roles[0]["name"] == "Yönetici"


async def test_yeni_kullanici_gecici_parolayla(world: World) -> None:
    [kerem] = await members(world)
    assert (kerem["full_name"], kerem["role_key"], kerem["source"]) == (
        "Kerem YILDIRIM",
        "manager",
        "site",
    )

    response = await add(world)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["message"] == (
        "Selin Arı Muhasebe olarak eklendi. Geçici parolayı kişiye elden iletin; "
        "ilk girişte değiştirecek."
    )
    member = body["data"]["member"]
    assert (member["email"], member["role_key"], member["role_name"]) == (
        "selin@ornek.local", "accounting", "Muhasebe"
    )  # fmt: skip
    password = body["data"]["temporary_password"]
    assert len(password) >= 16
    login = await world.api.post(
        "/api/v1/auth/login", json={"email": "selin@ornek.local", "password": password}
    )
    assert login.json()["must_change_password"] is True
    assert [m["full_name"] for m in await members(world)] == ["Kerem YILDIRIM", "Selin Arı"]

    # geçici parola denetim kaydına yazılmaz
    audit = (await world.get("/audit", params={"entity": "site_memberships"})).json()
    assert password not in str(audit)


async def test_kayitli_e_posta_mevcut_kullaniciya_rol(world: World) -> None:
    await create_user(world.factory, "selin@ornek.local", full_name="Selin Arı")
    response = await add(world)
    assert response.status_code == 201
    assert response.json()["data"]["temporary_password"] is None
    assert response.json()["message"] == (
        "Selin Arı kayıtlı bir kullanıcı; bu sitede Muhasebe rolü verildi. "
        "Mevcut parolasıyla giriş yapar."
    )
    again = await add(world)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_member"


async def test_dogrulama(world: World) -> None:
    response = await add(world, {"full_name": "A1", "email": "x", "role_key": "boss"})
    assert response.status_code == 422
    assert set(response.json()["error"]["fields"]) == {"full_name", "email", "role_key"}
    assert response.json()["error"]["fields"]["email"] == "Geçerli bir e-posta adresi girin."


async def test_rol_degistir_kapat_ac(world: World) -> None:
    created = (await add(world)).json()["data"]
    member_id = created["member"]["id"]
    selin = await login_headers(world.api, "selin@ornek.local", created["temporary_password"])

    changed = await patch(world, member_id, {"role_key": "board"})
    assert changed.json()["message"] == "Selin Arı rolü Yönetim Kurulu Üyesi olarak kaydedildi."
    assert changed.json()["data"]["role_key"] == "board"

    closed = await patch(world, member_id, {"is_active": False})
    assert closed.json()["message"] == "Selin Arı erişimi kapatıldı; oturumları sonlandırıldı."
    assert (await world.api.get("/api/v1/me", headers=selin)).status_code == 401

    reopened = await patch(world, member_id, {"is_active": True})
    assert reopened.json()["message"] == "Selin Arı erişimi yeniden açıldı."
    roles = await world.get("/audit", params={"entity": "site_memberships", "action": "update"})
    assert roles.json()["total"] == 3


async def test_kendi_kaydi_ve_son_yonetici(world: World) -> None:
    [kerem] = await members(world)
    own = await patch(world, kerem["id"], {"role_key": "accounting"})
    assert own.status_code == 409
    assert own.json()["error"]["code"] == "self_change"
    # başka bir yöneticinin elinden: tek yönetici düşürülemez (servis kuralı)
    with site_scope(world.site_id):
        async with world.factory() as session, session.begin():
            site = await session.get(Site, world.site_id)
            assert site is not None
            with pytest.raises(svc.MemberRuleError) as exc:
                await svc.update_member(
                    session, site, uuid.UUID(kerem["id"]), role_key=None, is_active=False,
                    actor_id=uuid.uuid4(), now=datetime.now(UTC),
                )  # fmt: skip
            assert exc.value.code == "last_manager"
    # ikinci yönetici varken ilki düşürülebilir
    second = (
        await add(
            world, {"full_name": "Deniz Ak", "email": "deniz@ornek.local", "role_key": "manager"}
        )
    ).json()
    deniz = await login_headers(
        world.api, "deniz@ornek.local", second["data"]["temporary_password"]
    )
    await world.api.post(
        "/api/v1/auth/change-password",
        json={
            "current_password": second["data"]["temporary_password"],
            "new_password": "Yeni-parola-1",
        },
        headers=deniz,
    )
    demoted = await patch(world, kerem["id"], {"role_key": "board"}, headers=deniz)
    assert demoted.status_code == 200, demoted.text


async def test_sirketten_gelen_erisim_buradan_degismez(world: World) -> None:
    owner = await create_user(world.factory, "sahip@kent.local", full_name="Can Sahip")
    async with world.factory() as session, session.begin():
        organization = Organization(name="Kent Yönetim")
        session.add(organization)
        await session.flush()
        site = await session.get(Site, world.site_id)
        assert site is not None
        site.organization_id = organization.id
        membership = OrganizationMembership(
            organization_id=organization.id, user_id=owner.id, role="Sahip"
        )
        session.add(membership)
    listed = {m["full_name"]: m for m in await members(world)}
    assert (listed["Can Sahip"]["source"], listed["Can Sahip"]["role_key"]) == (
        "organization",
        "manager",
    )
    derived = await patch(world, listed["Can Sahip"]["id"], {"is_active": False})
    assert derived.status_code == 409
    assert derived.json()["error"]["code"] == "derived_membership"


async def test_yetki_ve_site_izolasyonu(world: World) -> None:
    user = await create_user(world.factory, "muhasebe@test.local")
    await add_site_membership(world.factory, world.site_id, user.id, "Muhasebe")
    accounting = await login_headers(world.api, "muhasebe@test.local")
    assert (await world.api.get(world.url("/members"), headers=accounting)).status_code == 403
    assert (
        await world.api.post(world.url("/members"), json=ACCOUNTING, headers=accounting)
    ).status_code == 403

    member_id = (await add(world)).json()["data"]["member"]["id"]
    other = await world.api.patch(
        world.url(f"/members/{member_id}", "yildiz-sitesi"),
        json={"is_active": False},
        headers=world.headers,
    )
    assert other.status_code == 404
    assert (
        await world.api.get(world.url("/members", "yildiz-sitesi"), headers=world.headers)
    ).json()[0]["full_name"] == "Kerem YILDIRIM"
