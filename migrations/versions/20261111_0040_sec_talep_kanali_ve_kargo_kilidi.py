"""Test raporu (10.11.2026) — talep kanalı, atanan kullanıcı, açan kullanıcı; kargo teslim kodu
kilidi.

- `requests.channel` (`resident` · `staff`): sakin onayı (K18) yalnız sakin ucundan açılan
  talepte beklenir. **Eski kayıtlar:** raporlayan kişisi dolu olan `resident` (sakin onayı
  korunur — personel bu talepleri yine doğrudan kapatamaz), boş olan `staff`. Güvenli taraf:
  yanlışlıkla sakin onayını atlatan bir geçiş yok.
- `requests.assignee_user_id`: atanan personelin kullanıcı hesabı (`requests.assigned`
  kapsamı). Eski serbest metin atamalar boş kalır.
- `requests.created_by_user_id`: talebi açan kullanıcı (teknik personel kendi açtığını görür).
  Eski kayıtlarda boş.
- `packages.code_failed_attempts`, `packages.code_locked_until`: teslim kodu 5 hatalı denemede
  15 dakika kilit (kargo başına).

Hazır rol şablonlarının izinleri koddadır (`domain/access.py`, `site_roles.is_system` satırında
izin saklanmaz); teknik personel şablonunun değişikliği (`requests.assigned`) için veri göçü
gerekmez. Teknikten kopyalanmış siteye özel roller sitenin kararıdır; değiştirilmez.

Revision ID: 0040_sec
Revises: 0039
Create Date: 2026-11-11 10:00:00.000000

Kiracı tablosu ekliyorsan: enable_tenant_rls(op, "tablo") — docs/02-mimari.md §3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_sec"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("requests", sa.Column("channel", sa.Text(), nullable=True))
    op.execute(
        "UPDATE requests SET channel = CASE WHEN reported_by_person_id IS NULL "
        "THEN 'staff' ELSE 'resident' END"
    )
    op.alter_column("requests", "channel", nullable=False)
    op.create_check_constraint(
        op.f("ck_requests_channel"), "requests", "channel IN ('resident', 'staff')"
    )
    op.add_column("requests", sa.Column("assignee_user_id", sa.Uuid(), nullable=True))
    op.add_column("requests", sa.Column("created_by_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_requests_assignee_user_id_users"),
        "requests",
        "users",
        ["assignee_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_requests_created_by_user_id_users"),
        "requests",
        "users",
        ["created_by_user_id"],
        ["id"],
    )
    op.create_index(
        "ix_requests_assignee", "requests", ["site_id", "assignee_user_id"], unique=False
    )
    op.create_index(
        "ix_requests_creator", "requests", ["site_id", "created_by_user_id"], unique=False
    )

    op.add_column(
        "packages",
        sa.Column("code_failed_attempts", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.alter_column("packages", "code_failed_attempts", server_default=None)
    op.add_column(
        "packages",
        sa.Column("code_locked_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("packages", "code_locked_until")
    op.drop_column("packages", "code_failed_attempts")
    op.drop_index("ix_requests_creator", table_name="requests")
    op.drop_index("ix_requests_assignee", table_name="requests")
    op.drop_constraint(op.f("fk_requests_created_by_user_id_users"), "requests", type_="foreignkey")
    op.drop_constraint(op.f("fk_requests_assignee_user_id_users"), "requests", type_="foreignkey")
    op.drop_column("requests", "created_by_user_id")
    op.drop_column("requests", "assignee_user_id")
    op.drop_constraint(op.f("ck_requests_channel"), "requests", type_="check")
    op.drop_column("requests", "channel")
