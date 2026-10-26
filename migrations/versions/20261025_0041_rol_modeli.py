"""Rol ve yetki modeli (Yazılım Dokümanı PLT-09, PLT-02, ONY-03; PM kararları P18, K20):
siteye özel roller ve şablon düzeyleri (`site_roles`), rol atamaları — kişi × rol × kapsam ×
(başlangıç–bitiş) (`site_role_assignments`). Üyelikteki tekil `role` sütunu atamaya taşınır.

Veri: her üyeliğin rolü tesis kapsamlı, süresiz bir atamaya dönüşür (atanma zamanı = üyelik
zamanı). Downgrade, bitmemiş atamalardan en üst düzeydeki şablon rolünü üyeliğe geri yazar;
özel rol ataması taşınamaz (kaynak şablonu yazılır).

Revision ID: 0041
Revises: 0029
Create Date: 2026-10-26 10:00:00.000000

Kiracı tablosu ekliyorsan: enable_tenant_rls(op, "tablo") — docs/02-mimari.md §3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from site_yonetim.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0041"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("site_roles", "site_role_assignments")

# Şablon adı → anahtar ve varsayılan düzey (domain/access.py TEMPLATE_KEYS, DEFAULT_LEVELS)
_TEMPLATES = (
    ("Yönetici", "manager", 1),
    ("Yönetim Kurulu Üyesi", "board", 2),
    ("Denetçi", "auditor", 3),
    ("Muhasebe", "accounting", 3),
    ("İK Sorumlusu", "hr", 3),
    ("Güvenlik", "security", 4),
    ("Teknik Personel", "technical", 4),
    ("Görevli", "attendant", 4),
    ("Sakin", "resident", 9),
)
# 0003'teki üyelik rol kısıtı (downgrade bunu geri kurar)
_ROLE_CHECK_0003 = (
    "role IN ('Yönetici', 'Yönetim Kurulu Üyesi', 'Denetçi', 'Muhasebe', 'Güvenlik', "
    "'Teknik Personel', 'Sakin')"
)


def _case(column: str, source: int, target: int) -> str:
    whens = " ".join(f"WHEN '{t[source]}' THEN '{t[target]}'" for t in _TEMPLATES)
    return f"CASE {column} {whens} END"


def upgrade() -> None:
    op.create_table(
        "site_roles",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_key", sa.Text(), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("permissions", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_name", sa.Text(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "is_system OR permissions IS NOT NULL", name=op.f("ck_site_roles_permissions")
        ),
        sa.CheckConstraint("level BETWEEN 1 AND 9", name=op.f("ck_site_roles_level")),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_site_roles_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_roles")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_site_roles_site_id_id")),
        sa.UniqueConstraint("site_id", "key", name=op.f("uq_site_roles_site_id_key")),
        sa.UniqueConstraint("site_id", "name", name=op.f("uq_site_roles_site_id_name")),
    )
    op.create_index(op.f("ix_site_roles_site_id"), "site_roles", ["site_id"], unique=False)
    op.create_table(
        "site_role_assignments",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_key", sa.Text(), nullable=False),
        sa.Column("scope_kind", sa.Text(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=True),
        sa.Column("gate_id", sa.Uuid(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_by_name", sa.Text(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_by_name", sa.Text(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at >= starts_at",
            name=op.f("ck_site_role_assignments_period"),
        ),
        sa.CheckConstraint(
            "(scope_kind = 'site' AND block_id IS NULL AND gate_id IS NULL)"
            " OR (scope_kind = 'block' AND block_id IS NOT NULL AND gate_id IS NULL)"
            " OR (scope_kind = 'gate' AND gate_id IS NOT NULL AND block_id IS NULL)",
            name=op.f("ck_site_role_assignments_scope"),
        ),
        sa.CheckConstraint(
            "scope_kind IN ('site', 'block', 'gate')",
            name=op.f("ck_site_role_assignments_scope_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "block_id"],
            ["blocks.site_id", "blocks.id"],
            name=op.f("fk_site_role_assignments_site_id_block_id_blocks"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_site_role_assignments_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_site_role_assignments_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_site_role_assignments")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_site_role_assignments_site_id_id")),
    )
    op.create_index(
        "ix_site_role_assignments_site_id_user_id",
        "site_role_assignments",
        ["site_id", "user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_site_role_assignments_site_id"),
        "site_role_assignments",
        ["site_id"],
        unique=False,
    )
    for table in TABLES:
        enable_tenant_rls(op, table)

    # Göç sahibi de RLS'e tabi: bilinçli tüm siteler kapsamı (yalnız bu transaction).
    op.execute("SELECT set_config('app.all_sites', 'on', true)")
    op.execute(
        "INSERT INTO site_role_assignments "  # noqa: S608
        "(id, site_id, user_id, role_key, scope_kind, assigned_by_name, created_at) "
        f"SELECT gen_random_uuid(), m.site_id, m.user_id, {_case('m.role', 0, 1)}, 'site', "
        "'Tekil rolden taşındı', m.created_at FROM site_memberships m"
    )
    op.drop_constraint(op.f("ck_site_memberships_role"), "site_memberships", type_="check")
    op.drop_column("site_memberships", "role")


def downgrade() -> None:
    op.add_column("site_memberships", sa.Column("role", sa.Text(), nullable=True))
    op.execute("SELECT set_config('app.all_sites', 'on', true)")
    # Bitmemiş atamalardan en üst düzeydeki şablon; özel rolde kopyalandığı şablon.
    levels = " ".join(f"WHEN '{t[1]}' THEN {t[2]}" for t in _TEMPLATES)
    names = " ".join(f"WHEN '{t[1]}' THEN '{t[0]}'" for t in _TEMPLATES if t[1] in _OLD_KEYS)
    op.execute(
        "WITH ranked AS ("  # noqa: S608
        " SELECT a.site_id, a.user_id,"
        "  COALESCE(r.base_key, a.role_key) AS template_key,"
        f"  CASE COALESCE(r.base_key, a.role_key) {levels} ELSE 9 END AS level,"
        "  (a.ends_at IS NULL OR a.ends_at > now()) AS open"
        " FROM site_role_assignments a"
        " LEFT JOIN site_roles r ON r.site_id = a.site_id AND r.key = a.role_key"
        "  AND NOT r.is_system"
        "), best AS ("
        " SELECT DISTINCT ON (site_id, user_id) site_id, user_id, template_key"
        " FROM ranked WHERE template_key IN ("
        + ", ".join(f"'{k}'" for k in _OLD_KEYS)
        + ") ORDER BY site_id, user_id, open DESC, level"  # noqa: S608
        ") UPDATE site_memberships m"
        f" SET role = CASE b.template_key {names} END"
        " FROM best b WHERE b.site_id = m.site_id AND b.user_id = m.user_id"
    )
    # Eski modelde karşılığı olmayan (İK sorumlusu, görevli) ya da rolsüz üyelik: en dar rol.
    op.execute(
        "UPDATE site_memberships SET role = 'Sakin' WHERE role IS NULL AND person_id IS NOT NULL"
    )
    op.execute(
        "UPDATE site_memberships SET role = 'Teknik Personel', is_active = false WHERE role IS NULL"
    )
    op.alter_column("site_memberships", "role", nullable=False)
    op.create_check_constraint(
        op.f("ck_site_memberships_role"), "site_memberships", _ROLE_CHECK_0003
    )
    for table in TABLES:
        disable_tenant_rls(op, table)
    op.drop_index(op.f("ix_site_role_assignments_site_id"), table_name="site_role_assignments")
    op.drop_index("ix_site_role_assignments_site_id_user_id", table_name="site_role_assignments")
    op.drop_table("site_role_assignments")
    op.drop_index(op.f("ix_site_roles_site_id"), table_name="site_roles")
    op.drop_table("site_roles")


# Eski (0003) kısıtındaki şablonlar
_OLD_KEYS = ("manager", "board", "auditor", "accounting", "security", "technical", "resident")
