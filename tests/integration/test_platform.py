"""Platform uçları — docs/06 §2.2, docs/05 §5, docs/07 §8.6."""

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.models import Plan
from tests.integration.conftest import create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
BASLANGIC = ["finance", "announcements", "requests"]
STANDART = [*BASLANGIC, "documents", "visitors", "surveys"]


@pytest.fixture
async def plans(session_factory: Factory, admin_engine: object) -> dict[str, str]:
    async with session_factory() as session, session.begin():
        rows = [
            Plan(name="Başlangıç", max_units=30, allowed_modules=BASLANGIC, sort_order=1),
            Plan(name="Standart", max_units=150, allowed_modules=STANDART, sort_order=2),
        ]
        session.add_all(rows)
    return {p.name: str(p.id) for p in rows}


@pytest.fixture
async def admin(api: httpx2.AsyncClient, session_factory: Factory, plans: object) -> dict[str, str]:
    await create_user(session_factory, "platform@test.local", is_platform_admin=True)
    return await login_headers(api, "platform@test.local")


async def _customer(
    api: httpx2.AsyncClient, admin: dict[str, str], plan_id: str, **overrides: object
) -> httpx2.Response:
    body: dict[str, object] = {
        "name": "Kent Yönetim A.Ş.",
        "tax_number": "1234567890",
        "plan_id": plan_id,
        "admin_full_name": "Kerem Yıldırım",
        "admin_email": "Kerem@Kent.example",
    }
    body.update(overrides)
    return await api.post("/api/v1/platform/customers", json=body, headers=admin)


# --- Erişim -------------------------------------------------------------------


async def test_8_6_platform_yoneticisi_olmayan_404_alir(
    api: httpx2.AsyncClient, session_factory: Factory, plans: object
) -> None:
    await create_user(session_factory, "yonetici@test.local")
    headers = await login_headers(api, "yonetici@test.local")

    for method, path in (
        ("GET", "/api/v1/platform/plans"),
        ("POST", "/api/v1/platform/sites"),
        ("POST", "/api/v1/platform/customers"),
    ):
        response = await api.request(method, path, headers=headers, json={})
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


async def test_kimliksiz_platform_ucu_401(api: httpx2.AsyncClient) -> None:
    assert (await api.get("/api/v1/platform/plans")).status_code == 401


async def test_planlar_sayfali(api: httpx2.AsyncClient, admin: dict[str, str]) -> None:
    body = (await api.get("/api/v1/platform/plans?page_size=1", headers=admin)).json()

    assert body["total"] == 2
    assert body["page_size"] == 1
    assert [p["name"] for p in body["items"]] == ["Başlangıç"]


# --- Müşteri ------------------------------------------------------------------


