"""Satın alma — SAT-01..06, servis istekleri 62–67 (gerçek PostgreSQL).

Tedarikçi kartı, ayar ve bütçe, talep → teklif (tedarikçi linki) → istisna/onay → sipariş →
kabul (stok sistem adına) → fatura (üçlü eşleşme, fark onayı) → "ödendi" (gider + kasa çıkışı);
onay kuralı bağlantısı, yetki, site izolasyonu.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from sqlalchemy import select

from site_yonetim.db.tenancy import site_scope
from site_yonetim.models import AuditLog, CashMovement, Expense, StockItem, Vendor
from tests.integration.finance_world import World, finalized_plan, item_body
from tests.integration.helpers import add_site_membership, create_user, login_headers

PDF = b"%PDF-1.4\n%test\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@dataclass
class Buy:
    w: World
    muhasebe: dict[str, str]  # expenses.manage, stok yetkisi yok
    yk: dict[str, str]  # yalnız okuma (expenses.read)
    yk_id: uuid.UUID
    category: str
    stock_id: str
    vendors: dict[str, str]

    async def post(
        self, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> httpx2.Response:
        return await self.w.api.post(self.w.url(path), json=body, headers=headers or self.w.headers)

    async def get(
        self, path: str, headers: dict[str, str] | None = None, **params: Any
    ) -> httpx2.Response:
        return await self.w.api.get(
            self.w.url(path), headers=headers or self.w.headers, params=params
        )


async def _member(w: World, email: str, name: str, role: str) -> tuple[uuid.UUID, dict[str, str]]:
    user = await create_user(w.factory, email, full_name=name)
    await add_site_membership(w.factory, w.site_id, user.id, role)
    return user.id, await login_headers(w.api, email)


async def _vendor(b: Buy | World, name: str, tax: str, **extra: Any) -> httpx2.Response:
    w = b.w if isinstance(b, Buy) else b
    return await w.post("/vendors", {"name": name, "tax_no": tax, **extra})


@pytest.fixture
async def buy(world: World) -> Buy:
    w = world
    await finalized_plan(w, item_body(w, annual_amount="12000.00"))
    _, muhasebe = await _member(w, "muhasebe@test.local", "Mehmet MUHASEBE", "Muhasebe")
    yk_id, yk = await _member(w, "yk@test.local", "Yasemin YK", "Yönetim Kurulu Üyesi")
    stock = await w.post("/stock-items", {"name": "Pompa contası", "unit_label": "adet"})
    assert stock.status_code == 201, stock.text
    vendors = {}
    for name, tax in (
        ("Akın Pompa Ltd.", "1234567890"),
        ("Bora Tesisat", "2234567890"),
        ("Ceren Yapı", "3234567890"),
    ):
        created = await _vendor(w, name, tax, email="teklif@ornek.com")
        assert created.status_code == 201, created.text
        vendors[name.split()[0]] = created.json()["data"]["id"]
    return Buy(w, muhasebe, yk, yk_id, w.lookups["category"], stock.json()["data"]["id"], vendors)


def pr_body(b: Buy, *, price: str = "7500.00", qty: str = "2") -> dict[str, Any]:
    return {
        "title": "Hidrofor pompası",
        "reason": "Mevcut pompa arızalı, tamir edilemiyor.",
        "category_id": b.category,
        "items": [
            {"description": "Pompa", "qty": qty, "unit": "adet", "est_unit_price": price},
            {
                "description": "Conta",
                "qty": 4,
                "est_unit_price": "0.50",
                "stock_item_id": b.stock_id,
            },
        ],
    }


def quote_body(vendor: str, prices: tuple[str, str], **extra: Any) -> dict[str, Any]:
    return {
        "vendor_id": vendor,
        "lines": [{"unit_price": p} for p in prices],
        "delivery_days": 5,
        "payment_terms": "30 gün vadeli",
        "valid_until": "2026-07-20",
        **extra,
    }


async def open_pr(b: Buy, **kwargs: Any) -> dict[str, Any]:
    created = await b.post("/purchase-requests", pr_body(b, **kwargs))
    assert created.status_code == 201, created.text
    data: dict[str, Any] = created.json()["data"]
    return data


async def approved_pr(b: Buy, prices: tuple[str, str] = ("7000.00", "0.50")) -> dict[str, Any]:
    """İki teklifli, seçilmiş ve muhasebe onaylı talep (talep eden Kerem)."""
    pr = await open_pr(b)
    for vendor, p in ((b.vendors["Akın"], prices), (b.vendors["Bora"], ("7900.00", "0.60"))):
        added = await b.post(f"/purchase-requests/{pr['id']}/quotes", quote_body(vendor, p))
        assert added.status_code == 201, added.text
    detail = (await b.get(f"/purchase-requests/{pr['id']}")).json()
    first = next(q["id"] for q in detail["quotes"] if q["vendor_id"] == b.vendors["Akın"])
    selected = await b.post(f"/purchase-requests/{pr['id']}/select-quote", {"quote_id": first})
    assert selected.json()["data"]["status"] == "approval_pending", selected.text
    decided = await b.post(
        f"/purchase-requests/{pr['id']}/decide", {"decision": "approve"}, b.muhasebe
    )
    assert decided.json()["data"]["status"] == "approved", decided.text
    return decided.json()["data"]  # type: ignore[no-any-return]


# --- Tedarikçi kartı (62) ---------------------------------------------------------------------


async def test_tedarikci_karti(buy: Buy) -> None:
    b = buy
    bad = await _vendor(b, "X", "123", iban="TR00", email="yok", phone="12")
    assert set(bad.json()["error"]["fields"]) == {"name", "tax_no", "iban", "email", "phone"}
    same = await _vendor(b, "Başka Unvan", "1234567890")
    assert same.json()["error"]["fields"] == {"tax_no": "Bu vergi numarasıyla tedarikçi var."}
    created = await _vendor(
        b,
        "  Deniz   Asansör  ",
        "12345678901",
        iban="TR33 0006 1005 1978 6457 8413 26",
        phone="0212 555 11 22",
        email="Bilgi@Deniz.COM",
        category="Asansör bakım",
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert (data["name"], data["iban_masked"], data["email"], data["phone"]) == (
        "Deniz Asansör",
        "TR33 •••• •••• 1326",
        "bilgi@deniz.com",
        "+902125551122",
    )
    assert "iban" not in data
    # Düzenlemede boş IBAN mevcut olanı korur; durum değişir.
    patched = await b.w.api.patch(
        b.w.url(f"/vendors/{data['id']}"),
        json={"status": "blacklist", "iban": ""},
        headers=b.w.headers,
    )
    assert patched.json()["data"]["iban_masked"] == "TR33 •••• •••• 1326"
    assert patched.json()["data"]["status"] == "blacklist"
    found = (await b.get("/vendors", q="deniz")).json()
    assert [v["name"] for v in found["items"]] == ["Deniz Asansör"]
    assert (await b.get("/vendors", status="blacklist")).json()["total"] == 1
    assert (await b.get("/vendors", q="1234567890")).json()["total"] == 2  # vergi no araması
    # Denetim kaydında IBAN ve vergi no maskeli
    with site_scope(b.w.site_id):
        async with b.w.factory() as session:
            log = await session.scalar(
                select(AuditLog).where(
                    AuditLog.entity == "vendors",
                    AuditLog.entity_id == uuid.UUID(data["id"]),
                    AuditLog.action == "create",
                )
            )
    assert log is not None
    assert log.after is not None
    assert log.after["iban"] == "TR********************1326"
    assert log.after["tax_number"] == "*******8901"
    # Yetki: YK okur, yazamaz
    assert (await b.get("/vendors", b.yk)).status_code == 200
    assert (await _vendor_as(b, b.yk)).status_code == 403
    # Kara listedeki tedarikçiye link/teklif yok
    pr = await open_pr(b)
    link = await b.post(f"/purchase-requests/{pr['id']}/quote-links", {"vendor_id": data["id"]})
    assert "vendor_id" in link.json()["error"]["fields"]


async def _vendor_as(b: Buy, headers: dict[str, str]) -> httpx2.Response:
    return await b.post("/vendors", {"name": "Yeni", "tax_no": "9999999999"}, headers)


# --- Ayar ve bütçe (63) ---------------------------------------------------------------------


async def test_ayarlar_ve_butce(buy: Buy) -> None:
    b = buy
    default = (await b.get("/purchase-settings")).json()
    assert default == {
        "thresholds": [{"max": "10000.00", "quotes": 1}, {"max": "100000.00", "quotes": 2}],
        "above_quotes": 3,
        "tolerance_percent": "1.00",
        "link_days": 7,
    }
    unsorted = await b.w.api.put(
        b.w.url("/purchase-settings"),
        json={
            **default,
            "thresholds": [{"max": "5000.00", "quotes": 2}, {"max": "1000.00", "quotes": 1}],
            "tolerance_percent": 11,
            "link_days": 0,
        },
        headers=b.w.headers,
    )
    assert unsorted.json()["error"]["fields"] == {
        "thresholds": "Eşikler küçükten büyüğe sıralanmalı.",
        "tolerance_percent": "Tolerans %0–10 olmalı (en çok 2 ondalık).",
        "link_days": "Link geçerliliği 1–30 gün olmalı.",
    }
    saved = await b.w.api.put(
        b.w.url("/purchase-settings"),
        json={**default, "tolerance_percent": "2.5"},
        headers=b.w.headers,
    )
    assert saved.json()["data"]["tolerance_percent"] == "2.50"
    no_cat = await b.get("/purchase-budget")
    assert no_cat.json()["error"]["fields"] == {"category_id": "Kategori seçin."}
    spent = await b.w.api.post(
        b.w.url("/expenses"),
        data={
            "expense_category_id": b.category,
            "description": "Elektrik faturası",
            "amount": "1000.00",
            "date": "2026-06-10",
        },
        headers=b.w.headers,
    )
    assert spent.status_code == 201, spent.text
    budget = (await b.get("/purchase-budget", category_id=b.category)).json()
    assert budget == {
        "has_budget": True,
        "annual": "12000.00",
        "spent": "1000.00",
        "committed": "0.00",
        "remaining": "11000.00",
    }
    # Onaylı talep açık taahhüde girer (seçilen teklif tutarıyla)
    await approved_pr(b)
    after = (await b.get("/purchase-budget", category_id=b.category)).json()
    assert (after["committed"], after["remaining"]) == ("14002.00", "-3002.00")
    assert (
        await b.w.api.put(b.w.url("/purchase-settings"), json=default, headers=b.yk)
    ).status_code == 403


# --- Talep, teklif, link, onay (64) ---------------------------------------------------------


async def test_talep_teklif_linki_ve_dogrudan_onay(buy: Buy) -> None:
    b = buy
    invalid = await b.post(
        "/purchase-requests",
        {
            "title": "x",
            "reason": "",
            "category_id": str(uuid.uuid4()),
            "items": [{"description": "", "qty": "0", "est_unit_price": "0.00"}],
        },
    )
    assert set(invalid.json()["error"]["fields"]) == {
        "title",
        "reason",
        "category_id",
        "items.0.description",
        "items.0.qty",
        "items.0.est_unit_price",
    }
    pr = await open_pr(b)
    # 2 × 7.500 + 4 × 0,50 = 15.002 → 2 teklif; bütçe 12.000 → aşım
    assert (pr["number"], pr["estimated_total"], pr["required_quotes"]) == (
        "ST-2026-0001",
        "15002.00",
        2,
    )
    assert pr["over_budget"] is True
    assert pr["budget"]["remaining"] == "12000.00"
    assert pr["items"][1]["stock_item_name"] == "Pompa contası"
    pid = pr["id"]
    staff = await b.post(
        f"/purchase-requests/{pid}/quotes", quote_body(b.vendors["Akın"], ("7000.00", "0.50"))
    )
    assert staff.status_code == 201, staff.text
    assert staff.json()["data"]["total"] == "14002.00"
    twice = await b.post(
        f"/purchase-requests/{pid}/quotes", quote_body(b.vendors["Akın"], ("1.00", "1.00"))
    )
    assert twice.json()["error"]["fields"] == {"vendor_id": "Bu tedarikçinin teklifi var."}
    quoted = await b.post(f"/purchase-requests/{pid}/quote-links", {"vendor_id": b.vendors["Akın"]})
    assert (quoted.status_code, quoted.json()["error"]["code"]) == (409, "quoted")

    link = await b.post(f"/purchase-requests/{pid}/quote-links", {"vendor_id": b.vendors["Bora"]})
    assert link.status_code == 201, link.text
    token = link.json()["data"]["token"]
    assert len(token) >= 43
    again = await b.post(f"/purchase-requests/{pid}/quote-links", {"vendor_id": b.vendors["Bora"]})
    assert again.json()["error"]["code"] == "link_open"
    public = await b.w.api.get(f"/api/v1/public/quote-links/{token}")
    assert public.status_code == 200, public.text
    body = public.json()
    assert (body["site_name"], body["vendor_name"], body["number"]) == (
        "Aksu Konakları",
        "Bora Tesisat",
        "ST-2026-0001",
    )
    assert body["items"] == [
        {"description": "Pompa", "qty": "2", "unit": "adet"},
        {"description": "Conta", "qty": "4", "unit": "adet"},
    ]  # tahmini fiyat, bütçe, diğer teklif yok
    no_ack = await b.w.api.post(
        f"/api/v1/public/quote-links/{token}",
        json={**quote_body("", ("7900.00", "0.60")), "terms_ack": False},
    )
    assert no_ack.json()["error"]["fields"] == {
        "terms_ack": "Teklifin doğruluğunu ve aydınlatma metnini onaylayın."
    }
    sent = await b.w.api.post(
        f"/api/v1/public/quote-links/{token}",
        json={**quote_body("", ("7900.00", "0.60")), "terms_ack": True},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["data"] == {"total": "15802.40"}
    reused = await b.w.api.get(f"/api/v1/public/quote-links/{token}")
    assert (reused.status_code, reused.json()["error"]["code"]) == (410, "link_closed")
    unknown = await b.w.api.get(f"/api/v1/public/quote-links/{'x' * 43}")
    assert unknown.status_code == 404
    # Tedarikçi, gönderimden sonra belgesini bir kez ekler
    doc = await b.w.api.post(
        f"/api/v1/public/quote-links/{token}/file", files={"file": ("teklif.pdf", PDF)}
    )
    assert doc.status_code == 201, doc.text
    second = await b.w.api.post(
        f"/api/v1/public/quote-links/{token}/file", files={"file": ("teklif.pdf", PDF)}
    )
    assert second.status_code == 410

    detail = (await b.get(f"/purchase-requests/{pid}")).json()
    assert detail["quote_count"] == 2
    vendor_quote = next(q for q in detail["quotes"] if q["source"] == "vendor_link")
    assert (vendor_quote["created_by"], vendor_quote["file_name"]) == ("Bora Tesisat", "teklif.pdf")
    assert [link["state"] for link in detail["links"]] == ["used"]
    downloaded = await b.get(f"/files/{vendor_quote['file_id']}")
    assert downloaded.content == PDF

    staff_quote = next(q for q in detail["quotes"] if q["source"] == "staff")["id"]
    selected = await b.post(f"/purchase-requests/{pid}/select-quote", {"quote_id": staff_quote})
    data = selected.json()["data"]
    assert (data["status"], data["approval"]) == ("approval_pending", None)
    own = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "approve"})
    assert (own.status_code, own.json()["error"]["code"]) == (403, "self_approval")
    readonly = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "approve"}, b.yk)
    assert readonly.status_code == 403
    no_note = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "reject"}, b.muhasebe)
    assert no_note.json()["error"]["fields"] == {"note": "Ret gerekçesi yazın."}
    rejected = await b.post(
        f"/purchase-requests/{pid}/decide",
        {"decision": "reject", "note": "Bütçe yetersiz"},
        b.muhasebe,
    )
    final = rejected.json()["data"]
    assert (final["status"], final["reject_reason"]) == ("rejected", "Bütçe yetersiz")
    assert [(d["stage"], d["decision"], d["by"]) for d in final["decisions"]] == [
        ("purchase", "reject", "Mehmet MUHASEBE")
    ]
    order = await b.post("/purchase-orders", {"pr_id": pid}, b.muhasebe)
    assert (order.status_code, order.json()["error"]["code"]) == (409, "not_approved")
    cancel = await b.post(f"/purchase-requests/{pid}/cancel")
    assert cancel.json()["error"]["code"] == "invalid_status"
    listed = (await b.get("/purchase-requests", status="rejected")).json()
    assert [(r["number"], r["quote_count"]) for r in listed["items"]] == [("ST-2026-0001", 2)]


async def test_eksik_teklif_istisnasi_ve_iptal(buy: Buy) -> None:
    b = buy
    pr = await open_pr(b)
    pid = pr["id"]
    quote = await b.post(
        f"/purchase-requests/{pid}/quotes", quote_body(b.vendors["Akın"], ("7000.00", "0.50"))
    )
    qid = quote.json()["data"]["id"]
    short = await b.post(f"/purchase-requests/{pid}/select-quote", {"quote_id": qid})
    assert short.status_code == 422
    assert short.json()["error"]["code"] == "quote_exception"
    assert short.json()["error"]["message"].startswith("2 teklif gerekiyor, 1 var.")
    reason = {"quote_id": qid, "exception_reason": "Bölgede tek yetkili servis var."}
    sent = await b.post(f"/purchase-requests/{pid}/select-quote", reason)
    assert sent.json()["data"]["status"] == "exception_pending"
    back = await b.post(
        f"/purchase-requests/{pid}/decide",
        {"decision": "reject", "note": "İkinci teklif alın"},
        b.muhasebe,
    )
    assert back.json()["data"]["status"] == "quoting"
    assert back.json()["data"]["selected_quote_id"] is None
    await b.post(f"/purchase-requests/{pid}/select-quote", reason)
    ok = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "approve"}, b.muhasebe)
    assert ok.json()["data"]["status"] == "approval_pending"
    link = await b.post(f"/purchase-requests/{pid}/quote-links", {"vendor_id": b.vendors["Bora"]})
    assert link.json()["error"]["code"] == "invalid_status"
    cancelled = await b.post(f"/purchase-requests/{pid}/cancel")
    assert cancelled.json()["data"]["status"] == "cancelled"
    # Açık link iptalde kapanır
    other = await open_pr(b)
    token = (
        await b.post(
            f"/purchase-requests/{other['id']}/quote-links", {"vendor_id": b.vendors["Bora"]}
        )
    ).json()["data"]["token"]
    await b.post(f"/purchase-requests/{other['id']}/cancel")
    assert (await b.w.api.get(f"/api/v1/public/quote-links/{token}")).status_code == 410


# --- Onay kuralı bağlantısı ---------------------------------------------------------------


async def _rule(b: Buy, action: str) -> None:
    rule = await b.post(
        "/approval-rules",
        {
            "action_type": action,
            "steps": [{"roles": ["Yönetim Kurulu Üyesi"], "quorum": "any"}],
            "remind_after_days": 2,
            "escalate_after_days": 5,
            "effective_from": "2026-06-20",
        },
    )
    assert rule.status_code == 201, rule.text


async def test_onay_kurali_satin_alma_ve_siparis(buy: Buy) -> None:
    b = buy
    await _rule(b, "purchase_request")
    await _rule(b, "purchase_order")
    types = (await b.get("/approval-action-types")).json()
    connected = {t["key"] for t in types if t["connected"]}
    assert {
        "purchase_request",
        "purchase_order",
        "quote_exception",
        "invoice_difference",
    } <= connected
    pr = await open_pr(b)
    pid = pr["id"]
    for vendor, p in (
        (b.vendors["Akın"], ("7000.00", "0.50")),
        (b.vendors["Bora"], ("7900.00", "0.60")),
    ):
        await b.post(f"/purchase-requests/{pid}/quotes", quote_body(vendor, p))
    first = (await b.get(f"/purchase-requests/{pid}")).json()["quotes"][0]["id"]
    selected = (await b.post(f"/purchase-requests/{pid}/select-quote", {"quote_id": first})).json()
    approval = selected["data"]["approval"]
    assert approval["action_type"] == "purchase_request"
    assert "Onaylar ekranına" in selected["message"]
    # Muhasebe onaycı değil (kural YK'ya); doğrudan karar yok
    blocked = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "approve"}, b.muhasebe)
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (403, "not_an_approver")
    decided = await b.post(f"/approvals/{approval['id']}/approve", {}, b.yk)
    assert decided.json()["data"]["status"] == "executed", decided.text
    detail = (await b.get(f"/purchase-requests/{pid}")).json()
    assert (detail["status"], detail["approval"]) == ("approved", None)
    assert [(d["stage"], d["decision"], d["by"]) for d in detail["decisions"]] == [
        ("purchase", "approve", "Yasemin YK")
    ]
    pending = await b.post("/purchase-orders", {"pr_id": pid}, b.muhasebe)
    assert pending.status_code == 202, pending.text
    write = pending.json()["data"]
    assert (write["status"], write["order"]) == ("approval_pending", None)
    again = await b.post("/purchase-orders", {"pr_id": pid}, b.muhasebe)
    assert again.json()["error"]["code"] == "approval_pending"
    # Kararı talep ekranının aynı ucu da verebilir (onay mekanizmasına iletilir) — burada YK
    done = await b.post(f"/approvals/{write['approval_request_id']}/approve", {}, b.yk)
    assert done.json()["data"]["status"] == "executed", done.text
    detail = (await b.get(f"/purchase-requests/{pid}")).json()
    assert (detail["status"], detail["order_number"]) == ("ordered", "SIP-2026-0001")


async def test_onay_kurali_ile_istisna_talep_ekranindan(buy: Buy) -> None:
    b = buy
    await _rule(b, "quote_exception")
    pr = await open_pr(b)
    pid = pr["id"]
    qid = (
        await b.post(
            f"/purchase-requests/{pid}/quotes", quote_body(b.vendors["Akın"], ("7000.00", "0.50"))
        )
    ).json()["data"]["id"]
    sent = await b.post(
        f"/purchase-requests/{pid}/select-quote",
        {"quote_id": qid, "exception_reason": "Tek yetkili servis var."},
    )
    assert sent.json()["data"]["approval"]["action_type"] == "quote_exception"
    # YK talep ekranından karar verir (expenses.manage yok; onaycı olduğu için geçer)
    approved = await b.post(f"/purchase-requests/{pid}/decide", {"decision": "approve"}, b.yk)
    assert approved.status_code == 200, approved.text
    data = approved.json()["data"]
    # Satın alma için kural yok → doğrudan akış
    assert (data["status"], data["approval"]) == ("approval_pending", None)
    assert data["decisions"][0]["stage"] == "exception"


# --- Sipariş, kabul, fatura, ödeme (65–67) --------------------------------------------------


async def _stock(b: Buy) -> Decimal:
    with site_scope(b.w.site_id):
        async with b.w.factory() as session:
            item = await session.get(StockItem, uuid.UUID(b.stock_id))
            assert item is not None
            return item.quantity


async def test_siparis_kabul_fatura_odeme(buy: Buy) -> None:
    b = buy
    pr = await approved_pr(b)
    yk_order = await b.post("/purchase-orders", {"pr_id": pr["id"]}, b.yk)
    assert yk_order.status_code == 403
    created = await b.post("/purchase-orders", {"pr_id": pr["id"]}, b.muhasebe)
    assert created.status_code == 201, created.text
    order = created.json()["data"]["order"]
    oid = order["id"]
    assert (order["number"], order["total"], order["status"]) == (
        "SIP-2026-0001",
        "14002.00",
        "sent",
    )
    assert [(x["qty"], x["unit_price"]) for x in order["lines"]] == [
        ("2", "7000.00"),
        ("4", "0.50"),
    ]
    early = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "abc-1",
            "date": "2026-06-19",
            "due_date": "2026-07-19",
            "lines": [{"qty": 0, "unit_price": "7000.00"}, {"qty": 0, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    assert early.json()["error"]["fields"] == {"lines": "Fatura kalemlerini girin."}
    no_receipt = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "abc-1",
            "date": "2026-06-19",
            "due_date": "2026-07-19",
            "lines": [{"qty": 2, "unit_price": "7000.00"}, {"qty": 4, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    assert (no_receipt.status_code, no_receipt.json()["error"]["code"]) == (409, "no_receipt")

    over = await b.post(
        f"/purchase-orders/{oid}/receipts", {"lines": [{"qty": 3}, {"qty": 0}]}, b.muhasebe
    )
    assert over.json()["error"]["fields"] == {
        "lines.0": "Kalan 2; fazla teslim not olarak yazılır."
    }
    bad_rating = await b.post(
        f"/purchase-orders/{oid}/receipts",
        {"lines": [{"qty": 1}, {"qty": 0}], "rating": 6},
        b.muhasebe,
    )
    assert bad_rating.json()["error"]["fields"] == {"rating": "1–5 puan."}
    assert (
        await b.post(f"/purchase-orders/{oid}/receipts", {"lines": [{"qty": 1}]}, b.yk)
    ).status_code == 403
    # Muhasebe rolünün stok yazma yetkisi yok; stok girişi sistem adına yapılır.
    partial = await b.post(
        f"/purchase-orders/{oid}/receipts",
        {"lines": [{"qty": 1}, {"qty": "4"}], "rating": 4, "note": "1 pompa eksik"},
        b.muhasebe,
    )
    assert partial.status_code == 201, partial.text
    assert partial.json()["data"]["status"] == "partial"
    assert "stoğa girdi (1)" in partial.json()["message"]
    assert await _stock(b) == Decimal("4.000")
    with site_scope(b.w.site_id):
        async with b.w.factory() as session:
            moves = list(
                await session.scalars(select(AuditLog).where(AuditLog.entity == "stock_moves"))
            )
    assert [(m.actor_type, m.actor_name, m.user_id) for m in moves] == [
        ("system", "Mal kabulü · SIP-2026-0001", None)
    ]
    assert moves[0].reason == "SIP-2026-0001 kabulü — kabul eden: Mehmet MUHASEBE"
    receipt_id = partial.json()["data"]["receipts"][0]["id"]
    photo = await b.w.api.post(
        b.w.url(f"/purchase-receipts/{receipt_id}/file"),
        files={"file": ("irsaliye.pdf", PDF)},
        headers=b.muhasebe,
    )
    assert photo.status_code == 201, photo.text

    # Kısmi kabulle fatura: miktar farkı (fatura 2, teslim 1) → fark onayına düşer
    diff = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "abc-1",
            "date": "2026-06-19",
            "due_date": "2026-07-19",
            "lines": [{"qty": 2, "unit_price": "7000.00"}, {"qty": 4, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    assert diff.status_code == 201, diff.text
    inv = diff.json()["data"]
    assert (inv["number"], inv["status"], inv["total"], inv["expected_total"]) == (
        "ABC-1",
        "diff_pending",
        "14002.00",
        "7002.00",
    )
    assert inv["diffs"][0] == "Pompa: faturada 2, teslim alınan 1 adet"
    payables = (await b.get("/payables")).json()
    assert payables["invoices"] == []
    pay_diff = await b.post(
        f"/vendor-invoices/{inv['id']}/mark-paid",
        {"paid_on": "2026-06-20", "cash_account_id": str(uuid.uuid4())},
        b.muhasebe,
    )
    assert (pay_diff.status_code, pay_diff.json()["error"]["code"]) == (409, "not_matched")
    own = await b.post(
        f"/vendor-invoices/{inv['id']}/decide",
        {"decision": "approve", "note": "Fark uygun"},
        b.muhasebe,
    )
    assert (own.status_code, own.json()["error"]["code"]) == (403, "self_approval")
    no_note = await b.post(f"/vendor-invoices/{inv['id']}/decide", {"decision": "reject"})
    assert no_note.json()["error"]["fields"] == {"note": "Gerekçe zorunlu."}
    disputed = await b.post(
        f"/vendor-invoices/{inv['id']}/decide",
        {"decision": "reject", "note": "Teslim edilmeyeni faturalamış"},
    )
    assert disputed.json()["data"]["status"] == "disputed"
    duplicate = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "ABC-1",
            "date": "2026-06-19",
            "due_date": "2026-07-19",
            "lines": [{"qty": 1, "unit_price": "7000.00"}, {"qty": 4, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    assert duplicate.json()["error"]["fields"] == {
        "number": "Bu tedarikçinin bu numaralı faturası kayıtlı."
    }

    # Kalan pompa gelir; doğru fatura tolerans içinde (birim fiyat sapması, toplam %1 içinde)
    rest = await b.post(
        f"/purchase-orders/{oid}/receipts",
        {"lines": [{"qty": 1}, {"qty": 0}], "rating": 2},
        b.muhasebe,
    )
    assert rest.json()["data"]["status"] == "received"
    future = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "ABC-2",
            "date": "2026-06-21",
            "due_date": "2026-06-20",
            "lines": [{"qty": 2, "unit_price": "7000.00"}, {"qty": 4, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    assert set(future.json()["error"]["fields"]) == {"date", "due_date"}
    matched = await b.post(
        "/vendor-invoices",
        {
            "order_id": oid,
            "number": "ABC-2",
            "date": "2026-06-19",
            "due_date": "2026-06-19",
            "lines": [{"qty": 2, "unit_price": "7050.00"}, {"qty": 4, "unit_price": "0.50"}],
        },
        b.muhasebe,
    )
    m = matched.json()["data"]
    assert (m["status"], m["total"], m["diffs"]) == ("matched", "14102.00", [])
    order_now = (await b.get(f"/purchase-orders/{oid}")).json()
    assert order_now["status"] == "closed"
    assert [line["invoiced"] for line in order_now["lines"]] == ["2", "4"]
    vendors = (await b.get("/vendors", q="akın")).json()["items"]
    assert (vendors[0]["rating_avg"], vendors[0]["rating_count"], vendors[0]["order_count"]) == (
        "3.0",
        2,
        1,
    )

    invoice_file = await b.w.api.post(
        b.w.url(f"/vendor-invoices/{m['id']}/file"),
        files={"file": ("fatura.pdf", PDF)},
        headers=b.muhasebe,
    )
    assert invoice_file.status_code == 201
    payables = (await b.get("/payables")).json()
    assert [(i["number"], i["overdue"]) for i in payables["invoices"]] == [("ABC-2", True)]
    assert payables["total"] == "14102.00"

    accounts = (await b.get("/cash-accounts")).json()["items"]
    bank = next(a["id"] for a in accounts if a["kind"] == "bank")
    future_pay = await b.post(
        f"/vendor-invoices/{m['id']}/mark-paid",
        {"paid_on": "2026-06-21", "cash_account_id": bank},
        b.muhasebe,
    )
    assert future_pay.json()["error"]["fields"] == {
        "paid_on": "Ödeme tarihi bugünden ileri olamaz."
    }
    key = {**b.muhasebe, "Idempotency-Key": "odeme-abc-2-0001"}
    body = {"paid_on": "2026-06-20", "cash_account_id": bank}
    paid = await b.post(f"/vendor-invoices/{m['id']}/mark-paid", body, key)
    assert paid.status_code == 200, paid.text
    assert paid.json()["data"]["status"] == "paid"
    replay = await b.post(f"/vendor-invoices/{m['id']}/mark-paid", body, key)
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json() == paid.json()
    twice = await b.post(f"/vendor-invoices/{m['id']}/mark-paid", body, b.muhasebe)
    assert twice.json()["error"]["message"] == "Fatura zaten ödendi."
    with site_scope(b.w.site_id):
        async with b.w.factory() as session:
            expenses = list(await session.scalars(select(Expense)))
            movements = list(
                await session.scalars(select(CashMovement).where(CashMovement.source == "expense"))
            )
    assert len(expenses) == 1
    assert len(movements) == 1
    expense = expenses[0]
    assert (
        str(expense.expense_category_id),
        expense.amount,
        expense.vendor,
        expense.document_number,
        expense.paid_on,
        expense.date,
    ) == (
        b.category,
        Decimal("14102.00"),
        "Akın Pompa Ltd.",
        "ABC-2",
        date(2026, 6, 20),
        date(2026, 6, 19),
    )
    assert str(expense.stored_file_id) == invoice_file.json()["data"]["id"]
    assert movements[0].outflow == Decimal("14102.00")
    assert str(paid.json()["data"]["expense_id"]) == str(expense.id)
    # Bütçe: gider gerçekleşti, taahhüt düştü
    budget = (await b.get("/purchase-budget", category_id=b.category)).json()
    assert (budget["spent"], budget["committed"]) == ("14102.00", "0.00")


async def test_fatura_farki_onay_kurali(buy: Buy) -> None:
    b = buy
    await _rule(b, "invoice_difference")
    pr = await approved_pr(b)
    oid = (await b.post("/purchase-orders", {"pr_id": pr["id"]}, b.muhasebe)).json()["data"][
        "order"
    ]["id"]
    await b.post(
        f"/purchase-orders/{oid}/receipts", {"lines": [{"qty": 2}, {"qty": 4}]}, b.muhasebe
    )
    inv = (
        await b.post(
            "/vendor-invoices",
            {
                "order_id": oid,
                "number": "F-9",
                "date": "2026-06-20",
                "due_date": "2026-07-20",
                "lines": [{"qty": 2, "unit_price": "7500.00"}, {"qty": 4, "unit_price": "0.50"}],
            },
            b.muhasebe,
        )
    ).json()
    assert "Onaylar ekranına" in inv["message"]
    data = inv["data"]
    assert data["status"] == "diff_pending"
    assert data["approval_request_id"]
    assert data["diffs"][0] == "Pompa: birim fiyat 7.500,00 TL, siparişte 7.000,00 TL"
    # YK, fatura ekranından (onay mekanizmasıyla) karar verir
    decided = await b.post(
        f"/vendor-invoices/{data['id']}/decide",
        {"decision": "approve", "note": "Fiyat artışı yazılı bildirildi"},
        b.yk,
    )
    assert decided.status_code == 200, decided.text
    out = decided.json()["data"]
    assert (out["status"], out["decision"]["by"]) == ("matched", "Yasemin YK")
    assert (await b.get(f"/purchase-orders/{oid}")).json()["status"] == "closed"


# --- Site izolasyonu ve yetki ---------------------------------------------------------------


async def test_site_izolasyonu(buy: Buy) -> None:
    b = buy
    pr = await approved_pr(b)
    order = (await b.post("/purchase-orders", {"pr_id": pr["id"]}, b.muhasebe)).json()["data"]
    other = "yildiz-sitesi"
    w = b.w
    for path in (
        f"/purchase-requests/{pr['id']}",
        f"/purchase-orders/{order['order']['id']}",
    ):
        response = await w.api.get(w.url(path, other), headers=w.headers)
        assert response.status_code == 404, path
    for path in ("/vendors", "/purchase-requests", "/purchase-orders", "/vendor-invoices"):
        listed = (await w.api.get(w.url(path, other), headers=w.headers)).json()
        assert listed["total"] == 0, path
    patched = await w.api.patch(
        w.url(f"/vendors/{b.vendors['Akın']}", other),
        json={"status": "passive"},
        headers=w.headers,
    )
    assert patched.status_code == 404
    cross = await w.api.post(
        w.url("/purchase-orders", other), json={"pr_id": pr["id"]}, headers=w.headers
    )
    assert cross.status_code == 404
    # Aynı vergi no başka sitede serbest
    assert (
        await w.api.post(
            w.url("/vendors", other),
            json={"name": "Akın Pompa Ltd.", "tax_no": "1234567890"},
            headers=w.headers,
        )
    ).status_code == 201
    # Sakin / yetkisiz: talep açamaz
    _, technician = await _member(w, "teknik@test.local", "Tamer TEKNİK", "Teknik Personel")
    assert (await b.post("/purchase-requests", pr_body(b), technician)).status_code == 403
    assert (await b.post("/purchase-requests", pr_body(b), b.yk)).status_code == 403
    with site_scope(w.site_id):
        async with w.factory() as session:
            assert len(list(await session.scalars(select(Vendor)))) == 3
