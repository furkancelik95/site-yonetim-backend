"""FIFO mahsup ve gecikme tazminatı — saf (docs/04 §5.1, §6; docs/07 §3.5–3.9, 3.11–3.12)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from site_yonetim.domain.charging.payments import (
    AllocationResult,
    LateFeePolicyData,
    OpenDebt,
    PaymentMethod,
    allocate,
    late_fee,
    payment_description,
)
from site_yonetim.domain.finance import FinanceRuleError


def debt(amount: str, due: date, *, fee: bool = False, seq: int = 0) -> OpenDebt:
    return OpenDebt(uuid.uuid7(), due, fee, seq, Decimal(amount))


JUNE = date(2026, 6, 15)


def test_3_5_tahsilat_borcu_kapatir() -> None:
    result = allocate(Decimal(1000), [debt("1000", JUNE)])
    assert (result.applied, result.unapplied, result.closed_debt_count) == (
        Decimal(1000),
        Decimal(0),
        1,
    )


def test_3_6_kismi_odeme_kalan_borcu_birakir() -> None:
    result = allocate(Decimal(400), [debt("1000", JUNE)])
    assert result.allocations[0].amount == Decimal(400)
    assert result.unapplied == Decimal(0)


def test_3_7_fazla_odeme_avans_kalir() -> None:
    result = allocate(Decimal(1500), [debt("1000", JUNE)])
    assert (result.applied, result.unapplied) == (Decimal(1000), Decimal(500))


@pytest.mark.parametrize("amount", ["0", "-50", "10.005"])
def test_3_9_sifir_veya_negatif_tutar_reddedilir(amount: str) -> None:
    with pytest.raises(FinanceRuleError) as caught:
        allocate(Decimal(amount), [debt("1000", JUNE)])
    assert caught.value.field == "amount"


def test_oldest_due_first_and_fee_before_principal() -> None:
    july = debt("1000", date(2026, 7, 15), seq=1)
    june_principal = debt("1000", JUNE, seq=2)
    june_fee = debt("25", JUNE, fee=True, seq=3)
    result = allocate(Decimal(1100), [july, june_principal, june_fee])
    assert [(a.entry_id, a.amount) for a in result.allocations] == [
        (june_fee.entry_id, Decimal(25)),
        (june_principal.entry_id, Decimal(1000)),
        (july.entry_id, Decimal(75)),
    ]


def test_principal_first_setting() -> None:
    principal = debt("1000", JUNE, seq=1)
    fee = debt("25", JUNE, fee=True, seq=2)
    result = allocate(Decimal(1000), [fee, principal], principal_first=True)
    assert [a.entry_id for a in result.allocations] == [principal.entry_id]


def test_same_due_earlier_created_first_and_closed_debts_skipped() -> None:
    first, second = debt("100", JUNE, seq=1), debt("100", JUNE, seq=2)
    closed = debt("0.004", date(2026, 1, 1))
    result = allocate(Decimal(150), [second, closed, first])
    assert [(a.entry_id, a.amount) for a in result.allocations] == [
        (first.entry_id, Decimal(100)),
        (second.entry_id, Decimal(50)),
    ]


def test_no_open_debt_everything_is_advance() -> None:
    result = allocate(Decimal(300), [])
    assert (result.applied, result.unapplied) == (Decimal(0), Decimal(300))
    assert result.allocations == []


def test_descriptions() -> None:
    day = date(2026, 6, 15)
    two = AllocationResult([], Decimal(0), Decimal(0))
    closed = allocate(Decimal(2000), [debt("1000", JUNE), debt("1000", JUNE)])
    assert payment_description(day, PaymentMethod.BANK_TRANSFER, closed) == (
        "15.06.2026 tahsilat (havale/EFT) — 2 borç kaydına mahsup edildi"
    )
    advance = allocate(Decimal(1500), [debt("1000", JUNE)])
    assert payment_description(day, PaymentMethod.CASH, advance) == (
        "15.06.2026 tahsilat (nakit) — 1 borç kaydına mahsup edildi, 500,00 TL avans olarak kaldı"
    )
    only_advance = allocate(Decimal(500), [])
    assert payment_description(day, PaymentMethod.CREDIT_CARD, only_advance).endswith(
        "— 500,00 TL avans olarak kaldı"
    )
    assert two.closed_debt_count == 0


# --- gecikme tazminatı ----------------------------------------------------------------

POLICY = LateFeePolicyData()


def test_3_11_gecikme_tazminati() -> None:
    assert late_fee(Decimal(1000), date(2026, 6, 10), date(2026, 6, 30), POLICY) == Decimal("25.00")


def test_3_12_tolerans_icinde_tazminat_yok() -> None:
    assert late_fee(Decimal(1000), date(2026, 6, 10), date(2026, 6, 14), POLICY) == 0


def test_late_fee_edges() -> None:
    due, later = date(2026, 6, 10), date(2026, 6, 30)
    assert late_fee(Decimal(0), due, later, POLICY) == 0
    assert late_fee(Decimal(1000), due, later, LateFeePolicyData(is_enabled=False)) == 0
    assert late_fee(Decimal(1000), due, later, LateFeePolicyData(minimum_amount=Decimal(30))) == 0
