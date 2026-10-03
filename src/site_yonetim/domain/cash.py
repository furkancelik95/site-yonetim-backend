"""Kasa/banka ve gider kuralları — saf (docs/04 §9–§10; altın testler docs/07 §4).

"Para nerede" (kasa) ile "kim ne kadar borçlu" (cari) ayrıdır. Kasa hareketi ve gider
**silinmez, düzenlenmez**: düzeltme ters hareket / eksi tutarlı düzeltme kaydıyla yapılır.
"""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import ZERO, round_money
from site_yonetim.domain.text import format_period_tr

ACCOUNT_NAME_MIN, ACCOUNT_NAME_MAX = 2, 60
TEXT_MIN, TEXT_MAX = 3, 200
EXPENSE_MAX = Decimal(100_000_000)
VENDOR_MAX, DOCUMENT_MAX, NOTE_MAX = 100, 50, 500
REASON_MIN = 3
_IBAN_CHARS = re.compile(r"[^A-Z0-9]")


class CashAccountKind(StrEnum):
    CASH = "cash"
    BANK = "bank"


class CashSource(StrEnum):
    MANUAL = "manual"
    PAYMENT = "payment"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    OPENING = "opening"
    REFUND = "refund"  # cari alacağın sakine iadesi (çıkış)


class Direction(StrEnum):
    IN = "in"
    OUT = "out"


# --- Kasa hesabı (§10.1) ---------------------------------------------------------


def clean_account_name(name: str) -> str:
    text = " ".join(name.split())
    if not ACCOUNT_NAME_MIN <= len(text) <= ACCOUNT_NAME_MAX:
        raise FinanceRuleError(
            "invalid_name",
            f"Hesap adı {ACCOUNT_NAME_MIN}–{ACCOUNT_NAME_MAX} karakter olmalı.",
            field="name",
        )
    return text


def clean_iban(value: str | None) -> str | None:
    """Yalnız harf/rakam, büyük harf; `TR` ile başlar, 16–34 karakter (§10.1)."""
    if value is None or not value.strip():
        return None
    iban = _IBAN_CHARS.sub("", value.upper())
    if not (iban.startswith("TR") and 16 <= len(iban) <= 34):
        raise FinanceRuleError(
            "invalid_iban", "IBAN geçersiz görünüyor (TR ile başlamalı).", field="iban"
        )
    return iban


def opening_amounts(opening_balance: Decimal) -> tuple[Decimal, Decimal] | None:
    """Açılış hareketi: pozitifse giriş, negatifse çıkış; sıfırsa hareket yok."""
    if opening_balance == ZERO:
        return None
    return (opening_balance, ZERO) if opening_balance > ZERO else (ZERO, -opening_balance)


# --- Ortak doğrulamalar ------------------------------------------------------------


def clean_text(value: str, field: str, label: str = "Açıklama") -> str:
    text = " ".join(value.split())
    if not TEXT_MIN <= len(text) <= TEXT_MAX:
        raise FinanceRuleError(
            "invalid_text", f"{label} {TEXT_MIN}–{TEXT_MAX} karakter olmalı.", field=field
        )
    return text


