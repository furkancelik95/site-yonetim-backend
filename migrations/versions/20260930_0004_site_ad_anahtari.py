"""sites.name_key — Türkçe kurallarla normalize edilmiş benzersiz site adı.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 22:17:21.640302

Değer uygulamada üretilir (`models.platform.site_name_key`): PostgreSQL `lower()` Türkçe
I/İ kuralını bilmez ve sonucu sunucu yerel ayarına bağlıdır. Mevcut satırlar için aşağıdaki
SQL yaklaşık bir doldurmadır (üretimde henüz site yok).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sites", sa.Column("name_key", sa.Text(), nullable=True))
    op.execute(
        "UPDATE sites SET name_key = "
        "lower(regexp_replace(btrim(translate(name, 'Iİ', 'ıi')), '\\s+', ' ', 'g'))"
    )
    op.alter_column("sites", "name_key", nullable=False)
    op.create_unique_constraint(op.f("uq_sites_name_key"), "sites", ["name_key"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_sites_name_key"), "sites", type_="unique")
    op.drop_column("sites", "name_key")
