"""Tahakkuk taslağının anlık görüntüsü — saf (FIN-05).

Taslak hazırlanınca önizleme JSON olarak saklanır; onaylanan taslak **bu görüntüden** kesinleşir.
Onaycının gördüğü tutar deftere yazılan tutardır (arada bölüm/ilişki değişse bile). Değişiklik
gerekiyorsa taslak yenilenir ve yeniden onaya gider.
"""

import uuid
from decimal import Decimal
from typing import Any

from site_yonetim.domain.charging.engine import (
    ItemSource,
    ItemTotal,
    Preview,
    PreviewCharge,
    PreviewLine,
    PreviewWarning,
    WarningKind,
)
from site_yonetim.domain.finance import AllocationKind
from site_yonetim.domain.structure import AccountKind


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _undec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _uuid(value: str | None) -> uuid.UUID | None:
    return None if value is None else uuid.UUID(value)


def dump(preview: Preview, not_due_items: list[str]) -> dict[str, Any]:
    return {
        "charges": [
            {
                "unit_id": str(c.unit_id),
                "unit_name": c.unit_name,
                "ledger_account_id": str(c.ledger_account_id),
                "reference_code": c.reference_code,
                "person_id": str(c.person_id),
                "person_name": c.person_name,
                "account_kind": c.account_kind.value,
                "days": c.days,
                "period_days": c.period_days,
                "lines": [
                    {
                        "item_id": str(line.budget_item_id),
                        "source": line.source.value,
                        "description": line.description,
                        "amount": str(line.amount),
                        "allocation_kind": line.allocation_kind.value,
                        "weight": _dec(line.weight),
                        "weight_total": _dec(line.weight_total),
                        "source_amount": str(line.source_amount),
                        "explanation": line.explanation,
                    }
                    for line in c.lines
                ],
            }
            for c in preview.charges
        ],
        "warnings": [
            {
                "kind": w.kind.value,
                "message": w.message,
                "unit_id": None if w.unit_id is None else str(w.unit_id),
                "item_id": None if w.budget_item_id is None else str(w.budget_item_id),
            }
            for w in preview.warnings
        ],
        "item_totals": [
            {
                "item_id": str(t.budget_item_id),
                "name": t.name,
                "amount": str(t.amount),
                "distributed": str(t.distributed),
                "exempted": str(t.exempted),
            }
            for t in preview.item_totals
        ],
        "not_due_items": list(not_due_items),
    }


def load(data: dict[str, Any]) -> tuple[Preview, list[str]]:
    charges = [
        PreviewCharge(
            unit_id=uuid.UUID(c["unit_id"]),
            unit_name=c["unit_name"],
            ledger_account_id=uuid.UUID(c["ledger_account_id"]),
            reference_code=c["reference_code"],
            person_id=uuid.UUID(c["person_id"]),
            person_name=c["person_name"],
            account_kind=AccountKind(c["account_kind"]),
            days=c.get("days"),
            period_days=c.get("period_days"),
            lines=tuple(
                PreviewLine(
                    budget_item_id=uuid.UUID(line["item_id"]),
                    description=line["description"],
                    amount=Decimal(line["amount"]),
                    allocation_kind=AllocationKind(line["allocation_kind"]),
                    weight=_undec(line["weight"]),
                    weight_total=_undec(line["weight_total"]),
                    source_amount=Decimal(line["source_amount"]),
                    explanation=line["explanation"],
                    source=ItemSource(line["source"]),
                )
                for line in c["lines"]
            ),
        )
        for c in data.get("charges", [])
    ]
    warnings = [
        PreviewWarning(
            WarningKind(w["kind"]), w["message"], _uuid(w["unit_id"]), _uuid(w["item_id"])
        )
        for w in data.get("warnings", [])
    ]
    totals = [
        ItemTotal(
            uuid.UUID(t["item_id"]),
            t["name"],
            Decimal(t["amount"]),
            Decimal(t["distributed"]),
            Decimal(t.get("exempted", "0")),
        )
        for t in data.get("item_totals", [])
    ]
    return Preview(charges, warnings, totals), list(data.get("not_due_items", []))
