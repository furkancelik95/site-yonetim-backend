"""Devir bakiye: defter kaynak türü `opening` + hesap başına tek devir (kısmi benzersiz indeks).

Geri alma, `opening` hareketi varsa durur (finansal kayıt silinmez — kısıt eski haline dönemez).

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-03 12:44:28.930505

Kiracı tablosu ekliyorsan: enable_tenant_rls(op, "tablo") — docs/02-mimari.md §3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD = ("charge", "payment", "late_fee", "adjustment", "transfer", "advance")
_NEW = (*_OLD, "opening")


def _source_check(values: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_ledger_entries_source"), "ledger_entries", type_="check")
    allowed = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        op.f("ck_ledger_entries_source"), "ledger_entries", f"source IN ({allowed})"
    )


def upgrade() -> None:
    _source_check(_NEW)
    op.create_index(
        "uq_ledger_entries_opening",
        "ledger_entries",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("source = 'opening'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_ledger_entries_opening",
        table_name="ledger_entries",
        postgresql_where=sa.text("source = 'opening'"),
    )
    _source_check(_OLD)
