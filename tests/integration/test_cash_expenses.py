"""Gider ve kasa — docs/07 §4 (gerçek PostgreSQL, RLS'e tabi rol).

Kurgu (07 §4): bir site, bir gider kategorisi, **Banka** hesabı (açılış 10.000, açılış hareketi
ile), **Kasa** hesabı (açılış 0). Tarih: bugün.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from site_yonetim.api.deps import get_today
from site_yonetim.api.v1.expenses import get_file_store
from site_yonetim.db.tenancy import site_scope
from site_yonetim.domain.cash import CashSource
from site_yonetim.models import CashAccount, Expense, StoredFile
from site_yonetim.services import cash as cash_svc
from site_yonetim.services.files import FileStore
from site_yonetim.services.provisioning import provision_site
from tests.integration.helpers import add_site_membership, create_user, login_headers

Factory = async_sessionmaker[AsyncSession]
TODAY = date(2026, 10, 1)


@dataclass
class Kasa:
    api: httpx2.AsyncClient
    factory: Factory
    site_id: uuid.UUID
    other_site_id: uuid.UUID
    headers: dict[str, str]
    store: FileStore
    bank: str = ""
    cash: str = ""
    category: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def url(self, path: str, slug: str = "aksu-konaklari") -> str:
        return f"/api/v1/sites/{slug}{path}"

    async def get(
        self, path: str, headers: dict[str, str] | None = None, **kw: Any
    ) -> httpx2.Response:
        return await self.api.get(self.url(path), headers=headers or self.headers, **kw)

    async def post(
        self, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return await self.api.post(self.url(path), json=body, headers=headers or self.headers)

    async def balances(self) -> dict[str, str]:
        body = (await self.get("/cash-accounts")).json()
        return {a["name"]: a["balance"] for a in body["items"]} | {"_total": body["total_balance"]}

    async def expense(
        self,
        amount: str,
        *,
        paid: bool = False,
        account: str | None = None,
        day: date = TODAY,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **extra: str,
    ) -> httpx2.Response:
        form: dict[str, str] = {
            "expense_category_id": self.category,
            "description": "Asansör bakımı",
            "amount": amount,
            "date": day.isoformat(),
            "paid": "true" if paid else "false",
        }
        if paid:
            form["paid_on"] = day.isoformat()
        if account:
            form["cash_account_id"] = account
        form.update(extra)
        return await self.api.post(
            self.url("/expenses"), data=form, files=files, headers=headers or self.headers
        )


@pytest.fixture
async def kasa(
    api: httpx2.AsyncClient,
    api_app: FastAPI,
    session_factory: Factory,
    admin_engine: object,
    tmp_path: Path,
) -> Kasa:
    api_app.dependency_overrides[get_today] = lambda: TODAY
    store = FileStore(tmp_path / "files")
    api_app.dependency_overrides[get_file_store] = lambda: store
    ids = []
    for name in ("Aksu Konakları", "Yıldız Sitesi"):
        async with session_factory() as session, session.begin():
            ids.append((await provision_site(session, name=name)).id)
    user = await create_user(session_factory, "yonetici@test.local", full_name="Kerem YILDIRIM")
    for site_id in ids:
        await add_site_membership(session_factory, site_id, user.id, "Yönetici")
    k = Kasa(
        api, session_factory, ids[0], ids[1], await login_headers(api, "yonetici@test.local"), store
    )
    # Site açılışında kurulan iki hesap; Banka'ya açılış hareketi (07 §4 kurgusu).
    with site_scope(k.site_id):
        async with session_factory() as session, session.begin():
            accounts = {a.name: a for a in await session.scalars(select(CashAccount))}
            await cash_svc.add_movement(
                session, accounts["Banka Hesabı"], day=TODAY - timedelta(days=30),
                inflow=Decimal(10000), description="Açılış bakiyesi", source=CashSource.OPENING,
            )  # fmt: skip
    k.bank, k.cash = str(accounts["Banka Hesabı"].id), str(accounts["Kasa"].id)
    k.category = (await k.get("/expense-categories")).json()[0]["id"]
    return k


# --- 07 §4 -------------------------------------------------------------------------------


async def test_4_1_odenen_gider_kasadan_duser(kasa: Kasa) -> None:
    response = await kasa.expense("2500.00", paid=True, account=kasa.bank, vendor="Kone")
    assert response.status_code == 201, response.text
    assert response.json()["message"] == "2.500,00 TL gider ödendi olarak kaydedildi."
    assert (await kasa.balances())["Banka Hesabı"] == "7500.00"
    statement = (await kasa.get(f"/cash-accounts/{kasa.bank}/statement")).json()
    assert statement["movements"]["items"][0]["description"] == "Asansör bakımı · Kone"
    assert statement["movements"]["items"][0]["source"] == "expense"


async def test_4_2_odenmeyen_gider_kasayi_etkilemez(kasa: Kasa) -> None:
    await kasa.expense("2500.00")
    assert (await kasa.balances())["Banka Hesabı"] == "10000.00"


async def test_4_3_odendi_isaretlenirken_hesap_secilmezse_reddedilir(kasa: Kasa) -> None:
    response = await kasa.expense("2500.00", paid=True)
    assert response.status_code == 422
    assert "kasa" in response.json()["error"]["message"]


async def test_4_4_sonradan_odeme_kasaya_islenir(kasa: Kasa) -> None:
    expense_id = (await kasa.expense("1200.00")).json()["data"]["id"]
    paid = await kasa.post(
        f"/expenses/{expense_id}/pay", {"cash_account_id": kasa.cash, "paid_on": "2026-10-01"}
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["data"]["is_paid"] is True
    balances = await kasa.balances()
    assert (balances["Kasa"], balances["Banka Hesabı"]) == ("-1200.00", "10000.00")
    again = await kasa.post(
        f"/expenses/{expense_id}/pay", {"cash_account_id": kasa.cash, "paid_on": "2026-10-01"}
    )
    assert again.json()["error"]["message"] == "Bu gider zaten ödenmiş görünüyor."


async def test_4_5_geri_alinan_gider_kasaya_doner_silinmez(kasa: Kasa) -> None:
    expense_id = (await kasa.expense("3000.00", paid=True, account=kasa.bank)).json()["data"]["id"]
    reversed_ = await kasa.post(f"/expenses/{expense_id}/reverse", {"reason": "Mükerrer fatura"})
    assert reversed_.status_code == 200, reversed_.text
    correction = reversed_.json()["data"]
    assert (correction["amount"], correction["reversal_of_id"]) == ("-3000.00", expense_id)
    assert correction["description"] == "DÜZELTME — Asansör bakımı"
    assert (await kasa.balances())["Banka Hesabı"] == "10000.00"
    listing = (await kasa.get("/expenses")).json()
    assert listing["total"] == 2
    original = next(e for e in listing["items"] if e["id"] == expense_id)
    assert original["is_reversed"] is True
    assert listing["summary"]["total"] == "0.00"  # gerçekleşen gider toplamı


async def test_4_6_ayni_gider_iki_kez_geri_alinamaz(kasa: Kasa) -> None:
    expense_id = (await kasa.expense("500.00", paid=True, account=kasa.bank)).json()["data"]["id"]
    first = await kasa.post(f"/expenses/{expense_id}/reverse", {"reason": "Hata"})
    second = await kasa.post(f"/expenses/{expense_id}/reverse", {"reason": "Hata"})
    assert second.status_code == 409
    assert (await kasa.balances())["Banka Hesabı"] == "10000.00"
    correction = first.json()["data"]["id"]
    third = await kasa.post(f"/expenses/{correction}/reverse", {"reason": "Hata"})
    assert third.json()["error"]["message"] == "Düzeltme kaydı geri alınamaz."


async def test_4_7_aktarim_toplam_bakiyeyi_degistirmez(kasa: Kasa) -> None:
    response = await kasa.post(
        "/cash-transfers",
        {"from_id": kasa.bank, "to_id": kasa.cash, "date": "2026-10-01", "amount": "4000.00"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["outgoing"]["source_id"] == data["incoming"]["id"]
    assert data["incoming"]["description"] == "Hesaplar arası aktarım ← Banka Hesabı"
    assert await kasa.balances() == {
        "Banka Hesabı": "6000.00",
        "Kasa": "4000.00",
        "_total": "10000.00",
    }
    same = await kasa.post(
        "/cash-transfers",
        {"from_id": kasa.bank, "to_id": kasa.bank, "date": "2026-10-01", "amount": "1.00"},
    )
    assert same.status_code == 422


async def test_4_8_gider_kaynakli_hareket_kasadan_geri_alinamaz(kasa: Kasa) -> None:
    await kasa.expense("800.00", paid=True, account=kasa.bank)
    movement = (await kasa.get(f"/cash-accounts/{kasa.bank}/statement")).json()["movements"][
        "items"
    ][0]
    response = await kasa.post(f"/cash-movements/{movement['id']}/reverse", {"reason": "Yanlış"})
    assert response.status_code == 409
    assert "gider" in response.json()["error"]["message"]


async def test_4_9_elle_hareket_ters_kayitla_geri_alinir(kasa: Kasa) -> None:
    created = await kasa.post(
        "/cash-movements",
        {"cash_account_id": kasa.bank, "date": "2026-10-01", "direction": "out", "amount": "150.00",
         "description": "Banka masrafı"},
    )  # fmt: skip
    assert (await kasa.balances())["Banka Hesabı"] == "9850.00"
    movement_id = created.json()["data"]["id"]
    reversed_ = await kasa.post(f"/cash-movements/{movement_id}/reverse", {"reason": "Mükerrer"})
    assert reversed_.json()["data"]["description"] == "DÜZELTME — Banka masrafı"
    assert (await kasa.balances())["Banka Hesabı"] == "10000.00"
    again = await kasa.post(f"/cash-movements/{movement_id}/reverse", {"reason": "Mükerrer"})
    assert again.json()["error"]["code"] == "movement_already_reversed"
    reversal = await kasa.post(
        f"/cash-movements/{reversed_.json()['data']['id']}/reverse", {"reason": "x y z"}
    )
    assert reversal.json()["error"]["message"] == "Düzeltme kaydı geri alınamaz."


async def test_4_10_ileri_tarihli_gider_reddedilir(kasa: Kasa) -> None:
    response = await kasa.expense("100.00", day=TODAY + timedelta(days=30))
    assert response.status_code == 422
    tomorrow = await kasa.expense("100.00", day=TODAY + timedelta(days=1))  # 1 gün tolerans
    assert tomorrow.status_code == 201


async def test_4_11_ekstre_yuruyen_bakiyesi_dogru(kasa: Kasa) -> None:
    for days, direction, amount in ((3, "in", "1000.00"), (2, "out", "400.00")):
        await kasa.post(
            "/cash-movements",
            {"cash_account_id": kasa.bank, "date": (TODAY - timedelta(days=days)).isoformat(),
             "direction": direction, "amount": amount, "description": "Elle hareket"},
        )  # fmt: skip
    body = (await kasa.get(f"/cash-accounts/{kasa.bank}/statement")).json()
    assert body["closing"] == "10600.00"
    items = body["movements"]["items"]
    assert [i["date"] for i in items] == sorted((i["date"] for i in items), reverse=True)
    assert items[0]["running_balance"] == body["closing"]
    since = {"from": (TODAY - timedelta(days=2)).isoformat()}
    ranged = (await kasa.get(f"/cash-accounts/{kasa.bank}/statement", params=since)).json()
    assert (ranged["opening"], ranged["total_out"], ranged["closing"]) == (
        "11000.00",
        "400.00",
        "10600.00",
    )
    page_two = (
        await kasa.get(f"/cash-accounts/{kasa.bank}/statement", params={"page_size": 1, "page": 2})
    ).json()
    assert page_two["movements"]["items"][0]["running_balance"] == "11000.00"


async def test_4_12_baska_sitenin_kasasi_gorunmez(kasa: Kasa) -> None:
    await kasa.api.post(
        kasa.url("/cash-accounts", "yildiz-sitesi"),
        json={"name": "Yabancı Hesap", "kind": "bank"}, headers=kasa.headers,
    )  # fmt: skip
    names = [a["name"] for a in (await kasa.get("/cash-accounts")).json()["items"]]
    assert names == ["Banka Hesabı", "Kasa"]
    foreign = await kasa.api.get(
        kasa.url(f"/cash-accounts/{kasa.bank}/statement", "yildiz-sitesi"), headers=kasa.headers
    )
    assert foreign.status_code == 404


async def test_4_13_yanlis_icerikli_dosya_reddedilir(kasa: Kasa) -> None:
    response = await kasa.expense(
        "100.00",
        files={"document": ("fatura.pdf", b"<script>alert(1)</script>", "application/pdf")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_file_content"
    assert not kasa.store.root.exists() or list(kasa.store.root.rglob("*.*")) == []  # diskte iz yok
    assert (await kasa.get("/expenses")).json()["total"] == 0


async def test_4_14_gecerli_pdf_kaydedilir_disk_adi_bagimsiz(kasa: Kasa) -> None:
    content = b"%PDF-1.4 sahte fatura"
    response = await kasa.expense(
        "100.00", files={"document": ("../../gizli fatura.pdf", content, "application/pdf")}
    )
    assert response.status_code == 201, response.text
    file_id = response.json()["data"]["stored_file_id"]
    with site_scope(kasa.site_id):
        async with kasa.factory() as session:
            stored = await session.get(StoredFile, uuid.UUID(file_id))
    assert stored is not None
    assert stored.file_name == "gizli fatura.pdf"
    assert ".." not in stored.storage_path
    assert stored.storage_path == f"{kasa.site_id}/{file_id}.pdf"
    assert kasa.store.exists(stored.storage_path)
    download = await kasa.get(f"/files/{file_id}")
    assert download.content == content
    assert download.headers["content-type"] == "application/pdf"
    assert download.headers["content-disposition"].startswith('inline; filename="gizli fatura.pdf"')
    other = await kasa.api.get(kasa.url(f"/files/{file_id}", "yildiz-sitesi"), headers=kasa.headers)
    assert other.status_code == 404


# --- tahsilat → kasa ----------------------------------------------------------------------


async def test_payment_goes_into_cash_account(kasa: Kasa) -> None:
    block = (await kasa.post("/blocks", {"name": "A"})).json()["data"]["id"]
    unit = (await kasa.post("/units", {"block_id": block, "number": "1"})).json()["data"]["id"]
    party = await kasa.post(
        f"/units/{unit}/parties",
        {
            "role": "owner",
            "start_date": "2020-01-01",
            "person": {"first_name": "Ayşe", "last_name": "Yılmaz"},
        },
    )
    account = next(
        a["id"] for a in party.json()["data"]["opened_accounts"] if a["kind"] == "occupant"
    )
    paid = await kasa.post(
        "/payments",
        {"ledger_account_id": account, "amount": "750.00", "date": "2026-10-01", "method": "cash",
         "cash_account_id": kasa.cash},
    )  # fmt: skip
    assert paid.status_code == 201, paid.text
    assert paid.json()["data"]["payment"]["cash_account_id"] == kasa.cash
    assert (await kasa.balances())["Kasa"] == "750.00"
    movement = (await kasa.get(f"/cash-accounts/{kasa.cash}/statement")).json()["movements"][
        "items"
    ][0]
    assert (movement["source"], movement["description"]) == ("payment", "Tahsilat — A1-O")
    denied = await kasa.post(f"/cash-movements/{movement['id']}/reverse", {"reason": "Yanlış"})
    assert "tahsilat" in denied.json()["error"]["message"]
    foreign = await kasa.post(
        "/payments",
        {"ledger_account_id": account, "amount": "1.00", "date": "2026-10-01", "method": "cash",
         "cash_account_id": str(uuid.uuid4())},
    )  # fmt: skip
    assert foreign.json()["error"]["code"] == "cash_account_not_found"


# --- hesap, doğrulama, özet -----------------------------------------------------------------


async def test_account_rules(kasa: Kasa) -> None:
    opened = await kasa.post(
        "/cash-accounts",
        {
            "name": "Ziraat",
            "kind": "bank",
            "iban": "tr33 0006 1005 1978 6457 8413 26",
            "opening_balance": "-250.00",
        },
    )
    assert opened.status_code == 201, opened.text
    data = opened.json()["data"]
    assert (data["iban"], data["balance"]) == ("TR330006100519786457841326", "-250.00")
    duplicate = await kasa.post("/cash-accounts", {"name": "  ziraat ", "kind": "bank"})
    assert duplicate.json()["error"]["message"] == "'ziraat' adında bir hesap zaten var."
    bad_iban = await kasa.post(
        "/cash-accounts", {"name": "Deniz", "kind": "bank", "iban": "DE89370400440532013000"}
    )
    assert bad_iban.json()["error"]["fields"] == {
        "iban": "IBAN geçersiz görünüyor (TR ile başlamalı)."
    }
    with site_scope(kasa.site_id):
        async with kasa.factory() as session, session.begin():
            account = await session.get(CashAccount, uuid.UUID(kasa.cash))
            assert account is not None
            account.is_active = False
    closed = await kasa.expense("10.00", paid=True, account=kasa.cash)
    assert closed.json()["error"]["code"] == "cash_account_inactive"
    totals = (await kasa.get("/cash-accounts")).json()
    assert totals["total_balance"] == "9750.00"  # kapalı Kasa toplama girmez


async def test_expense_validation_filters_and_summary(kasa: Kasa) -> None:
    assert (await kasa.expense("0.00")).status_code == 422
    assert (await kasa.expense("100000000.01")).status_code == 422
    assert (await kasa.expense("100.00", description="ab")).status_code == 422
    assert (await kasa.expense("100.00", expense_category_id=str(uuid.uuid4()))).json()["error"][
        "code"
    ] == "category_not_found"
    early = await kasa.expense("100.00", paid=True, account=kasa.bank, paid_on="2026-09-30")
    assert early.json()["error"]["message"] == "Ödeme tarihi belge tarihinden önce olamaz."
    await kasa.expense("1000.00", paid=True, account=kasa.bank)
    await kasa.expense("250.00", vendor="  " + "V" * 150)
    await kasa.expense("400.00", day=date(2025, 12, 31))
    unpaid = (await kasa.get("/expenses", params={"paid": "unpaid", "year": 2026})).json()
    assert [e["amount"] for e in unpaid["items"]] == ["250.00"]
    assert len(unpaid["items"][0]["vendor"]) == 100  # 100 karakterde kesilir
    summary = (await kasa.get("/expenses", params={"year": 2026})).json()["summary"]
    assert (summary["total"], summary["unpaid_total"], summary["unpaid_count"]) == (
        "1250.00",
        "250.00",
        1,
    )
    assert summary["by_category"][0]["total"] == "1250.00"
    assert (await kasa.get(f"/expenses/{uuid.uuid4()}")).status_code == 404


async def test_closed_period_rejects_expense(kasa: Kasa) -> None:
    await kasa.expense("10.00")  # dönemi açar
    with site_scope(kasa.site_id):
        async with kasa.factory() as session, session.begin():
            await session.execute(text("UPDATE periods SET status = 'closed'"))
    response = await kasa.expense("10.00")
    assert (
        response.json()["error"]["message"] == "10/2026 dönemi kapalı, bu tarihe kayıt yazılamaz."
    )


async def test_idempotent_expense(kasa: Kasa) -> None:
    key = {**kasa.headers, "Idempotency-Key": str(uuid.uuid4())}
    first = await kasa.expense("300.00", paid=True, account=kasa.bank, headers=key)
    second = await kasa.expense("300.00", paid=True, account=kasa.bank, headers=key)
    assert second.json() == first.json()
    assert (await kasa.balances())["Banka Hesabı"] == "9700.00"


async def test_ledgers_are_immutable(kasa: Kasa) -> None:
    await kasa.expense("300.00", paid=True, account=kasa.bank)
    with site_scope(kasa.site_id):
        async with kasa.factory() as session:
            for statement, pattern in (
                ("UPDATE cash_movements SET inflow = 1", "değiştirilemez"),
                ("UPDATE expenses SET amount = 1", "değiştirilemez"),
                ("UPDATE expenses SET paid_on = NULL, cash_account_id = NULL", "değiştirilemez"),
                ("DELETE FROM expenses", "silinemez"),
            ):
                with pytest.raises(DBAPIError, match=pattern):
                    await session.execute(text(statement))
                await session.rollback()
            # İzin verilen tek değişiklikler: ödeme bir kez, geri alındı işareti
            expense = await session.scalar(select(Expense))
            assert expense is not None


async def test_permissions(kasa: Kasa) -> None:
    board = await create_user(kasa.factory, "kurul@test.local")
    await add_site_membership(kasa.factory, kasa.site_id, board.id, "Yönetim Kurulu Üyesi")
    headers = await login_headers(kasa.api, "kurul@test.local")
    assert (await kasa.get("/cash-accounts", headers)).status_code == 200
    assert (await kasa.get("/expenses", headers)).status_code == 200
    assert (await kasa.expense("10.00", headers=headers)).status_code == 403
    income = {"cash_account_id": kasa.bank, "date": "2026-10-01", "direction": "in",
              "amount": "1.00", "description": "Faiz"}  # fmt: skip
    denied = await kasa.post("/cash-movements", income, headers)
    assert denied.status_code == 403
    guard = await create_user(kasa.factory, "guvenlik@test.local")
    await add_site_membership(kasa.factory, kasa.site_id, guard.id, "Güvenlik")
    guard_headers = await login_headers(kasa.api, "guvenlik@test.local")
    assert (await kasa.get("/cash-accounts", guard_headers)).status_code == 403
    assert (await kasa.get("/expenses", guard_headers)).status_code == 403
