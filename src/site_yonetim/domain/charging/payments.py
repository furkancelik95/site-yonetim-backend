"""Tahsilatın borçlara dağıtımı (FIFO) ve gecikme tazminatı — saf (docs/04 §5–§6).

Açık borç = borç hareketi − ona daha önce yapılmış mahsuplar. "Daha önce yapılmış mahsuplar
düşülür" kuralı kritiktir: düşülmezse ikinci tahsilat ilk tahsilatın kapattığı borcu tekrar
kapatır (docs/07 §3.8).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
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


@dataclass(frozen=True, slots=True)
class LateFeeRule:
    """Sitenin gecikme yöntemi (FIN-03, §8.8; finans parametreleri `late_fee_*` — K2)."""

    monthly_rate_percent: Decimal
    grace_days: int
    method: str = "daily"  # daily: oran × gün / 30 · full_month: başlamış ay = tam ay
    cap_percent: int = 0  # tavan: ana borcun %X'i (0 = yok)
    minimum_amount: Decimal = ZERO


def accrued_late_fee(
    principal: Decimal,
    due_date: date,
    reductions: Sequence[tuple[date, Decimal]],
    as_of: date,
    rule: LateFeeRule,
    *,
    start_floor: date | None = None,
) -> Decimal:
    """Bir ana borca `as_of` gününe kadar işlemiş **toplam** gecikme (basit; FIN-03 formülleri).

    - Gecikme günü = gün − (vade + tolerans); ≤ 0 ise yok. `as_of` günü sayılır.
    - Kısmi ödemede kalan ana borç üzerinden günlük dilimlerle: `reductions` (tarih, tutar)
      o günün **ertesinden** itibaren açık tutarı düşürür (ödeme günü hâlâ gecikmiş sayılır).
    - `daily`: Σ açık × aylık oran / 30 · `full_month`: her başlamış 30 günlük dilim için o dilimin
      başındaki açık × aylık oran.
    - `start_floor`: yöntem yürürlüğü — bu günden önceki günlere gecikme işletilmez.
    - Tavan ve asgari tutar en sonda uygulanır. Tazminata tazminat işletilmez.
    """
    start = due_date + timedelta(days=rule.grace_days)  # bu gün dahil gecikme yok
    if as_of <= start or principal <= ZERO:
        return ZERO
    ordered = sorted(reductions)

    def open_on(day: date) -> Decimal:
        paid = sum((amount for when, amount in ordered if when < day), ZERO)
        return max(principal - paid, ZERO)

    rate = rule.monthly_rate_percent / Decimal(100)
    total = ZERO
    if rule.method == "full_month":
        late_days = (as_of - start).days
        months = -(-late_days // 30)
        for k in range(months):
            first_day = start + timedelta(days=30 * k + 1)
            if start_floor is not None and first_day < start_floor:
                continue
            total += open_on(first_day) * rate
    else:
        first = start + timedelta(days=1)
        if start_floor is not None and first < start_floor:
            first = start_floor
        # açık tutarın değiştiği günlerle dilimlere böl (gün gün değil)
        bounds = sorted(
            {
                first,
                *(
                    when + timedelta(days=1)
                    for when, _ in ordered
                    if first < when + timedelta(days=1) <= as_of
                ),
            }
        )
        for index, segment_start in enumerate(bounds):
            if segment_start > as_of:
                break
            segment_end = (
                bounds[index + 1] if index + 1 < len(bounds) else as_of + timedelta(days=1)
            )
            days = (segment_end - segment_start).days
            total += open_on(segment_start) * rate / Decimal(30) * days
    fee = round_money(total)
    if rule.cap_percent > 0:
        fee = min(fee, round_money(principal * Decimal(rule.cap_percent) / Decimal(100)))
    return ZERO if fee < rule.minimum_amount else fee
