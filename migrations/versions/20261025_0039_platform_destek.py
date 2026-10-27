"""Platform destek (Yazılım Dokümanı PYN-04, PYN-06, PLT-12; servis istekleri 82–84; PM kararı
K12): destek biletleri ve yazışması, destek oturumu (müşteri onaylı, süreli, salt okunur), sakin
olarak görüntüleme, bilgi bankası. Plana destek seviyesi, yapı ayarına "sakine bildir".

Denetim: aktör tipi "support_session" ve eylem "view" (açılan ekran) eklendi.

Revision ID: 0039
Revises: 0043
Create Date: 2026-10-27 10:00:00.000000

Kiracı tablosu ekliyorsan: enable_tenant_rls(op, "tablo") — docs/02-mimari.md §3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from site_yonetim.db.rls import disable_tenant_rls, enable_tenant_rls

revision: str = "0039"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "support_tickets",
    "support_ticket_messages",
    "support_sessions",
    "view_as_sessions",
)
IMMUTABLE = ("support_ticket_messages",)

# Liste main'deki en son göçten (0026) alındı; birleştirmede başka paketlerin değerleriyle
# birleştirilmeli.
AUDIT_ACTIONS_OLD = (
    "action IN ('create', 'update', 'delete', 'import', 'download', 'export', 'contact_view')"
)
AUDIT_ACTIONS_NEW = (
    "action IN ('create', 'update', 'delete', 'import', 'download', 'export', 'contact_view',"
    " 'view')"
)
ACTOR_TYPES_OLD = "actor_type IN ('user', 'system', 'delegate')"
ACTOR_TYPES_NEW = "actor_type IN ('user', 'system', 'delegate', 'support_session')"


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.execute("CREATE SEQUENCE support_ticket_number_seq START 1")
    op.add_column(
        "plans",
        sa.Column("support_tier", sa.Text(), server_default="business_hours", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_plans_support_tier"),
        "plans",
        "support_tier IN ('business_hours', 'critical_24_7', 'full_24_7')",
    )
    op.add_column(
        "structure_settings",
        sa.Column("view_as_notify_resident", sa.Boolean(), server_default="true", nullable=False),
    )

    op.create_table(
        "support_tickets",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("context_path", sa.Text(), nullable=False),
        sa.Column("app_version", sa.Text(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_name", sa.Text(), nullable=False),
        sa.Column("support_tier", sa.Text(), nullable=False),
        sa.Column("first_due", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolve_due", sa.DateTime(timezone=True), nullable=False),
        sa.Column("on_call", sa.Boolean(), nullable=False),
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolve_breached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "category IN ('login', 'payment', 'finance', 'import', 'security', 'other')",
            name=op.f("ck_support_tickets_category"),
        ),
        sa.CheckConstraint(
            "priority IN ('critical', 'high', 'normal')", name=op.f("ck_support_tickets_priority")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'waiting_customer', 'resolved', 'closed')",
            name=op.f("ck_support_tickets_status"),
        ),
        sa.CheckConstraint(
            "support_tier IN ('business_hours', 'critical_24_7', 'full_24_7')",
            name=op.f("ck_support_tickets_support_tier"),
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 5 AND 120", name=op.f("ck_support_tickets_title_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_support_tickets_created_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_support_tickets_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_tickets")),
        sa.UniqueConstraint("number", name=op.f("uq_support_tickets_number")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_support_tickets_site_id_id")),
    )
    op.create_index(
        op.f("ix_support_tickets_site_id"), "support_tickets", ["site_id"], unique=False
    )
    op.create_index(
        "ix_support_tickets_site_created",
        "support_tickets",
        ["site_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_support_tickets_status", "support_tickets", ["status"], unique=False)

    op.create_table(
        "support_ticket_messages",
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "side IN ('customer', 'support')", name=op.f("ck_support_ticket_messages_side")
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_support_ticket_messages_author_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "ticket_id"],
            ["support_tickets.site_id", "support_tickets.id"],
            name=op.f("fk_support_ticket_messages_site_id_ticket_id_support_tickets"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_support_ticket_messages_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_ticket_messages")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_support_ticket_messages_site_id_id")),
    )
    op.create_index(
        op.f("ix_support_ticket_messages_site_id"),
        "support_ticket_messages",
        ["site_id"],
        unique=False,
    )
    op.create_index(
        "ix_support_ticket_messages_ticket",
        "support_ticket_messages",
        ["ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "support_sessions",
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("hours", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_name", sa.Text(), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_by_name", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by_name", sa.Text(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'expired')",
            name=op.f("ck_support_sessions_status"),
        ),
        sa.CheckConstraint("hours BETWEEN 1 AND 24", name=op.f("ck_support_sessions_hours")),
        sa.CheckConstraint(
            "status <> 'active' OR ends_at IS NOT NULL",
            name=op.f("ck_support_sessions_active_ends"),
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_id"], ["users.id"], name=op.f("fk_support_sessions_decided_by_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["users.id"],
            name=op.f("fk_support_sessions_requested_by_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_support_sessions_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_support_sessions")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_support_sessions_site_id_id")),
    )
    op.create_index(
        op.f("ix_support_sessions_site_id"), "support_sessions", ["site_id"], unique=False
    )
    op.create_index(
        "ix_support_sessions_status_ends", "support_sessions", ["status", "ends_at"], unique=False
    )
    op.create_index(
        "uq_support_sessions_open",
        "support_sessions",
        ["site_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'active')"),
    )

    op.create_table(
        "view_as_sessions",
        sa.Column("viewer_id", sa.Uuid(), nullable=False),
        sa.Column("viewer_name", sa.Text(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("support_session_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resident_notified", sa.Boolean(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'ended', 'expired')", name=op.f("ck_view_as_sessions_status")
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "person_id"],
            ["persons.site_id", "persons.id"],
            name=op.f("fk_view_as_sessions_site_id_person_id_persons"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "support_session_id"],
            ["support_sessions.site_id", "support_sessions.id"],
            name=op.f("fk_view_as_sessions_site_id_support_session_id_support_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_view_as_sessions_site_id_sites")
        ),
        sa.ForeignKeyConstraint(
            ["viewer_id"], ["users.id"], name=op.f("fk_view_as_sessions_viewer_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_view_as_sessions")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_view_as_sessions_site_id_id")),
    )
    op.create_index(
        op.f("ix_view_as_sessions_site_id"), "view_as_sessions", ["site_id"], unique=False
    )
    op.create_index(
        "ix_view_as_sessions_viewer", "view_as_sessions", ["viewer_id", "status"], unique=False
    )
    op.create_index(
        "ix_view_as_sessions_status_ends", "view_as_sessions", ["status", "ends_at"], unique=False
    )

    op.create_table(
        "knowledge_articles",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("audience", sa.Text(), nullable=False),
        sa.Column("screens", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_name", sa.Text(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "audience IN ('staff', 'resident')", name=op.f("ck_knowledge_articles_audience")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 5 AND 120",
            name=op.f("ck_knowledge_articles_title_length"),
        ),
        sa.CheckConstraint(
            "char_length(body) >= 30", name=op.f("ck_knowledge_articles_body_length")
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_id"],
            ["users.id"],
            name=op.f("fk_knowledge_articles_updated_by_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_articles")),
    )
    op.create_table(
        "knowledge_feedback",
        sa.Column("article_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["knowledge_articles.id"],
            name=op.f("fk_knowledge_feedback_article_id_knowledge_articles"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_knowledge_feedback_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_feedback")),
        sa.UniqueConstraint(
            "article_id", "user_id", name=op.f("uq_knowledge_feedback_article_id_user_id")
        ),
    )

    for table in TENANT_TABLES:
        enable_tenant_rls(op, table)
    for table in IMMUTABLE:  # yazışma değişmez (forbid_history_change: göç 0008)
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION forbid_history_change()"
        )

    op.drop_constraint(op.f("ck_audit_log_action"), "audit_log", type_="check")
    op.create_check_constraint(op.f("ck_audit_log_action"), "audit_log", AUDIT_ACTIONS_NEW)
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.create_check_constraint(op.f("ck_audit_log_actor_type"), "audit_log", ACTOR_TYPES_NEW)


def downgrade() -> None:
    # Yeni değerleri taşıyan denetim satırları varsa eski kısıt kurulamaz: değişmez kayıt
    # silinmez — downgrade bu durumda bilinçli olarak durur.
    op.drop_constraint(op.f("ck_audit_log_actor_type"), "audit_log", type_="check")
    op.create_check_constraint(op.f("ck_audit_log_actor_type"), "audit_log", ACTOR_TYPES_OLD)
    op.drop_constraint(op.f("ck_audit_log_action"), "audit_log", type_="check")
    op.create_check_constraint(op.f("ck_audit_log_action"), "audit_log", AUDIT_ACTIONS_OLD)

    for table in IMMUTABLE:
        op.execute(f'DROP TRIGGER IF EXISTS {table}_immutable ON "{table}"')
    for table in reversed(TENANT_TABLES):
        disable_tenant_rls(op, table)
    op.drop_table("knowledge_feedback")
    op.drop_table("knowledge_articles")
    op.drop_index("ix_view_as_sessions_status_ends", table_name="view_as_sessions")
    op.drop_index("ix_view_as_sessions_viewer", table_name="view_as_sessions")
    op.drop_index(op.f("ix_view_as_sessions_site_id"), table_name="view_as_sessions")
    op.drop_table("view_as_sessions")
    op.drop_index("uq_support_sessions_open", table_name="support_sessions")
    op.drop_index("ix_support_sessions_status_ends", table_name="support_sessions")
    op.drop_index(op.f("ix_support_sessions_site_id"), table_name="support_sessions")
    op.drop_table("support_sessions")
    op.drop_index("ix_support_ticket_messages_ticket", table_name="support_ticket_messages")
    op.drop_index(op.f("ix_support_ticket_messages_site_id"), table_name="support_ticket_messages")
    op.drop_table("support_ticket_messages")
    op.drop_index("ix_support_tickets_status", table_name="support_tickets")
    op.drop_index("ix_support_tickets_site_created", table_name="support_tickets")
    op.drop_index(op.f("ix_support_tickets_site_id"), table_name="support_tickets")
    op.drop_table("support_tickets")
    op.drop_column("structure_settings", "view_as_notify_resident")
    op.drop_constraint(op.f("ck_plans_support_tier"), "plans", type_="check")
    op.drop_column("plans", "support_tier")
    op.execute("DROP SEQUENCE IF EXISTS support_ticket_number_seq")
