"""Bilgi bankası: yayından kaldırma (arşiv) — test raporu, platform maddesi.

- knowledge_articles.archived_at (boş = yayında). Arşivdeki makale yardımda ve aramada
  görünmez; kalıcı silme yok. Mevcut makaleler yayında kalır.

Downgrade: sütun düşer; arşivdeki makaleler yeniden yayında görünür (veri silinmez).

Revision ID: 0045_fin
Revises: 0039
Create Date: 2026-11-11 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_fin"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_articles",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_articles", "archived_at")
