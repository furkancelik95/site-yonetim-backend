"""Banka hareketi aktarımı — frontend servis isteği 06, issue #35 (gerçek PostgreSQL).

Kurgu `finance_world`: A-1 maliki Ayşe Yılmaz (A1-M, A1-O); Haziran'da A1-O'ya 1.000 TL
tahakkuk. Bugün 20.06.2026. Ekstre "Banka Hesabı"na yüklenir.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Any

import httpx2
import openpyxl
import pytest

from site_yonetim.api.deps import get_now
from site_yonetim.services import bank_imports as svc
from tests.integration.finance_world import World, finalized_plan, post_run
from tests.integration.helpers import add_site_membership, create_user, login_headers

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Biçim A: önde hesap bilgisi, tek işaretli "Tutar" sütunu
STATEMENT_A: list[list[Any]] = [
    ["Hesap Hareketleri"],
    ["IBAN", "TR33 0006 1005 1978 6457 8413 26"],
    [],
    ["Tarih", "Açıklama", "Tutar", "Bakiye", "Dekont No"],
    ["18.06.2026", "FAST AYSE YILMAZ A1-O HAZIRAN AIDAT", "600,00", "", "FT1"],
    ["18.06.2026", "EFT AYSE YILMAZ", "200,00", "", "FT2"],
    ["19.06.2026", "HAVALE 1234567 NOLU HESAPTAN", "350,00", "", "FT3"],
    ["19.06.2026", "ELEKTRIK FATURASI OTOMATIK ODEME", "-320,00", "", None],
]


def xlsx(rows: list[list[Any]]) -> bytes:
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)  # type: ignore[union-attr]
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


async def bank(w: World, name: str = "Banka Hesabı") -> str:
    accounts = (await w.get("/cash-accounts")).json()["items"]
    return str(next(a["id"] for a in accounts if a["name"] == name))


async def upload(
    w: World,
    data: bytes,
    *,
    name: str = "ekstre.xlsx",
    account: str | None = None,
    headers: dict[str, str] | None = None,
    slug: str = "aksu-konaklari",
) -> httpx2.Response:
    form = {"cash_account_id": account or await bank(w)}
    return await w.api.post(
        w.url("/bank-imports", slug),
        data=form,
        files={"file": (name, data, XLSX)},
        headers=headers or w.headers,
    )


async def confirm(
    w: World, import_id: str, rows: list[tuple[int, str]], headers: dict[str, str] | None = None
) -> httpx2.Response:
    body = {"rows": [{"row_number": n, "ledger_account_id": a} for n, a in rows]}
    return await w.api.post(
        w.url(f"/bank-imports/{import_id}/confirm"), json=body, headers=headers or w.headers
    )


@pytest.fixture
async def charged(world: World) -> World:
    await finalized_plan(world)
    await post_run(world)
    return world


async def test_onizleme_eslestirir_ve_hicbir_sey_yazmaz(charged: World) -> None:
    response = await upload(charged, xlsx(STATEMENT_A))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["row_count"], body["matched_count"], body["suggested_count"]) == (4, 1, 1)
    assert (body["unmatched_count"], body["ignored_count"], body["duplicate_count"]) == (1, 1, 0)
    assert body["total_in"] == "1150.00"
    assert body["file_name"] == "ekstre.xlsx"
    first, second, third, fourth = body["rows"]
    assert (first["status"], first["amount"], first["bank_reference"]) == (
        "matched",
        "600.00",
        "FT1",
    )
    assert first["suggestion"] == {
        "ledger_account_id": charged.account_id,
        "reference_code": "A1-O",
        "unit_name": "A-1",
        "person_name": "Ayşe YILMAZ",
        "balance": "1000.00",
        "confidence": "high",
        "reason": "Açıklamada referans kodu var (A1-O)",
    }
    # ad Ayşe'nin iki hesabıyla eşleşir; yalnız oturan hesabı borçlu → o önerilir
    assert (second["status"], second["suggestion"]["ledger_account_id"]) == (
        "suggested", charged.account_id
    )  # fmt: skip
    assert (third["status"], third["suggestion"]) == ("unmatched", None)
    assert (fourth["status"], fourth["direction"]) == ("ignored", "out")
    assert (await charged.get("/payments")).json()["total"] == 0
    assert await charged.balance() == Decimal("1000.00")


async def test_onay_tahsilat_kasa_ve_mukerrer(charged: World) -> None:
    preview = (await upload(charged, xlsx(STATEMENT_A))).json()
    rows = [(1, charged.account_id), (3, charged.account_id), (4, charged.account_id)]
    result = await confirm(charged, preview["import_id"], rows)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["message"] == "2 banka hareketi tahsilat olarak işlendi (950,00 TL)."
    assert body["data"]["created_payments"] == 2
    assert body["data"]["skipped"] == [
        {"row_number": 4, "code": "ignored", "message": "Çıkış hareketi tahsilat olarak işlenmez."}
    ]
    assert await charged.balance() == Decimal("50.00")
    payments = (await charged.get("/payments")).json()["items"]
    assert {(p["method"], p["reference"], p["amount"]) for p in payments} == {
        ("bank_transfer", "FT1", "600.00"),
        ("bank_transfer", "FT3", "350.00"),
    }
    balances = {
        a["name"]: a["balance"] for a in (await charged.get("/cash-accounts")).json()["items"]
    }
    assert balances["Banka Hesabı"] == "950.00"

    again = await confirm(charged, preview["import_id"], rows)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_confirmed"

    # aynı dosya yeniden: aktarılmış satırlar mükerrer, aktarılmayan değil
    reupload = (await upload(charged, xlsx(STATEMENT_A))).json()
    statuses = [r["status"] for r in reupload["rows"]]
    assert statuses == ["duplicate", "suggested", "duplicate", "ignored"]
    dup = await confirm(charged, reupload["import_id"], [(1, charged.account_id)])
    assert dup.json()["data"]["skipped"][0]["code"] == "duplicate"
    audit = (await charged.get("/audit", params={"entity": "bank_imports"})).json()
    assert audit["items"][-1]["after"]["created_payments"] == 2


async def test_csv_borc_alacak_sutunlari(charged: World) -> None:
    text = (
        "İŞLEM TARİHİ;AÇIKLAMA;BORÇ;ALACAK;BAKİYE;REFERANS NO\r\n"
        "19/06/2026;EFT A1-O AIDAT;;1.000,00;1.000,00;EF77\r\n"
        "19/06/2026;KASA MASRAFI;12,50;;987,50;\r\n"
    )
    response = await upload(charged, text.encode("cp1254"), name="ekstre.csv")
    assert response.status_code == 200, response.text
    first, second = response.json()["rows"]
    assert (first["status"], first["amount"], first["bank_reference"]) == (
        "matched",
        "1000.00",
        "EF77",
    )
    assert (second["status"], second["amount"]) == ("ignored", "12.50")


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("ekstre.pdf", b"%PDF-1.4", "Yalnız .xlsx, .xls ya da .csv yüklenebilir."),
        ("ekstre.xlsx", b"not a zip", "Dosya okunamadı."),
        ("ekstre.xls", b"not ole2", "Dosya okunamadı."),
        ("ekstre.csv", b"Tarih;Tutar\n01.06.2026;5\n", "Açıklama sütunu bulunamadı."),
    ],
)
async def test_dosya_hatalari(charged: World, name: str, data: bytes, message: str) -> None:
    response = await upload(charged, data, name=name)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["fields"]["file"].startswith(message)


async def test_buyuk_dosya(charged: World) -> None:
    response = await upload(charged, b"x" * (5 * 1024 * 1024 + 10), name="ekstre.csv")
    assert response.status_code == 422
    assert response.json()["error"]["fields"]["file"] == "Dosya en fazla 5 MB olabilir."


async def test_banka_hesabi_zorunlu(charged: World) -> None:
    kasa = await upload(charged, xlsx(STATEMENT_A), account=await bank(charged, "Kasa"))
    assert kasa.status_code == 422
    assert kasa.json()["error"]["fields"] == {"cash_account_id": "Banka hesabını seçin."}
    missing = await upload(charged, xlsx(STATEMENT_A), account=str(uuid.uuid4()))
    assert missing.json()["error"]["fields"] == {"cash_account_id": "Banka hesabını seçin."}


async def test_onizleme_yalniz_yukleyene_ve_sure_dolunca_yok(charged: World) -> None:
    preview = (await upload(charged, xlsx(STATEMENT_A))).json()
    user = await create_user(charged.factory, "muhasebe@test.local")
    await add_site_membership(charged.factory, charged.site_id, user.id, "Muhasebe")
    other = await login_headers(charged.api, "muhasebe@test.local")
    stranger = await confirm(charged, preview["import_id"], [(1, charged.account_id)], other)
    assert stranger.status_code == 404

    later = datetime.now(UTC) + timedelta(hours=7)
    charged.app.dependency_overrides[get_now] = lambda: later
    try:
        expired = await confirm(charged, preview["import_id"], [(1, charged.account_id)])
    finally:
        charged.app.dependency_overrides.pop(get_now)
    assert expired.status_code == 404
    assert expired.json()["error"]["message"].startswith("Aktarım bulunamadı ya da süresi doldu")
    assert await svc.purge_expired(charged.factory, now=later) == 1


async def test_ayni_hareket_eszamanli_iki_onayda_bir_kez(charged: World) -> None:
    first = (await upload(charged, xlsx(STATEMENT_A))).json()["import_id"]
    second = (await upload(charged, xlsx(STATEMENT_A))).json()["import_id"]
    results = await asyncio.gather(
        confirm(charged, first, [(1, charged.account_id)]),
        confirm(charged, second, [(1, charged.account_id)]),
    )
    created = sorted(r.json()["data"]["created_payments"] for r in results)
    assert created == [0, 1]
    assert await charged.balance() == Decimal("400.00")


async def test_ayni_anahtarla_tekrar(charged: World) -> None:
    preview = (await upload(charged, xlsx(STATEMENT_A))).json()
    key = {**charged.headers, "Idempotency-Key": str(uuid.uuid4())}
    one = await confirm(charged, preview["import_id"], [(1, charged.account_id)], key)
    two = await confirm(charged, preview["import_id"], [(1, charged.account_id)], key)
    assert one.status_code == two.status_code == 200
    assert two.headers.get("idempotent-replayed") == "true"
    assert await charged.balance() == Decimal("400.00")


async def test_yetki_ve_site_izolasyonu(charged: World) -> None:
    user = await create_user(charged.factory, "denetci@test.local")
    await add_site_membership(charged.factory, charged.site_id, user.id, "Denetçi")
    auditor = await login_headers(charged.api, "denetci@test.local")
    assert (await upload(charged, xlsx(STATEMENT_A), headers=auditor)).status_code == 403

    preview = (await upload(charged, xlsx(STATEMENT_A))).json()
    other = await charged.api.post(
        charged.url(f"/bank-imports/{preview['import_id']}/confirm", "yildiz-sitesi"),
        json={"rows": [{"row_number": 1, "ledger_account_id": charged.account_id}]},
        headers=charged.headers,
    )
    assert other.status_code == 404
    foreign_bank = await upload(charged, xlsx(STATEMENT_A), slug="yildiz-sitesi")
    assert foreign_bank.status_code == 422  # Aksu'nun banka hesabı Yıldız'da yok
    # Yıldız'da Aksu'nun cari hesabı da yok: satır atlanır
    yildiz_bank = (
        await charged.api.get(
            charged.url("/cash-accounts", "yildiz-sitesi"), headers=charged.headers
        )
    ).json()["items"]
    account = next(a["id"] for a in yildiz_bank if a["name"] == "Banka Hesabı")
    yildiz = await upload(charged, xlsx(STATEMENT_A), account=account, slug="yildiz-sitesi")
    result = await charged.api.post(
        charged.url(f"/bank-imports/{yildiz.json()['import_id']}/confirm", "yildiz-sitesi"),
        json={"rows": [{"row_number": 1, "ledger_account_id": charged.account_id}]},
        headers=charged.headers,
    )
    assert result.json()["data"]["skipped"][0]["code"] == "account_not_found"
    assert await charged.balance() == Decimal("1000.00")
