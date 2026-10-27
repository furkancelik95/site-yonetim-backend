"""Aktarım geçmişi — servis isteği 25 (YAP-06): araç ve açılış bakiyesi aktarımı, mutabakat,
geri alma; bölüm aktarımında aynı kişinin tek kayıt olması (K16)."""

import uuid
from io import BytesIO
from pathlib import Path

import httpx2
import openpyxl
import pytest
from sqlalchemy import func, select

from site_yonetim.api.v1.imports import get_import_store
from site_yonetim.domain.imports.unit_validator import Column
from site_yonetim.models import LedgerEntry, Person
from site_yonetim.services.imports import ImportStore
from tests.integration.finance_world import World
from tests.integration.p02_helpers import OTHER, call, new_unit, scope, staff
from tests.integration.test_period_lock import set_period

CSV = "text/csv"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def stored(world: World, tmp_path: Path) -> World:
    store = ImportStore(tmp_path / "imports")
    world.app.dependency_overrides[get_import_store] = lambda: store
    return world


async def _upload(
    w: World,
    kind: str,
    text: str,
    *,
    name: str = "dosya.csv",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    return await w.api.post(
        w.url(f"/imports/{kind}"),
        files={"file": (name, text.encode("utf-8"), CSV)},
        headers=headers or w.headers,
    )


async def test_arac_aktarimi_kismi_ve_geri_alma(stored: World) -> None:
    w = stored
    preview = await _upload(
        w,
        "vehicles",
        "Bölüm;Plaka;Marka;Renk\nA-1;34 ABC 123;Clio;Beyaz\nZ-9;34XY99;;\nA-1;ABC;;\n",
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["total_rows"], body["importable_count"]) == (3, 1)
    assert [i["column"] for i in body["issues"]] == ["Bölüm", "Plaka"]
    assert body["rows"][0]["plate_display"] == "34 ABC 123"
    assert (await w.get("/vehicles")).json()["total"] == 0  # önizleme kayıt oluşturmaz

    confirmed = await w.post(f"/imports/{body['import_id']}/confirm", {})
    twice = await w.post(f"/imports/{body['import_id']}/confirm", {})

    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["created"] == 1
    assert twice.status_code == 404
    history = (await w.get("/imports")).json()["items"]
    assert [(h["type"], h["status"], h["created_count"], h["can_rollback"]) for h in history] == [
        ("vehicles", "imported", 1, True)
    ]
    rolled = await w.post(f"/imports/{history[0]['id']}/rollback")
    again = await w.post(f"/imports/{history[0]['id']}/rollback")
    assert rolled.json()["data"]["status"] == "rolled_back"
    assert (await w.get("/vehicles", params={"status": "inactive"})).json()["total"] == 1
    assert again.json()["error"]["code"] == "not_imported"


async def test_dosya_turu_ve_sutun_kontrolu(stored: World) -> None:
    wrong_type = await stored.api.post(
        stored.url("/imports/vehicles"),
        files={"file": ("a.txt", b"x", "text/plain")},
        headers=stored.headers,
    )
    missing = await _upload(stored, "opening-balances", "Kod;Miktar\nA1-O;10\n")

    assert wrong_type.json()["error"]["code"] == "invalid_file_type"
    assert (
        missing.json()["error"]["message"] == "Başlıkta Referans kodu ve Tutar sütunları gerekli."
    )


async def _accounts(w: World) -> dict[str, str]:
    detail = (await w.get(f"/units/{w.unit_id}")).json()
    return {a["kind"]: a["reference_code"] for a in detail["accounts"]}


async def test_acilis_bakiyesi_mutabakat_zorunlu(stored: World) -> None:
    w = stored
    codes = await _accounts(w)
    text = (
        "Referans;Tutar;Tarih\n"
        f"{codes['occupant']};1.250,50;01.06.2026\n{codes['owner']};-300,00;\n"
    )
    preview = (await _upload(w, "opening-balances", text)).json()
    import_id = preview["import_id"]

    assert preview["total_amount"] == "950.50"
    assert preview["rows"][1]["direction"] == "credit"
    missing = await w.post(f"/imports/{import_id}/confirm", {})
    mismatch = await w.post(f"/imports/{import_id}/confirm", {"reconciliation_total": "950.49"})
    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "reconciliation_mismatch"
    assert "fark" in mismatch.json()["error"]["message"]

    # Hatalı denemeler önizlemeyi tüketmez; doğru toplamla yazılır
    ok = await w.post(f"/imports/{import_id}/confirm", {"reconciliation_total": "950.50"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["created"] == 2
    statement = (await w.get(f"/accounts/{w.account_id}/statement")).json()
    assert statement["account"]["balance"] == "1250.50"


async def test_acilis_bakiyesi_hatali_satir_varsa_aktarilmaz(stored: World) -> None:
    w = stored
    codes = await _accounts(w)
    preview = (
        await _upload(w, "opening-balances", f"Referans;Tutar\n{codes['occupant']};100\nYOK-1;5\n")
    ).json()

    response = await w.post(
        f"/imports/{preview['import_id']}/confirm", {"reconciliation_total": "100.00"}
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "rows_invalid"


async def test_acilis_bakiyesi_geri_alma_ters_kayit_ve_engel(stored: World) -> None:
    w = stored
    codes = await _accounts(w)
    first = (
        await _upload(w, "opening-balances", f"Referans;Tutar\n{codes['occupant']};100\n")
    ).json()
    await w.post(f"/imports/{first['import_id']}/confirm", {"reconciliation_total": "100.00"})
    second = (await _upload(w, "opening-balances", f"Referans;Tutar\n{codes['owner']};40\n")).json()
    await w.post(f"/imports/{second['import_id']}/confirm", {"reconciliation_total": "40.00"})
    history = {h["total_amount"]: h for h in (await w.get("/imports")).json()["items"]}

    # Owner hesabına sonradan hareket (tahsilat) girildi → geri alma engellenir
    owner_account = (await w.get(f"/units/{w.unit_id}")).json()["accounts"]
    account_id = next(a["id"] for a in owner_account if a["kind"] == "owner")
    with scope(w):
        async with w.factory() as session, session.begin():
            session.add(
                LedgerEntry(
                    account_id=uuid.UUID(account_id), date=w.today[0], debit=0, credit=10,
                    source="payment", description="Tahsilat",
                )
            )  # fmt: skip
    listed = {h["total_amount"]: h for h in (await w.get("/imports")).json()["items"]}
    blocked = await w.post(f"/imports/{history['40.00']['id']}/rollback")
    rolled = await w.post(f"/imports/{history['100.00']['id']}/rollback")

    assert listed["40.00"]["can_rollback"] is False
    assert listed["40.00"]["rollback_blocked_by"].startswith("Aktarılan hesaplara sonradan 1")
    assert blocked.json()["error"]["code"] == "rollback_blocked"
    assert rolled.status_code == 200, rolled.text
    assert await w.balance() == 0  # ters kayıtla sıfırlandı (orijinal yerinde)
    with scope(w):
        async with w.factory() as session:
            entries = await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.account_id == uuid.UUID(w.account_id))
            )
    assert entries == 2


async def test_acilis_bakiyesi_geri_alma_kilitli_donemde_engellenir(stored: World) -> None:
    """D-11: açılış bakiyesi geri alma da dönem kilidine tabi (409 period_locked)."""
    w = stored
    codes = await _accounts(w)
    batch = (
        await _upload(w, "opening-balances", f"Referans;Tutar\n{codes['occupant']};100\n")
    ).json()
    await w.post(f"/imports/{batch['import_id']}/confirm", {"reconciliation_total": "100.00"})
    [history] = (await w.get("/imports")).json()["items"]
    today = w.today[0]
    await set_period(w, today.year, today.month, "locked")
    response = await w.post(f"/imports/{history['id']}/rollback")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "period_locked"


async def test_yetki_ve_izolasyon(stored: World) -> None:
    w = stored
    board = await staff(w, "yk@test.local", "Yönetim Kurulu Üyesi")
    preview = (await _upload(w, "vehicles", "Bölüm;Plaka\nA-1;34ABC123\n")).json()

    forbidden = await _upload(w, "vehicles", "Bölüm;Plaka\nA-1;34ABC123\n", headers=board)
    history = await call(w, "GET", "/imports", headers=board)
    other = await call(w, "POST", f"/imports/{preview['import_id']}/confirm", {}, slug=OTHER)

    assert forbidden.status_code == 403
    assert history.status_code == 403
    assert other.status_code == 404


# --- Bölüm aktarımı: geçmiş, K16, geri alma ----------------------------------------------


def _units_xlsx(*rows: dict[Column, object]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.worksheets[0]
    sheet.title = "Daireler"
    sheet.append([c.value for c in Column])
    for row in rows:
        sheet.append([row.get(c) for c in Column])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _row(block: str, number: str, first: str, last: str, phone: str | None) -> dict[Column, object]:
    return {
        Column.BLOCK: block,
        Column.NUMBER: number,
        Column.OWNER_FIRST: first,
        Column.OWNER_LAST: last,
        Column.OWNER_PHONE: phone,
    }


async def test_bolum_aktarimi_ayni_kisi_tek_kayit_gecmis_ve_geri_alma(stored: World) -> None:
    w = stored
    await call(w, "PATCH", f"/people/{w.person_id}", {"phone": "05321112233"})
    data = _units_xlsx(
        _row("B", "1", "Ali", "Can", "05327776655"),
        _row("B", "2", "ali", "CAN", "+905327776655"),  # aynı kişi (ad + telefon)
        _row("B", "3", "Ayşe", "Yılmaz", "05321112233"),  # sitede kayıtlı kişi
        _row("B", "4", "Ali", "Can", "05320000000"),  # aynı ad, farklı telefon → ayrı kişi
    )
    preview = await w.api.post(
        w.url("/imports/units"), files={"file": ("d.xlsx", data, XLSX)}, headers=w.headers
    )
    body = preview.json()
    assert preview.status_code == 200, preview.text
    merges = {
        (m["full_name"], tuple(m["row_numbers"]), m["existing"]) for m in body["person_merges"]
    }
    assert merges == {("Ali CAN", (2, 3), False), ("Ayşe YILMAZ", (4,), True)}

    confirmed = await w.post(f"/imports/units/{body['import_id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["data"]["created_people"] == 2  # Ali (tek) + Ali (farklı tel.)
    with scope(w):
        async with w.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Person)) == 3

    history = (await w.get("/imports")).json()["items"]
    assert [(h["type"], h["created_count"]) for h in history] == [("units", 4)]
    rolled = await w.post(f"/imports/{history[0]['id']}/rollback")
    assert rolled.status_code == 200, rolled.text
    units = (await w.get("/units")).json()
    assert [u["display_name"] for u in units["items"]] == ["A-1"]
    with scope(w):
        async with w.factory() as session:
            assert await session.scalar(select(func.count()).select_from(Person)) == 1


async def test_bolum_aktarimi_sonradan_iliski_eklenince_geri_alinamaz(stored: World) -> None:
    w = stored
    data = _units_xlsx(_row("B", "1", "Ali", "Can", None))
    body = (
        await w.api.post(
            w.url("/imports/units"), files={"file": ("d.xlsx", data, XLSX)}, headers=w.headers
        )
    ).json()
    await w.post(f"/imports/units/{body['import_id']}/confirm")
    unit_id = next(
        u["id"] for u in (await w.get("/units")).json()["items"] if u["display_name"] == "B-1"
    )
    await new_unit(w, "9")  # ilgisiz
    await w.post(
        f"/units/{unit_id}/parties",
        {"role": "tenant", "start_date": "2026-06-01",
         "person": {"first_name": "Veli", "last_name": "Kara"}},
    )  # fmt: skip
    batch = (await w.get("/imports")).json()["items"][0]

    response = await w.post(f"/imports/{batch['id']}/rollback")

    assert batch["can_rollback"] is False
    assert response.json()["error"]["code"] == "rollback_blocked"
