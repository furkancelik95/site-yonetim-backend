"""Tahakkuk testleri için ortak test sitesi — docs/07-test-senaryolari.md §0.

Kurgu verisi; tahakkuk motoru geldiğinde genişletilecek.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SampleUnit:
    block: str
    number: int
    type_name: str
    type_weight: Decimal
    gross_area: Decimal
    land_share: Decimal  # arsa payı oranı (pay / 1000)
    has_tenant: bool


def _a_block(number: int) -> SampleUnit:
    if number % 2 == 0:
        return SampleUnit(
            "A", number, "2+1", Decimal("1.3"), Decimal(110), Decimal(45) / 1000, True
        )
    return SampleUnit("A", number, "1+1", Decimal("1.0"), Decimal(75), Decimal(33) / 1000, False)


def _b_block(number: int) -> SampleUnit:
    tenant = number % 2 == 0
    if number % 3 == 0:
        return SampleUnit(
            "B", number, "2+1", Decimal("1.3"), Decimal(105), Decimal(42) / 1000, tenant
        )
    return SampleUnit("B", number, "1+1", Decimal("1.0"), Decimal(72), Decimal(31) / 1000, tenant)


TEST_SITE_UNITS: tuple[SampleUnit, ...] = tuple(
    [_a_block(n) for n in range(1, 13)] + [_b_block(n) for n in range(1, 13)]
)