async def test_musteri_acilir_gecici_parola_bir_kez_ve_calisir(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    response = await _customer(api, admin, plans["Standart"])

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["admin"]["email"] == "kerem@kent.example"
    assert len(data["temporary_password"]) >= 16
    assert "bir kez" in response.json()["message"]

    me = await api.get(
        "/api/v1/me",
        headers=await login_headers(api, "kerem@kent.example", data["temporary_password"]),
    )
    assert me.json()["full_name"] == "Kerem Yıldırım"
    assert me.json()["must_change_password"] is True  # ilk girişte değiştirmek zorunda


async def test_ayni_eposta_ile_ikinci_musteri_409(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    await _customer(api, admin, plans["Standart"])

    response = await _customer(api, admin, plans["Standart"], name="Başka Şirket")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_already_exists"


@pytest.mark.parametrize(
    ("overrides", "field", "message"),
    [
        ({"admin_email": "gecersiz"}, "admin_email", "Geçerli bir e-posta"),
        ({"tax_number": "123"}, "tax_number", "10 haneli"),
        ({"tax_number": "1234567891"}, "tax_number", "Vergi kimlik numarası geçersiz"),
        ({"tax_number": "12345678901"}, "tax_number", "T.C. kimlik numarası geçersiz"),
        ({"name": "AB"}, "name", "En az 3"),
    ],
)
async def test_musteri_dogrulama_hatalari_turkce(
    api: httpx2.AsyncClient,
    admin: dict[str, str],
    plans: dict[str, str],
    overrides: dict[str, object],
    field: str,
    message: str,
) -> None:
    response = await _customer(api, admin, plans["Standart"], **overrides)

    assert response.status_code == 422
    assert message in response.json()["error"]["fields"][field]


async def test_olmayan_plan_422(api: httpx2.AsyncClient, admin: dict[str, str]) -> None:
    response = await _customer(api, admin, "0199a3b2-7c4e-7000-8000-000000000000")

    assert response.status_code == 422
    assert response.json()["error"]["fields"] == {"plan_id": "Seçilen plan bulunamadı."}


# --- Site ---------------------------------------------------------------------


async def test_site_acilir_ve_musteri_sahibi_gorur(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    customer = (await _customer(api, admin, plans["Standart"])).json()["data"]

    response = await api.post(
        "/api/v1/platform/sites",
        headers=admin,
        json={
            "name": "Çamlıca Konutları",
            "plan_id": plans["Standart"],
            "organization_id": customer["organization_id"],
            "city": "İstanbul",
            "iban": "TR33 0006 1005 1978 6457 8413 26",
        },
    )

    assert response.status_code == 201
    site = response.json()["data"]
    assert site["slug"] == "camlica-konutlari"
    assert site["iban"] == "TR330006100519786457841326"
    assert site["modules"] == ["announcements", "documents", "finance", "requests"]
    assert "Excel" in response.json()["message"]

    owner = await login_headers(api, "kerem@kent.example", customer["temporary_password"])
    me = (await api.get("/api/v1/me", headers=owner)).json()
    assert [(s["slug"], s["role"]) for s in me["sites"]] == [("camlica-konutlari", "Yönetici")]
    # Platform yöneticisi siteyi açtı ama verisini görmez (docs/05 §5)
    assert (await api.get("/api/v1/sites/camlica-konutlari", headers=admin)).status_code == 404


async def test_ayni_site_ikinci_kez_acilamaz(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    body = {"name": "Aksu Konakları", "plan_id": plans["Başlangıç"]}
    await api.post("/api/v1/platform/sites", headers=admin, json=body)

    again = await api.post(
        "/api/v1/platform/sites", headers=admin, json={**body, "name": "AKSU KONAKLARI"}
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "site_already_exists"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"iban": "TR330006100519786457841327"}, "iban"),
        ({"organization_id": "0199a3b2-7c4e-7000-8000-000000000000"}, "organization_id"),
        ({"slug": "--"}, "slug"),
    ],
)
async def test_site_dogrulama_hatalari(
    api: httpx2.AsyncClient,
    admin: dict[str, str],
    plans: dict[str, str],
    overrides: dict[str, object],
    field: str,
) -> None:
    response = await api.post(
        "/api/v1/platform/sites",
        headers=admin,
        json={"name": "Deneme Sitesi", "plan_id": plans["Başlangıç"], **overrides},
    )

    assert response.status_code == 422
    assert field in response.json()["error"]["fields"]


async def test_plan_dusurulunce_moduller_daralir(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    site = (
        await api.post(
            "/api/v1/platform/sites",
            headers=admin,
            json={"name": "Deneme Sitesi", "plan_id": plans["Standart"]},
        )
    ).json()["data"]

    response = await api.patch(
        f"/api/v1/platform/sites/{site['id']}/plan",
        headers=admin,
        json={"plan_id": plans["Başlangıç"]},
    )

    assert response.status_code == 200
    assert response.json()["data"]["modules"] == ["announcements", "finance", "requests"]
    assert "Başlangıç" in response.json()["message"]


async def test_olmayan_sitenin_plani_404(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    response = await api.patch(
        "/api/v1/platform/sites/0199a3b2-7c4e-7000-8000-000000000000/plan",
        headers=admin,
        json={"plan_id": plans["Başlangıç"]},
    )

    assert response.status_code == 404


async def test_olmayan_plana_gecilemez(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    site = (
        await api.post(
            "/api/v1/platform/sites",
            headers=admin,
            json={"name": "Deneme Sitesi", "plan_id": plans["Standart"]},
        )
    ).json()["data"]

    response = await api.patch(
        f"/api/v1/platform/sites/{site['id']}/plan",
        headers=admin,
        json={"plan_id": "0199a3b2-7c4e-7000-8000-000000000000"},
    )

    assert response.status_code == 422


# --- Test raporu: site adı, adres eki, düzenleme ------------------------------------------


def test_site_etiketi_sitesi_tekrarlamaz() -> None:
    from site_yonetim.api.v1.platform import site_label

    assert site_label("Çamlıca Konutları") == "Çamlıca Konutları sitesi"
    assert site_label("Yıldız Sitesi") == "Yıldız Sitesi"
    assert site_label("YILDIZ SİTESİ") == "YILDIZ SİTESİ"  # tr-TR küçük harf: "sitesi"
    assert site_label("Gül Site") == "Gül Site"
    assert site_label("Sitem Apartmanı") == "Sitem Apartmanı sitesi"


async def test_site_acilisinda_adres_eki_duzeltilirse_bildirilir(
    api: httpx2.AsyncClient, admin: dict[str, str], plans: dict[str, str]
) -> None:
    named = await api.post(
        "/api/v1/platform/sites",
        headers=admin,
        json={"name": "Yıldız Sitesi", "slug": "Yıldız Evleri_2", "plan_id": plans["Standart"]},
    )
    assert named.status_code == 201, named.text
    data = named.json()["data"]
    assert (data["slug"], data["requested_slug"], data["slug_adjusted"]) == (
        "yildiz-evleri-2",
        "Yıldız Evleri_2",
        True,
    )
    message = named.json()["message"]
    assert message.startswith("Yıldız Sitesi kurulum durumunda açıldı.")  # "sitesi sitesi" yok
    assert "'Yıldız Evleri_2' kurala göre 'yildiz-evleri-2' olarak düzeltildi" in message

    plain = await api.post(
        "/api/v1/platform/sites",
        headers=admin,
        json={"name": "Gül Konutları", "plan_id": plans["Standart"]},
    )
    body = plain.json()
    assert (body["data"]["requested_slug"], body["data"]["slug_adjusted"]) == (None, False)
    assert "Adres eki: 'gul-konutlari'." in body["message"]
    assert body["message"].startswith("Gül Konutları sitesi kurulum")


async def test_site_ve_musteri_bilgisi_duzenlenir(
    api: httpx2.AsyncClient,
    admin: dict[str, str],
    plans: dict[str, str],
    session_factory: Factory,
) -> None:
    customer = (await _customer(api, admin, plans["Standart"])).json()["data"]
    renamed = await api.patch(
        f"/api/v1/platform/customers/{customer['organization_id']}",
        headers=admin,
        json={"name": "  Kent   Yönetim Hizmetleri A.Ş. "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["data"]["name"] == "Kent Yönetim Hizmetleri A.Ş."
    assert renamed.json()["data"]["tax_number"] == "1234567890"
    short = await api.patch(
        f"/api/v1/platform/customers/{customer['organization_id']}",
        headers=admin,
        json={"name": "A"},
    )
    assert short.status_code == 422

    sites = []
    for name in ("Aksu Konakları", "Mimoza Apartmanı"):
        created = await api.post(
            "/api/v1/platform/sites",
            headers=admin,
            json={"name": name, "plan_id": plans["Standart"], "city": "Ankara"},
        )
        sites.append(created.json()["data"])
    url = f"/api/v1/platform/sites/{sites[0]['id']}"
    patched = await api.patch(
        url,
        headers=admin,
        json={"name": "Aksu Evleri", "address": "Gül Sk. 5", "district": "Çankaya", "city": ""},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert (data["name"], data["slug"], data["district"], data["city"]) == (
        "Aksu Evleri", "aksu-konaklari", "Çankaya", None,
    )  # fmt: skip
    assert patched.json()["message"] == "Aksu Evleri sitesi bilgileri güncellendi."
    # Adres eki ve plan bu uçtan değişmez (alan yok sayılmaz değil: şemada yok)
    ignored = await api.patch(
        url, headers=admin, json={"slug": "baska", "plan_id": plans["Başlangıç"]}
    )
    assert ignored.status_code == 200
    assert (ignored.json()["data"]["slug"], ignored.json()["data"]["plan_id"]) == (
        "aksu-konaklari", plans["Standart"],
    )  # fmt: skip
    clash = await api.patch(url, headers=admin, json={"name": "MİMOZA APARTMANI"})
    assert (clash.status_code, clash.json()["error"]["code"]) == (409, "site_already_exists")
    missing = await api.patch(
        "/api/v1/platform/sites/0199a3b2-7c4e-7000-8000-000000000000", headers=admin, json={}
    )
    assert missing.status_code == 404

    await create_user(session_factory, "baskasi@test.local")
    other = await login_headers(api, "baskasi@test.local")
    for path in (url, f"/api/v1/platform/customers/{customer['organization_id']}"):
        denied = await api.patch(path, headers=other, json={"name": "Benim Sitem"})
        assert denied.status_code == 404  # platform ucu: platform yöneticisi değilse 404
