"""Güvenlik sertleştirme: IP bazlı giriş hız sınırı (login_throttle), geçici parola işareti.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02 00:53:56.191568

login_throttle global tablodur (giriş site bağlamından önce) — RLS yok.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_throttle",
        sa.Column("ip", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("failures >= 0", name=op.f("ck_login_throttle_failures")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_throttle")),
        sa.UniqueConstraint("ip", name=op.f("uq_login_throttle_ip")),
    )
    op.create_index(
        op.f("ix_login_throttle_window_start"), "login_throttle", ["window_start"], unique=False
    )
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
    op.drop_index(op.f("ix_login_throttle_window_start"), table_name="login_throttle")
    op.drop_table("login_throttle")
