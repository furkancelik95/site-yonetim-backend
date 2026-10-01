"""Kasa, gider ve belge kuralları — saf (docs/04 §9–§10, docs/09 §3)."""

from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.cash import (
    CashSource,
    ExpenseState,
    MovementState,
    check_expense_amount,
    check_expense_reversible,
    check_movement_reversible,
    check_not_future,
    check_paid_on,
    check_payable,
    clean_account_name,
    clean_iban,
    clean_text,
    clip,
    closed_period_error,
    expense_movement_text,
    opening_amounts,
)
from site_yonetim.domain.files import check_document, clean_file_name
from site_yonetim.domain.finance import FinanceRuleError

TODAY = date(2026, 10, 1)


def code(exc: pytest.ExceptionInfo[FinanceRuleError]) -> str:
    return exc.value.code


def test_account_name_and_iban() -> None:
    assert clean_account_name("  Ziraat   Bankası ") == "Ziraat Bankası"
    with pytest.raises(FinanceRuleError):
        clean_account_name("Z")
    assert clean_iban("tr33 0006 1005 1978 6457 8413 26") == "TR330006100519786457841326"
    assert clean_iban("  ") is None
    for bad in ("DE89370400440532013000", "TR12345"):
        with pytest.raises(FinanceRuleError) as caught:
            clean_iban(bad)
        assert caught.value.message == "IBAN geçersiz görünüyor (TR ile başlamalı)."


def test_opening_movement() -> None:
    assert opening_amounts(Decimal(0)) is None
    assert opening_amounts(Decimal(100)) == (Decimal(100), Decimal(0))
    assert opening_amounts(Decimal(-50)) == (Decimal(0), Decimal(50))


def test_texts_and_dates() -> None:
    assert clean_text("  Asansör   bakımı ", "description") == "Asansör bakımı"
    with pytest.raises(FinanceRuleError):
        clean_text("ab", "description")
    assert clip("  " + "x" * 120, 100) == "x" * 100
    assert clip("   ", 10) is None
    assert clip(None, 10) is None
    check_not_future(date(2026, 10, 2), TODAY, tolerance=1)
    with pytest.raises(FinanceRuleError):
        check_not_future(date(2026, 10, 2), TODAY)
    assert closed_period_error(date(2026, 9, 5)).message == (
        "09/2026 dönemi kapalı, bu tarihe kayıt yazılamaz."
    )


def test_expense_amount_limits() -> None:
    check_expense_amount(Decimal("100000000.00"))
    for bad in ("0", "-1", "100000000.01", "1.005"):
        with pytest.raises(FinanceRuleError):
            check_expense_amount(Decimal(bad))


def test_paid_on_rules() -> None:
    document = date(2026, 9, 20)
    assert check_paid_on(date(2026, 9, 25), document, TODAY) == date(2026, 9, 25)
    with pytest.raises(FinanceRuleError) as caught:
        check_paid_on(date(2026, 9, 19), document, TODAY)
    assert caught.value.message == "Ödeme tarihi belge tarihinden önce olamaz."
    with pytest.raises(FinanceRuleError):
        check_paid_on(None, document, TODAY)
    with pytest.raises(FinanceRuleError):
        check_paid_on(date(2026, 10, 2), document, TODAY)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (ExpenseState(is_reversed=True, is_reversal=False, is_paid=False), "expense_reversed"),
        (ExpenseState(is_reversed=False, is_reversal=True, is_paid=False), "expense_reversed"),
        (ExpenseState(is_reversed=False, is_reversal=False, is_paid=True), "expense_already_paid"),
    ],
)
def test_pay_rejections(state: ExpenseState, expected: str) -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_payable(state)
    assert code(caught) == expected


def test_expense_reverse_rules() -> None:
    ok = ExpenseState(is_reversed=False, is_reversal=False, is_paid=True)
    assert check_expense_reversible(ok, "  Hatalı   fatura ") == "Hatalı fatura"
    with pytest.raises(FinanceRuleError) as caught:
        check_expense_reversible(ok, "ab")
    assert code(caught) == "reason_too_short"
    with pytest.raises(FinanceRuleError) as caught:
        check_expense_reversible(ExpenseState(True, False, True), "Hata var")
    assert code(caught) == "expense_already_reversed"
    with pytest.raises(FinanceRuleError) as caught:
        check_expense_reversible(ExpenseState(False, True, False), "Hata var")
    assert caught.value.message == "Düzeltme kaydı geri alınamaz."


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (MovementState(CashSource.EXPENSE, False, False), "movement_from_record"),
        (MovementState(CashSource.PAYMENT, False, False), "movement_from_record"),
        (MovementState(CashSource.MANUAL, True, False), "reversal_not_reversible"),
        (MovementState(CashSource.MANUAL, False, True), "movement_already_reversed"),
    ],
)
def test_movement_reverse_rejections(state: MovementState, expected: str) -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_movement_reversible(state, "Gerekçe")
    assert code(caught) == expected


def test_4_8_message_mentions_gider() -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_movement_reversible(MovementState(CashSource.EXPENSE, False, False), "Gerekçe")
    assert "gider" in caught.value.message
    assert (
        check_movement_reversible(MovementState(CashSource.TRANSFER, False, False), "Hata")
        == "Hata"
    )


def test_movement_text() -> None:
    assert expense_movement_text("Asansör bakımı", "Kone") == "Asansör bakımı · Kone"
    assert expense_movement_text("Asansör bakımı", None) == "Asansör bakımı"


# --- belge -----------------------------------------------------------------------------


def test_file_name_is_cleaned() -> None:
    assert clean_file_name("../../gizli fatura.pdf") == "gizli fatura.pdf"
    assert clean_file_name("C:\\dosyalar\\fa<tu>ra.pdf") == "fa_tu_ra.pdf"
    assert clean_file_name(None) == "belge"
    assert len(clean_file_name("a" * 300 + ".pdf")) == 120


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("fatura.pdf", b"%PDF-1.4 ...", "application/pdf"),
        ("foto.JPG", b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        ("ekran.png", b"\x89PNG\r\n\x1a\n", "image/png"),
        ("resim.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    ],
)
def test_valid_documents(name: str, data: bytes, content_type: str) -> None:
    _, kind = check_document(name, data)
    assert kind.content_type == content_type


def test_4_13_wrong_content_is_rejected() -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_document("fatura.pdf", b"<script>alert(1)</script>")
    assert caught.value.code == "invalid_file_content"


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("fatura.exe", b"%PDF", "invalid_file_type"),
        ("fatura", b"%PDF", "invalid_file_type"),
        ("fatura.pdf", b"", "empty_file"),
        ("foto.png", b"%PDF-1.4", "invalid_file_content"),
    ],
)
def test_document_rejections(name: str, data: bytes, expected: str) -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_document(name, data)
    assert caught.value.code == expected


def test_document_over_10_mb_is_rejected() -> None:
    with pytest.raises(FinanceRuleError) as caught:
        check_document("fatura.pdf", b"%PDF" + b"0" * (10 * 1024 * 1024))
    assert caught.value.code == "file_too_large"
