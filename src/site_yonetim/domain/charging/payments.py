"""Tahsilatın borçlara dağıtımı (FIFO) ve gecikme tazminatı — saf (docs/04 §5–§6).

Açık borç = borç hareketi − ona daha önce yapılmış mahsuplar. "Daha önce yapılmış mahsuplar
düşülür" kuralı kritiktir: düşülmezse ikinci tahsilat ilk tahsilatın kapattığı borcu tekrar
kapatır (docs/07 §3.8).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from site_yonetim.domain.finance import FinanceRuleError
from site_yonetim.domain.money import EPSILON, ZERO, round_money
from site_yonetim.domain.text import format_date_tr, format_money_tr


class PaymentMethod(StrEnum):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CREDIT_CARD = "credit_card"
    OTHER = "other"


class PaymentStatus(StrEnum):
    PENDING = "pending"  # sakin bildirdi, yönetici onaylamadı
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


METHOD_LABELS = {
    PaymentMethod.CASH: "nakit",
    PaymentMethod.BANK_TRANSFER: "havale/EFT",
    PaymentMethod.CREDIT_CARD: "kredi kartı",
    PaymentMethod.OTHER: "diğer",
}


@dataclass(frozen=True, slots=True)
class OpenDebt:
    entry_id: uuid.UUID
    due_date: date  # vadesi, yoksa hareket tarihi
    is_late_fee: bool
    sequence: int  # oluşturulma sırası (eşitlikte önce oluşturulan önce)
    open_amount: Decimal


@dataclass(frozen=True, slots=True)
class Allocation:
    entry_id: uuid.UUID
    amount: Decimal


@dataclass(frozen=True, slots=True)
class AllocationResult:
    allocations: list[Allocation]
    applied: Decimal
    unapplied: Decimal  # avans: hesapta alacak olarak kalır

    @property
    def closed_debt_count(self) -> int:
        return len(self.allocations)


def check_amount(amount: Decimal) -> None:
    if amount <= ZERO or amount != round_money(amount):
        raise FinanceRuleError(
            "invalid_amount",
            "Tahsilat tutarı sıfırdan büyük olmalı (en fazla 2 ondalık).",
            field="amount",
        )


def allocate(
    amount: Decimal, debts: Sequence[OpenDebt], *, principal_first: bool = False
) -> AllocationResult:
    """En eski vadeden başlayarak dağıtır (docs/04 §5.1).

    Sıra: vade → aynı vadede önce gecikme tazminatı (TBK m.100; `principal_first` tersine
    çevirir) → önce oluşturulan. Her borca `min(kalan, açık)` yazılır. Artan avanstır.
    """
    check_amount(amount)
    ordered = sorted(
        (d for d in debts if d.open_amount > EPSILON),
        key=lambda d: (d.due_date, d.is_late_fee == principal_first, d.sequence),
    )
    remaining = amount
    allocations: list[Allocation] = []
    for debt in ordered:
        if remaining <= ZERO:
            break
        part = min(remaining, debt.open_amount)
        allocations.append(Allocation(debt.entry_id, part))
        remaining -= part
    return AllocationResult(allocations, amount - remaining, remaining)


def payment_description(day: date, method: PaymentMethod, result: AllocationResult) -> str:
    """`15.06.2026 tahsilat (havale/EFT) — 2 borç kaydına mahsup edildi` (docs/04 §5.1)."""
    head = f"{format_date_tr(day)} tahsilat ({METHOD_LABELS[method]})"
    parts = []
    if result.allocations:
        parts.append(f"{result.closed_debt_count} borç kaydına mahsup edildi")
    if result.unapplied > ZERO:
        parts.append(f"{format_money_tr(result.unapplied)} avans olarak kaldı")
    return f"{head} — {', '.join(parts)}"


# --- Gecikme tazminatı (KMK m.20) — docs/04 §6 ---------------------------------------


@dataclass(frozen=True, slots=True)
class LateFeePolicyData:
    monthly_rate_percent: Decimal = Decimal(5)
    grace_days: int = 5
    minimum_amount: Decimal = ZERO
    is_enabled: bool = True


def late_fee(
    outstanding: Decimal, due_date: date, as_of: date, policy: LateFeePolicyData
) -> Decimal:
    """Aylık %5 → günlük aylık/30; tolerans günleri düşülür; asgari tutarın altı sıfır.

    Yalnız hesaplar — deftere ay sonu işi ve ödeme anı yazacak; oran ve yöntemin hukuki
    teyidi açık karar (docs/12 K2). Tazminata tazminat işletilmez: `outstanding` anaparadır.
    """
    if not policy.is_enabled or outstanding <= ZERO:
        return ZERO
    late_days = (as_of - due_date).days - policy.grace_days
    if late_days <= 0:
        return ZERO
    daily_rate = policy.monthly_rate_percent / Decimal(100) / Decimal(30)
    fee = round_money(outstanding * daily_rate * late_days)
    return ZERO if fee < policy.minimum_amount else fee
