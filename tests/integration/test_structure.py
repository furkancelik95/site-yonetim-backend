"""Site yapısı uçları — docs/03 §3–§5, docs/06 §2.4 (gerçek PostgreSQL, RLS'e tabi rol).

Trello "BE · Dilim 3 · Site, blok, daire, sakin" — bitti ölçütü: iki ayrı site açılır,
birinin dairesi diğerinde görünmez.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_today
from site_yonetim.services.provisioning import provision_site
from tests.integration.conftest import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class World:
    a: str  # slug
    b: str
    headers: dict[str, str]  # iki sitede de Yönetici


@pytest.fixture
async def world(
    api: httpx2.AsyncClient, api_app: FastAPI, session_factory: Factory, admin_engine: object
) -> World:
    api_app.dependency_overrides[get_today] = lambda: date(2026, 10, 1)
    site_ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            site_ids.append((await provision_site(session, name=name)).id)
    user = await create_user(session_factory, "yonetici@test.local")
    for site_id in site_ids:
        await add_site_membership(session_factory, site_id, user.id, "Yönetici")
    return World("aksu-konaklari", "yildiz-sitesi", await login_headers(api, "yonetici@test.local"))


async def _post(
    api: httpx2.AsyncClient, w: World, slug: str, path: str, body: Mapping[str, object]
) -> httpx2.Response:
    return await api.post(f"/api/v1/sites/{slug}{path}", json=body, headers=w.headers)


async def _block(api: httpx2.AsyncClient, w: World, slug: str, name: str = "A") -> str:
    response = await _post(api, w, slug, "/blocks", {"name": name, "has_elevator": True})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


async def _unit(
    api: httpx2.AsyncClient, w: World, slug: str, block_id: str, number: str = "12"
) -> str:
    response = await _post(api, w, slug, "/units", {"block_id": block_id, "number": number})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


# --- Bitti ölçütü -------------------------------------------------------------


async def test_bir_sitenin_dairesi_digerinde_gorunmez(
    api: httpx2.AsyncClient, world: World
) -> None:
    unit_a = await _unit(api, world, world.a, await _block(api, world, world.a))

    list_a = (await api.get(f"/api/v1/sites/{world.a}/units", headers=world.headers)).json()
    list_b = (await api.get(f"/api/v1/sites/{world.b}/units", headers=world.headers)).json()
    detail_in_b = await api.get(f"/api/v1/sites/{world.b}/units/{unit_a}", headers=world.headers)
    patch_in_b = await api.patch(
        f"/api/v1/sites/{world.b}/units/{unit_a}", json={"floor": 3}, headers=world.headers
    )

    assert [u["display_name"] for u in list_a["items"]] == ["A-12"]
    assert list_b == {"items": [], "page": 1, "page_size": 50, "total": 0}
    assert detail_in_b.status_code == 404
    assert patch_in_b.status_code == 404


async def test_baska_sitenin_blogu_ile_bolum_eklenemez(
    api: httpx2.AsyncClient, world: World
) -> None:
    block_a = await _block(api, world, world.a)

    response = await _post(api, world, world.b, "/units", {"block_id": block_a, "number": "1"})

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"block_id": "Seçilen blok bulunamadı."}


# --- Blok ---------------------------------------------------------------------


async def test_blok_turkce_harf_duyarsiz_benzersiz(api: httpx2.AsyncClient, world: World) -> None:
    await _block(api, world, world.a, "Işık")

    again = await _post(api, world, world.a, "/blocks", {"name": "IŞIK"})
    other_site = await _post(api, world, world.b, "/blocks", {"name": "IŞIK"})

    assert again.status_code == 409
    assert other_site.status_code == 201


async def test_blok_listesi_bolum_sayisiyla(api: httpx2.AsyncClient, world: World) -> None:
    block = await _block(api, world, world.a)
    for number in ("1", "2", "3"):
        await _unit(api, world, world.a, block, number)

    body = (await api.get(f"/api/v1/sites/{world.a}/blocks", headers=world.headers)).json()

    assert body["total"] == 1
    assert body["items"][0]["unit_count"] == 3


async def test_blok_guncellenir(api: httpx2.AsyncClient, world: World) -> None:
    block = await _block(api, world, world.a)

    response = await api.patch(
        f"/api/v1/sites/{world.a}/blocks/{block}", json={"name": "Kule 1"}, headers=world.headers
    )

    assert response.json()["data"]["name"] == "Kule 1"


async def test_daire_tipleri_kurulumdan_gelir(api: httpx2.AsyncClient, world: World) -> None:
    body = (await api.get(f"/api/v1/sites/{world.a}/unit-types", headers=world.headers)).json()

    assert [(t["name"], t["weight"]) for t in body["items"]] == [
        ("1+1", "1.0000"),
        ("2+1", "1.3500"),
        ("3+1", "1.7000"),
    ]


# --- Bölüm --------------------------------------------------------------------


async def test_ayni_blokta_ayni_numara_409(api: httpx2.AsyncClient, world: World) -> None:
    block = await _block(api, world, world.a)
    await _unit(api, world, world.a, block, "5")

    response = await _post(api, world, world.a, "/units", {"block_id": block, "number": "5"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unit_already_exists"


async def test_arsa_payi_eksik_ya_da_hatali_422(api: httpx2.AsyncClient, world: World) -> None:
    block = await _block(api, world, world.a)

    missing = await _post(
        api,
        world,
        world.a,
        "/units",
        {"block_id": block, "number": "1", "land_share_numerator": 45},
    )
    too_big = await _post(
        api,
        world,
        world.a,
        "/units",
        {
            "block_id": block,
            "number": "2",
            "land_share_numerator": 50,
            "land_share_denominator": 40,
        },
    )

    assert missing.status_code == 422
    assert "land_share_denominator" in missing.json()["error"]["fields"]
    assert too_big.status_code == 422


async def test_bolum_listesi_sayfali_dogal_sirali_ve_aranabilir(
    api: httpx2.AsyncClient, world: World
) -> None:
    block = await _block(api, world, world.a)
    for number in ("10", "2", "1"):
        await _unit(api, world, world.a, block, number)

    first = (
        await api.get(f"/api/v1/sites/{world.a}/units?page_size=2", headers=world.headers)
    ).json()
    search = (await api.get(f"/api/v1/sites/{world.a}/units?q=A-10", headers=world.headers)).json()

    assert [u["number"] for u in first["items"]] == ["1", "2"]
    assert first["total"] == 3
    assert [u["display_name"] for u in search["items"]] == ["A-10"]


async def test_bolum_pasiflestirilir_silinmez(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    response = await api.patch(
        f"/api/v1/sites/{world.a}/units/{unit}", json={"is_active": False}, headers=world.headers
    )
    active = (
        await api.get(f"/api/v1/sites/{world.a}/units?is_active=true", headers=world.headers)
    ).json()

    assert response.status_code == 200
    assert "pasifleştirildi" in response.json()["message"]
    assert active["total"] == 0
    assert (
        await api.get(f"/api/v1/sites/{world.a}/units/{unit}", headers=world.headers)
    ).status_code == 200


async def test_bolum_zorunlu_alani_bosaltilamaz(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    response = await api.patch(
        f"/api/v1/sites/{world.a}/units/{unit}", json={"number": None}, headers=world.headers
    )

    assert response.status_code == 422


# --- Kişi ---------------------------------------------------------------------


async def test_kisi_adi_turkce_bicimlenir_telefon_e164(
    api: httpx2.AsyncClient, world: World
) -> None:
    response = await _post(
        api,
        world,
        world.a,
        "/people",
        {
            "first_name": "aYşE",
            "last_name": "yılmaz",
            "phone": "0532 123 45 67",
            "email": "Ayse@Ornek.com",
        },
    )

    person = response.json()["data"]
    assert (person["first_name"], person["last_name"]) == ("Ayşe", "YILMAZ")
    assert person["phone"] == "+905321234567"
    assert person["email"] == "ayse@ornek.com"


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        ({"first_name": "Ayşe2", "last_name": "Yılmaz"}, "body", "rakam"),
        ({"first_name": "A", "last_name": "Yılmaz"}, "body", "2–40"),
        ({"first_name": "Ayşe", "last_name": "Yılmaz", "phone": "0212 123 45 67"}, "phone", "cep"),
    ],
)
async def test_kisi_dogrulama_hatalari_turkce(
    api: httpx2.AsyncClient, world: World, body: dict[str, str], field: str, message: str
) -> None:
    response = await _post(api, world, world.a, "/people", body)

    assert response.status_code == 422
    assert message in response.json()["error"]["fields"][field]


async def test_kisi_aramasi_turkce_harf_duyarsiz(api: httpx2.AsyncClient, world: World) -> None:
    await _post(api, world, world.a, "/people", {"first_name": "Işık", "last_name": "Çelik"})
    await _post(api, world, world.a, "/people", {"first_name": "İsmail", "last_name": "Er"})

    async def find(q: str) -> list[str]:
        body = (
            await api.get(f"/api/v1/sites/{world.a}/people", params={"q": q}, headers=world.headers)
        ).json()
        return [p["full_name"] for p in body["items"]]

    assert await find("IŞIK") == ["Işık ÇELİK"]
    assert await find("ismail") == ["İsmail ER"]
    assert await find("%") == []  # joker karakter kaçışlanır


async def test_kisi_guncellenir(api: httpx2.AsyncClient, world: World) -> None:
    person = (
        await _post(api, world, world.a, "/people", {"first_name": "Ayşe", "last_name": "Yılmaz"})
    ).json()["data"]

    response = await api.patch(
        f"/api/v1/sites/{world.a}/people/{person['id']}",
        json={"last_name": "kaya", "phone": "5321234567"},
        headers=world.headers,
    )

    assert response.json()["data"]["full_name"] == "Ayşe KAYA"
    assert response.json()["data"]["phone"] == "+905321234567"


# --- Bölüm–kişi ilişkisi ve cari hesaplar ------------------------------------


async def _party(
    api: httpx2.AsyncClient, w: World, unit: str, role: str, first: str, **extra: object
) -> httpx2.Response:
    body: dict[str, object] = {
        "role": role,
        "start_date": "2026-01-01",
        "person": {"first_name": first, "last_name": "Test"},
        **extra,
    }
    return await _post(api, w, w.a, f"/units/{unit}/parties", body)


async def test_malik_ve_kiraci_cari_hesaplari_acilir(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    owner = await _party(api, world, unit, "owner", "Ayşe")
    tenant = await _party(api, world, unit, "tenant", "Mehmet", start_date="2026-06-01")

    assert [a["reference_code"] for a in owner.json()["data"]["opened_accounts"]] == [
        "A12-M",
        "A12-O",
    ]
    assert "2 cari hesap açıldı (A12-M, A12-O)" in owner.json()["message"]
    assert [a["reference_code"] for a in tenant.json()["data"]["opened_accounts"]] == ["A12-K"]

    detail = (await api.get(f"/api/v1/sites/{world.a}/units/{unit}", headers=world.headers)).json()
    assert detail["owner_names"] == ["Ayşe TEST"]
    assert detail["tenant_names"] == ["Mehmet TEST"]
    assert {a["kind"] for a in detail["accounts"]} == {"owner", "occupant"}


async def test_kiraci_varken_eklenen_malige_oturan_hesabi_acilmaz(
    api: httpx2.AsyncClient, world: World
) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    await _party(api, world, unit, "tenant", "Mehmet")

    owner = await _party(api, world, unit, "owner", "Ayşe")

    assert [a["reference_code"] for a in owner.json()["data"]["opened_accounts"]] == ["A12-M"]


async def test_referans_kodu_cakisirsa_sayi_eklenir(api: httpx2.AsyncClient, world: World) -> None:
    unit_a12 = await _unit(api, world, world.a, await _block(api, world, world.a, "A"), "12")
    unit_a1_2 = await _unit(api, world, world.a, await _block(api, world, world.a, "A1"), "2")
    await _party(api, world, unit_a12, "owner", "Ayşe")

    second = await _party(api, world, unit_a1_2, "owner", "Fatma")

    assert [a["reference_code"] for a in second.json()["data"]["opened_accounts"]] == [
        "A12-M2",
        "A12-O2",
    ]


async def test_hisseli_mulkiyet_yuzde_yuzu_asamaz(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    await _party(api, world, unit, "owner", "Ayşe", share_percent="60")

    ok = await _party(api, world, unit, "owner", "Fatma", share_percent="40")
    too_much = await _party(api, world, unit, "owner", "Zeynep", share_percent="10")

    assert ok.status_code == 201
    assert too_much.status_code == 409
    assert too_much.json()["error"]["code"] == "owner_shares_exceed"


async def test_ayni_kisi_ikinci_kez_malik_eklenemez(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    person_id = (await _party(api, world, unit, "owner", "Ayşe", share_percent="50")).json()[
        "data"
    ]["party"]["person"]["id"]

    again = await _post(
        api,
        world,
        world.a,
        f"/units/{unit}/parties",
        {
            "role": "owner",
            "start_date": "2026-02-01",
            "person_id": person_id,
            "share_percent": "10",
        },
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "party_already_active"


async def test_kiraci_cikisi_bitis_tarihiyle_gecmis_korunur(
    api: httpx2.AsyncClient, world: World
) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    party = (await _party(api, world, unit, "tenant", "Mehmet")).json()["data"]["party"]

    ended = await _post(
        api, world, world.a, f"/units/{unit}/parties/{party['id']}/end", {"end_date": "2026-09-30"}
    )
    twice = await _post(
        api, world, world.a, f"/units/{unit}/parties/{party['id']}/end", {"end_date": "2026-09-30"}
    )

    assert ended.status_code == 200
    assert ended.json()["data"]["end_date"] == "2026-09-30"
    assert ended.json()["data"]["is_current"] is False
    assert twice.status_code == 409
    detail = (await api.get(f"/api/v1/sites/{world.a}/units/{unit}", headers=world.headers)).json()
    assert detail["tenant_names"] == []
    assert [p["end_date"] for p in detail["parties"]] == ["2026-09-30"]  # kayıt silinmedi


async def test_bitis_baslangictan_once_olamaz(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    party = (await _party(api, world, unit, "tenant", "Mehmet")).json()["data"]["party"]

    response = await _post(
        api, world, world.a, f"/units/{unit}/parties/{party['id']}/end", {"end_date": "2025-12-31"}
    )

    assert response.status_code == 422
    assert "end_date" in response.json()["error"]["fields"]


async def test_kisi_ya_person_id_ya_person_ile_verilir(
    api: httpx2.AsyncClient, world: World
) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    response = await _post(
        api, world, world.a, f"/units/{unit}/parties", {"role": "owner", "start_date": "2026-01-01"}
    )

    assert response.status_code == 422


async def test_baska_sitenin_kisisi_baglanamaz(api: httpx2.AsyncClient, world: World) -> None:
    person_b = (
        await _post(api, world, world.b, "/people", {"first_name": "Ali", "last_name": "Veli"})
    ).json()["data"]["id"]
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    response = await _post(
        api,
        world,
        world.a,
        f"/units/{unit}/parties",
        {"role": "owner", "start_date": "2026-01-01", "person_id": person_b},
    )

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"person_id": "Seçilen kişi bulunamadı."}


# --- Yetki --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "method", "path", "expected"),
    [
        ("Güvenlik", "GET", "/units", 403),
        ("Güvenlik", "GET", "/people", 403),
        ("Denetçi", "GET", "/units", 403),  # kişisel veri görmez, bölüm listesi de yok
        ("Yönetim Kurulu Üyesi", "GET", "/units", 200),
        ("Yönetim Kurulu Üyesi", "POST", "/blocks", 403),  # görür, değiştirmez
        ("Muhasebe", "POST", "/people", 403),
    ],
)
async def test_izinler(
    api: httpx2.AsyncClient,
    world: World,
    session_factory: Factory,
    role: str,
    method: str,
    path: str,
    expected: int,
) -> None:
    email = f"{uuid.uuid4().hex[:8]}@test.local"
    user = await create_user(session_factory, email)
    site = (await api.get(f"/api/v1/sites/{world.a}", headers=world.headers)).json()
    await add_site_membership(session_factory, uuid.UUID(site["id"]), user.id, role)
    headers = await login_headers(api, email)

    body = {"name": "Z", "first_name": "Ali", "last_name": "Veli"}
    response = await api.request(
        method, f"/api/v1/sites/{world.a}{path}", headers=headers, json=body
    )

    assert response.status_code == expected, response.text


async def test_blok_guncelleme_hatalari(api: httpx2.AsyncClient, world: World) -> None:
    await _block(api, world, world.a, "A")
    block_b = await _block(api, world, world.a, "B")
    url = f"/api/v1/sites/{world.a}/blocks/{block_b}"

    clash = await api.patch(url, json={"name": "a"}, headers=world.headers)
    empty = await api.patch(url, json={"name": None}, headers=world.headers)
    missing = await api.patch(
        f"/api/v1/sites/{world.a}/blocks/{uuid.uuid4()}", json={"name": "X"}, headers=world.headers
    )

    assert clash.status_code == 409
    assert empty.status_code == 422
    assert missing.status_code == 404


async def test_baska_sitenin_daire_tipi_kullanilamaz(api: httpx2.AsyncClient, world: World) -> None:
    type_b = (await api.get(f"/api/v1/sites/{world.b}/unit-types", headers=world.headers)).json()[
        "items"
    ][0]["id"]
    block = await _block(api, world, world.a)

    response = await _post(
        api, world, world.a, "/units", {"block_id": block, "number": "1", "unit_type_id": type_b}
    )

    assert response.status_code == 422
    assert "unit_type_id" in response.json()["error"]["fields"]


async def test_kisi_guncelleme_hatalari(api: httpx2.AsyncClient, world: World) -> None:
    person = (
        await _post(api, world, world.a, "/people", {"first_name": "Ayşe", "last_name": "Yılmaz"})
    ).json()["data"]

    bad_name = await api.patch(
        f"/api/v1/sites/{world.a}/people/{person['id']}",
        json={"last_name": "Y1"},
        headers=world.headers,
    )
    missing = await api.patch(
        f"/api/v1/sites/{world.a}/people/{uuid.uuid4()}",
        json={"first_name": "Ali"},
        headers=world.headers,
    )

    assert bad_name.status_code == 422
    assert "last_name" in bad_name.json()["error"]["fields"]
    assert missing.status_code == 404


async def test_olmayan_iliski_sonlandirilamaz(api: httpx2.AsyncClient, world: World) -> None:
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))

    response = await _post(
        api, world, world.a, f"/units/{unit}/parties/{uuid.uuid4()}/end", {"end_date": "2026-09-30"}
    )

    assert response.status_code == 404


async def test_mevcut_kisi_kiraci_olarak_eklenir_ve_ayni_rolde_tekrar_eklenemez(
    api: httpx2.AsyncClient, world: World
) -> None:
    person = (
        await _post(api, world, world.a, "/people", {"first_name": "Mehmet", "last_name": "Kaya"})
    ).json()["data"]
    unit = await _unit(api, world, world.a, await _block(api, world, world.a))
    body = {"role": "tenant", "start_date": "2026-01-01", "person_id": person["id"]}

    first = await _post(api, world, world.a, f"/units/{unit}/parties", body)
    again = await _post(api, world, world.a, f"/units/{unit}/parties", body)

    assert first.status_code == 201
    assert again.status_code == 409
