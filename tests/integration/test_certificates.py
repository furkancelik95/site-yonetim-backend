"""Borçsuzluk belgesi — frontend servis isteği 01, issue #24 (gerçek PostgreSQL).

Kurgu `finance_world`: A-1 oturan hesabı (A1-O), Haziran 2026'da 1.000 TL tahakkuk; bugün
20.06.2026. Yönetici "Kerem YILDIRIM".
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import LedgerAccount
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers


def path(w: World) -> str:
    return f"/accounts/{w.account_id}/clearance-certificates"


async def pay(w: World, amount: str) -> None:
    response = await w.post(
        "/payments",
        {
            "ledger_account_id": w.account_id,
            "amount": amount,
            "date": "2026-06-18",
            "method": "cash",
        },
    )
    assert response.status_code == 201, response.text


@pytest.fixture
async def charged(world: World) -> World:
    await finalized_plan(world)
    await post_run(world)
    return world


async def test_borclu_hesaba_belge_verilmez(charged: World) -> None:
    response = await charged.post(path(charged), {})
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "has_debt",
        "message": "Bu hesabın 1.000,00 TL borcu var; borçsuzluk belgesi verilemez.",
        "fields": None,
    }


async def test_borcu_kapanan_hesaba_belge_numarali_ve_degismez(charged: World) -> None:
    await pay(charged, "1000.00")
    response = await charged.post(path(charged), {})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["message"] == "BB-2026-00001 numaralı borçsuzluk belgesi düzenlendi."
    certificate = body["data"]
    assert certificate["number"] == "BB-2026-00001"
    assert certificate["site"] == {"name": "Aksu Konakları", "slug": "aksu-konaklari"}
    assert certificate["account"] == {
        "id": charged.account_id,
        "reference_code": "A1-O",
        "kind": "occupant",
        "unit_name": "A-1",
        "person_name": "Ayşe YILMAZ",
    }
    assert (certificate["balance"], certificate["as_of"], certificate["valid_until"]) == (
        "0.00",
        "2026-06-20",
        "2026-07-20",
    )
    assert certificate["issued_by"] == "Kerem YILDIRIM"

    # yazdırma: aynı nesne, `data` sarmalı olmadan
    read = await charged.get(f"/clearance-certificates/{certificate['id']}")
    assert read.status_code == 200
    assert read.json() == certificate

    # numara artar; yeni yılda baştan başlar
    assert (await charged.post(path(charged), {})).json()["data"]["number"] == "BB-2026-00002"
    charged.set_today(date(2027, 1, 4))
    assert (await charged.post(path(charged), {})).json()["data"]["number"] == "BB-2027-00001"

    # belge sonradan değişmez, silinmez; denetim kaydında
    with site_scope(charged.site_id):
        async with charged.factory() as session:
            for statement in (
                "UPDATE clearance_certificates SET balance = 0",
                "DELETE FROM clearance_certificates",
            ):
                with pytest.raises(DBAPIError, match="Geçmiş kaydı değiştirilemez"):
                    await session.execute(text(statement))
                await session.rollback()
    audit = (await charged.get("/audit", params={"entity": "clearance_certificates"})).json()
    assert audit["total"] == 3


async def test_alacakli_hesaba_belge_verilir_bakiye_yazilir(charged: World) -> None:
    await pay(charged, "1250.00")
    certificate = (await charged.post(path(charged), {})).json()["data"]
    assert certificate["balance"] == "-250.00"
    # sonradan yeni borç gelse de belgedeki bakiye aynı kalır
    charged.set_today(date(2026, 7, 2))
    july = await charged.post("/charge-runs", {"charge_date": "2026-07-01"})
    assert july.status_code == 201, july.text
    assert await charged.balance() == Decimal("750.00")
    read = await charged.get(f"/clearance-certificates/{certificate['id']}")
    assert read.json()["balance"] == "-250.00"


async def test_ayni_anahtarla_ikinci_numara_cikmaz(charged: World) -> None:
    await pay(charged, "1000.00")
    key = {"Idempotency-Key": str(uuid.uuid4())}
    first = await charged.post(path(charged), {}, headers=key)
    second = await charged.post(path(charged), {}, headers=key)
    assert first.status_code == second.status_code == 201
    assert second.headers.get("idempotent-replayed") == "true"
    assert first.json() == second.json()
    third = await charged.post(path(charged), {})
    assert third.json()["data"]["number"] == "BB-2026-00002"


async def test_kapali_hesap_ve_olmayan_kayitlar(charged: World) -> None:
    assert (
        await charged.post(f"/accounts/{uuid.uuid4()}/clearance-certificates", {})
    ).status_code == 404
    assert (await charged.get(f"/clearance-certificates/{uuid.uuid4()}")).status_code == 404
    await pay(charged, "1000.00")
    with site_scope(charged.site_id):
        async with charged.factory() as session, session.begin():
            await session.execute(
                update(LedgerAccount)
                .where(LedgerAccount.id == uuid.UUID(charged.account_id))
                .values(is_closed=True)
            )
    closed = await charged.post(path(charged), {})
    assert closed.status_code == 409
    assert closed.json()["error"]["code"] == "account_closed"


async def test_yetki_ve_site_izolasyonu(charged: World) -> None:
    await pay(charged, "1000.00")
    certificate = (await charged.post(path(charged), {})).json()["data"]
    read_path = f"/clearance-certificates/{certificate['id']}"

    async def headers_for(email: str, role: str) -> dict[str, str]:
        user = await create_user(charged.factory, email)
        await add_site_membership(charged.factory, charged.site_id, user.id, role)
        return await login_headers(charged.api, email)

    auditor = await headers_for("denetci@test.local", "Denetçi")
    read = await charged.api.get(charged.url(read_path), headers=auditor)
    assert read.status_code == 200
    assert read.json()["account"]["person_name"] is None  # Denetçi kişisel veri görmez
    denied = await charged.api.post(charged.url(path(charged)), json={}, headers=auditor)
    assert denied.status_code == 403

    accounting = await headers_for("muhasebe@test.local", "Muhasebe")
    issued = await charged.api.post(charged.url(path(charged)), json={}, headers=accounting)
    assert issued.status_code == 201

    technician = await headers_for("teknik@test.local", "Teknik Personel")
    assert (await charged.api.get(charged.url(read_path), headers=technician)).status_code == 403

    # Yıldız Sitesi adresinden Aksu'nun hesabı ve belgesi yok
    other = "yildiz-sitesi"
    assert (
        await charged.api.post(charged.url(path(charged), other), json={}, headers=charged.headers)
    ).status_code == 404
    assert (
        await charged.api.get(charged.url(read_path, other), headers=charged.headers)
    ).status_code == 404
