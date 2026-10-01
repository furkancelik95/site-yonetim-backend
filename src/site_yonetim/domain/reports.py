"""Gelir–gider raporu ve tahsilat özeti — saf (docs/04 §12).

Gelir = **fiilen tahsil edilen para** (onaylı tahsilat + elle kasa girişi), kesilen tahakkuk değil.
Gider = gerçekleşen giderler (geri alınan ve düzeltme kayıtları hariç), belge tarihine göre.
Toplamalar veritabanında yapılır; bu modül yalnız biçimler (12 ay, paylar, karşılaştırma).
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from site_yonetim.domain.money import ZERO

HUNDRED = Decimal(100)
_RATE = Decimal("0.01")


def percent(part: Decimal, whole: Decimal) -> Decimal:
    """Yüzde, 2 ondalık; bütün sıfırsa 0."""
    if whole == ZERO:
        return ZERO.quantize(_RATE)
    return (part * HUNDRED / whole).quantize(_RATE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class MonthRow:
    month: int
    income: Decimal
    expense: Decimal

    @property
    def difference(self) -> Decimal:
        return self.income - self.expense


def months(income: Mapping[int, Decimal], expense: Mapping[int, Decimal]) -> list[MonthRow]:
    """12 ay; veri olmayan aylar sıfırla gelir."""
    return [MonthRow(m, income.get(m, ZERO), expense.get(m, ZERO)) for m in range(1, 13)]


@dataclass(frozen=True, slots=True)
class CategoryAmount:
    category_id: uuid.UUID
    name: str
    amount: Decimal
    count: int


@dataclass(frozen=True, slots=True)
class CategoryShare:
    category_id: uuid.UUID
    name: str
    amount: Decimal
    count: int
    share: Decimal  # %


def category_shares(rows: Iterable[CategoryAmount]) -> list[CategoryShare]:
    items = list(rows)
    total = sum((r.amount for r in items), ZERO)
    return [
        CategoryShare(r.category_id, r.name, r.amount, r.count, percent(r.amount, total))
        for r in sorted(items, key=lambda r: (-r.amount, r.name))
    ]


@dataclass(frozen=True, slots=True)
class BudgetLine:
    category_id: uuid.UUID
    name: str
    budgeted: Decimal
    actual: Decimal

    @property
    def difference(self) -> Decimal:
        return self.budgeted - self.actual

    @property
    def usage(self) -> Decimal:
        return percent(self.actual, self.budgeted)

    @property
    def is_over(self) -> bool:
        return self.actual > self.budgeted


def budget_comparison(
    budgeted: Mapping[uuid.UUID, Decimal],
    actual: Mapping[uuid.UUID, Decimal],
    names: Mapping[uuid.UUID, str],
) -> list[BudgetLine]:
    """Kategori başına bütçe × gerçekleşen. Bütçede olmayan ama harcama yapılmış kategoriler de
    bütçe 0 ile listelenir (docs/04 §12)."""
    ids = set(budgeted) | {cid for cid, amount in actual.items() if amount != ZERO}
    lines = [
        BudgetLine(cid, names.get(cid, "—"), budgeted.get(cid, ZERO), actual.get(cid, ZERO))
        for cid in ids
    ]
    return sorted(lines, key=lambda line: (-line.budgeted, line.name))


@dataclass(frozen=True, slots=True)
class CollectionRow:
    """Bir dönemin aidat tahsilatı: o dönem kesilen borç ve ona yapılan mahsuplar."""

    year: int
    month: int
    charged: Decimal
    collected: Decimal

    @property
    def outstanding(self) -> Decimal:
        return self.charged - self.collected

    @property
    def rate(self) -> Decimal:
        return percent(self.collected, self.charged)
