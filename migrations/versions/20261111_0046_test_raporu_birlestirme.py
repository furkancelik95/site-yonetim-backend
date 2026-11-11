"""Birleştirme: test raporu dalları (0040_sec güvenlik/talep + 0045_fin finans/platform).

İki dal da 0039'dan çıkıyor ve farklı tablolara dokunuyor; şema değişikliği yok.

Revision ID: 0046
Revises: 0040_sec, 0045_fin
Create Date: 2026-11-11 23:30:00.000000
"""

from collections.abc import Sequence

revision: str = "0046"
down_revision: str | tuple[str, ...] | None = ("0040_sec", "0045_fin")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