def clip(value: str | None, limit: int) -> str | None:
    """Kırpılır ve sınırda kesilir (docs/04 §9.1: tedarikçi, belge no, not)."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def check_positive(amount: Decimal, field: str = "amount") -> None:
    if amount <= ZERO or amount != round_money(amount):
        raise FinanceRuleError(
            "invalid_amount", "Tutar sıfırdan büyük olmalı (en fazla 2 ondalık).", field=field
        )


def check_not_future(day: date, today: date, field: str = "date", *, tolerance: int = 0) -> None:
    if day > today + timedelta(days=tolerance):
        raise FinanceRuleError("date_in_future", "Tarih ileri bir tarih olamaz.", field=field)


def check_reason(reason: str) -> str:
    text = " ".join(reason.split())
    if len(text) < REASON_MIN:
        raise FinanceRuleError(
            "reason_too_short", "Gerekçe en az 3 karakter olmalı.", field="reason"
        )
    return text


def closed_period_error(day: date) -> FinanceRuleError:
    return FinanceRuleError(
        "period_closed",
        f"{format_period_tr(day.year, day.month)} dönemi kapalı, bu tarihe kayıt yazılamaz.",
        conflict=True,
    )


def inactive_account_error() -> FinanceRuleError:
    return FinanceRuleError(
        "cash_account_inactive",
        "Seçilen kasa/banka hesabı kapalı; hareket yazılamaz.",
        field="cash_account_id",
    )


# --- Kasa hareketi geri alma (§10.4) -----------------------------------------------


@dataclass(frozen=True, slots=True)
class MovementState:
    source: CashSource
    is_reversal: bool
    already_reversed: bool


def check_movement_reversible(state: MovementState, reason: str) -> str:
    if state.source in (CashSource.PAYMENT, CashSource.EXPENSE):
        label = "tahsilat" if state.source is CashSource.PAYMENT else "gider"
        raise FinanceRuleError(
            "movement_from_record",
            f"Bu hareket bir {label} kaydından doğdu. Düzeltme o kayıt üzerinden yapılmalı, "
            "yoksa kasa düzelir ama cari hesap yanlış kalır.",
            conflict=True,
        )
    if state.is_reversal:
        raise FinanceRuleError(
            "reversal_not_reversible", "Düzeltme kaydı geri alınamaz.", conflict=True
        )
    if state.already_reversed:
        raise FinanceRuleError(
            "movement_already_reversed", "Bu hareket zaten geri alınmış.", conflict=True
        )
    return check_reason(reason)


# --- Gider (§9) -----------------------------------------------------------------------


def check_expense_amount(amount: Decimal) -> None:
    check_positive(amount)
    if amount > EXPENSE_MAX:
        raise FinanceRuleError(
            "amount_too_large", "Gider tutarı en fazla 100.000.000 TL olabilir.", field="amount"
        )


def check_paid_on(paid_on: date | None, document_date: date, today: date) -> date:
    if paid_on is None:
        raise FinanceRuleError("paid_on_required", "Ödeme tarihini girin.", field="paid_on")
    check_not_future(paid_on, today, "paid_on")
    if paid_on < document_date:
        raise FinanceRuleError(
            "paid_before_document", "Ödeme tarihi belge tarihinden önce olamaz.", field="paid_on"
        )
    return paid_on


def missing_cash_account() -> FinanceRuleError:
    return FinanceRuleError(
        "cash_account_required",
        "Ödendi işaretlenen giderde kasa/banka hesabı seçilmeli.",
        field="cash_account_id",
    )


@dataclass(frozen=True, slots=True)
class ExpenseState:
    is_reversed: bool
    is_reversal: bool
    is_paid: bool


def check_payable(state: ExpenseState) -> None:
    if state.is_reversed or state.is_reversal:
        raise FinanceRuleError("expense_reversed", "Bu gider geri alınmış.", conflict=True)
    if state.is_paid:
        raise FinanceRuleError(
            "expense_already_paid", "Bu gider zaten ödenmiş görünüyor.", conflict=True
        )


def check_expense_reversible(state: ExpenseState, reason: str) -> str:
    if state.is_reversal:
        raise FinanceRuleError(
            "reversal_not_reversible", "Düzeltme kaydı geri alınamaz.", conflict=True
        )
    if state.is_reversed:
        raise FinanceRuleError(
            "expense_already_reversed", "Bu gider zaten geri alınmış.", conflict=True
        )
    return check_reason(reason)


def expense_movement_text(description: str, vendor: str | None) -> str:
    """Kasadan çıkış açıklaması: `{açıklama} · {tedarikçi}` (§9.1)."""
    return f"{description} · {vendor}" if vendor else description
