"""Satın alma — saf kurallar (SAT-01..06; servis istekleri 62–67)."""

from decimal import Decimal

import pytest

from site_yonetim.domain.purchasing import (
    DEFAULT_SETTINGS,
    Budget,
    InvoiceLineInput,
    OrderLineView,
    PurchaseFieldError,
    VendorInput,
    VendorStatus,
    check_settings,
    check_vendor,
    line_total,
    mask_iban_display,
    match_invoice,
    normalize_phone,
    order_number,
    parse_quantity,
    request_number,
    required_quotes,
)

D = Decimal


def vendor(**changes: str | None) -> VendorInput:
    base: dict[str, str | None] = {
        "name": "Akın Pompa",
        "tax_no": "1234567890",
        "phone": None,
        "email": None,
        "iban": None,
        "category": None,
        "status": None,
        "note": None,
    }
    base.update(changes)
    return VendorInput(**base)


@pytest.mark.parametrize(
    ("total", "quotes"),
    [
        (D("0.01"), 1),
        (D("9999.99"), 1),
        (D("10000.00"), 2),  # eşik "altındaysa": 10.000 dahil değil
        (D("99999.99"), 2),
        (D("100000.00"), 3),
        (D("5000000"), 3),
    ],
)
def test_varsayilan_teklif_esikleri(total: Decimal, quotes: int) -> None:
    assert required_quotes(DEFAULT_SETTINGS, total) == quotes


def test_ayar_dogrulama() -> None:
    settings = check_settings([(D("5000"), 1), (D("50000.004"), 2)], 4, D("0.5"), 10)
    assert [t.max for t in settings.thresholds] == [D("5000.00"), D("50000.00")]
    assert required_quotes(settings, D("70000")) == 4
    with pytest.raises(PurchaseFieldError) as exc:
        check_settings([(D("5000"), 2), (D("1000"), 1)], 11, D("10.5"), 31)
    assert set(exc.value.fields) == {"thresholds", "above_quotes", "tolerance_percent", "link_days"}
    with pytest.raises(PurchaseFieldError) as exc:
        check_settings([], 1, D("0.123"), 7)
    assert set(exc.value.fields) == {"thresholds", "tolerance_percent"}


def test_tedarikci_karti_normalizasyonu() -> None:
    checked = check_vendor(
        vendor(
            name="  Akın   Pompa ",
            tax_no="123 456 7890",
            phone="0 (212) 555 11 22",
            email=" Bilgi@Akin.COM ",
            iban="tr33 0006 1005 1978 6457 8413 26",
            status="passive",
        )
    )
    assert (checked.name, checked.tax_number, checked.phone, checked.email, checked.iban) == (
        "Akın Pompa",
        "1234567890",
        "+902125551122",
        "bilgi@akin.com",
        "TR330006100519786457841326",
    )
    assert checked.status is VendorStatus.PASSIVE
    assert check_vendor(vendor(tax_no="12345678901")).tax_number == "12345678901"  # T.C. kimlik
    with pytest.raises(PurchaseFieldError) as exc:
        check_vendor(vendor(name="A", tax_no="12", iban="TR330006100519786457841327", status="x"))
    assert set(exc.value.fields) == {"name", "tax_no", "iban", "status"}


def test_telefon_ve_iban_maskesi() -> None:
    assert normalize_phone("+90 532 123 45 67") == "+905321234567"
    with pytest.raises(ValueError, match="10 hane"):
        normalize_phone("123")
    assert mask_iban_display("TR330006100519786457841326") == "TR33 •••• •••• 1326"
    assert mask_iban_display(None) is None


def test_miktar_ve_tutar() -> None:
    assert parse_quantity("2,5", allow_zero=False) == D("2.500")
    assert parse_quantity(0, allow_zero=True) == D("0.000")
    for bad in ("0", "-1", "1.0001", "abc", None, True):
        with pytest.raises(ValueError, match=r".*"):
            parse_quantity(bad, allow_zero=False)
    assert line_total(D("3"), D("0.335")) == D("1.01")  # 1,005 → yarım yukarı
    assert (request_number(2026, 7), order_number(2026, 12)) == ("ST-2026-0007", "SIP-2026-0012")


def test_butce_asimi() -> None:
    budget = Budget(has_budget=True, annual=D("12000"), spent=D("1000"), committed=D("2000"))
    assert budget.remaining == D("9000")
    assert budget.exceeded_by(D("9000.01"))
    assert not budget.exceeded_by(D("9000"))
    assert not Budget(False, D(0), D("5"), D(0)).exceeded_by(D("1000000"))


def _order() -> list[OrderLineView]:
    return [
        OrderLineView("Pompa", "adet", D("7000.00"), D("2")),
        OrderLineView("Conta", "adet", D("0.50"), D("4")),
    ]


def test_ucli_eslesme_tam_ve_tolerans_icinde() -> None:
    exact = match_invoice(
        _order(), [InvoiceLineInput(D("2"), D("7000")), InvoiceLineInput(D("4"), D("0.5"))], D(1)
    )
    assert (exact.matched, exact.total, exact.expected, exact.diffs) == (
        True,
        D("14002.00"),
        D("14002.00"),
        [],
    )
    # Birim fiyat sapması toplam %1 içinde → eşleşir, fark listesine yazılmaz
    near = match_invoice(
        _order(), [InvoiceLineInput(D("2"), D("7070")), InvoiceLineInput(D("4"), D("0.5"))], D(1)
    )
    assert (near.matched, near.total, near.diffs) == (True, D("14142.00"), [])


def test_ucli_eslesme_farklari() -> None:
    # SAT-05 S1: 10 sipariş, 8 kabul, 10 fatura → eşleşmez
    order = [OrderLineView("Boya", "kutu", D("100.00"), D("8"))]
    qty = match_invoice(order, [InvoiceLineInput(D("10"), D("100"))], D(1))
    assert not qty.matched
    assert qty.diffs == [
        "Boya: faturada 10, teslim alınan 8 kutu",
        "Toplam 1.000,00 TL, beklenen 800,00 TL (tolerans %1)",
    ]
    price = match_invoice(
        _order(), [InvoiceLineInput(D("2"), D("7500")), InvoiceLineInput(D("4"), D("0.5"))], D(1)
    )
    assert not price.matched
    assert price.diffs == [
        "Pompa: birim fiyat 7.500,00 TL, siparişte 7.000,00 TL",
        "Toplam 15.002,00 TL, beklenen 14.002,00 TL (tolerans %1)",
    ]
    # Tolerans sınırı dahil: 14.002 × %1 = 140,02
    edge = match_invoice(
        _order(), [InvoiceLineInput(D("2"), D("7070.01")), InvoiceLineInput(D("4"), D("0.5"))], D(1)
    )
    assert edge.matched
    assert edge.total - edge.expected == D("140.02")
