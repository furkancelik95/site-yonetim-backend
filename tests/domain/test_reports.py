"""Rapor biçimleme — saf (docs/04 §12)."""

import uuid
from decimal import Decimal

from site_yonetim.domain.reports import (
    BudgetLine,
    CategoryAmount,
    CollectionRow,
    budget_comparison,
    category_shares,
    months,
    percent,
)

A, B, C = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()


def test_twelve_months_with_zeros() -> None:
    rows = months({1: Decimal(1000), 3: Decimal(500)}, {1: Decimal(400)})
    assert [r.month for r in rows] == list(range(1, 13))
    assert (rows[0].income, rows[0].expense, rows[0].difference) == (
        Decimal(1000),
        Decimal(400),
        Decimal(600),
    )
    assert rows[1].difference == 0
    assert rows[2].difference == 500


def test_percent() -> None:
    assert percent(Decimal(1), Decimal(3)) == Decimal("33.33")
    assert percent(Decimal(2), Decimal(3)) == Decimal("66.67")
    assert percent(Decimal(5), Decimal(0)) == Decimal("0.00")


def test_category_shares_sorted_by_amount() -> None:
    shares = category_shares(
        [
            CategoryAmount(A, "İşletme", Decimal(750), 3),
            CategoryAmount(B, "Demirbaş", Decimal(250), 1),
        ]
    )
    assert [(s.name, s.share) for s in shares] == [
        ("İşletme", Decimal("75.00")),
        ("Demirbaş", Decimal("25.00")),
    ]


def test_budget_comparison_includes_unbudgeted_spending() -> None:
    lines = budget_comparison(
        {A: Decimal(12000), B: Decimal(0)},
        {A: Decimal(13000), C: Decimal(500)},
        {A: "İşletme", B: "Demirbaş", C: "Diğer"},
    )
    assert [(line.name, line.budgeted, line.actual) for line in lines] == [
        ("İşletme", Decimal(12000), Decimal(13000)),
        ("Demirbaş", Decimal(0), Decimal(0)),
        ("Diğer", Decimal(0), Decimal(500)),
    ]
    over = lines[0]
    assert (over.difference, over.usage, over.is_over) == (Decimal(-1000), Decimal("108.33"), True)
    assert BudgetLine(A, "x", Decimal(100), Decimal(50)).is_over is False


def test_collection_rate() -> None:
    row = CollectionRow(2026, 9, Decimal(1000), Decimal(940))
    assert (row.outstanding, row.rate) == (Decimal(60), Decimal("94.00"))
    assert CollectionRow(2026, 9, Decimal(0), Decimal(0)).rate == Decimal("0.00")
