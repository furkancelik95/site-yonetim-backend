"""İletişim, acil durum, anket ve şeffaflık (Yazılım Dokümanı ILT-01..06, RAP-02; servis
istekleri 71–74; PM kararları K31, K32, K8).

- Duyuru: zamanlama (`scheduled_for`, `channels`, iptal), zorunlu okuma onayı (`requires_ack`,
  `ack_hours`, `ack_deadline`, SMS yedeği izi) ve teslim satırında `acked_at`.
- Anket (K31): `polls.is_anonymous`, `one_per_unit`, `block_ids`; çoklu soru (`poll_questions`)
  ve soru tipleri. Eski tek soruluk anketler tek "tek seçim" sorusuna çevrilir; `poll_votes`
  satırları `poll_ballots` + `poll_answers`'a taşınır (kimlik korunur), sonra tablo kalkar.
- Yeni kiracı tabloları (RLS): şablon sürümleri (değişmez), acil durum + kapsam bölümleri,
  acil durum saklama ayarı, acil bilgi kartı, şeffaflık raporu, anket soruları/pusula/yanıt/
  katılımcı.
- Onay mekanizmasına yeni işlem tipi: "Şeffaflık raporu" (`transparency_report`). Kısıt listesi
  main'deki son göçten (0027) alındı.

Revision ID: 0036
Revises: 0027
Create Date: 2026-10-27 11:09:33.847747
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from site_yonetim.db.rls import disable_tenant_rls, enable_tenant_rls

TABLES = (
    "emergencies",
    "emergency_info_cards",
    "emergency_settings",
    "notification_template_versions",
    "transparency_reports",
    "poll_participants",
    "poll_questions",
    "emergency_units",
    "poll_ballots",
    "poll_answers",
)
IMMUTABLE = ("notification_template_versions",)

# Onay işlem tipleri: main'deki son göç (0027, P03a) listesi + "transparency_report".
_ACTIONS_0027 = (
    "charge_rule", "charge_run", "charge_run_reversal", "refund", "debt_write_off", "expense",
    "emergency_expense", "purchase_request", "purchase_order", "quote_exception", "reservation",
    "renovation_permit", "payment_plan", "paid_service_tariff", "role_change",
    "finance_mode_change", "registration", "stock_count_difference", "approval_rule_change",
    "leave_request",  # göç 0029 (insan kaynakları)
    "ownership_transfer",  # göç 0026 (yapı, kişiler)
    "period_unlock", "cash_difference", "ad_hoc_charge", "finance_parameter",
    "demand_letter",  # göç 0027 (finans P03a)
)  # fmt: skip
_ACTIONS = (*_ACTIONS_0027, "transparency_report")


def _action_check(actions: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{a}'" for a in actions)
    for table in ("approval_rules", "approval_requests"):
        op.drop_constraint(op.f(f"ck_{table}_action_type"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_action_type"), table, f"action_type IN ({allowed})"
        )


# Veri taşıma kiracı tablolarında: göç rolü de RLS'e tabi (FORCE) → işlem boyunca tüm siteler.
_ALL_SITES = "SELECT set_config('app.all_sites', 'on', true)"

revision: str = "0036"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emergencies",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("block_ids", sa.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("started_by", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("purge_on", sa.Date(), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
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
            "kind IN ('earthquake', 'fire', 'flood', 'gas', 'other')",
            name=op.f("ck_emergencies_kind"),
        ),
        sa.CheckConstraint("scope IN ('all', 'blocks')", name=op.f("ck_emergencies_scope")),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_emergencies_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_emergencies")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_emergencies_site_id_id")),
    )
    op.create_index(op.f("ix_emergencies_site_id"), "emergencies", ["site_id"], unique=False)
    op.create_index(
        "uq_emergencies_one_active",
        "emergencies",
        ["site_id"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "emergency_info_cards",
        sa.Column("muster_points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("phones", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("instructions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("block_notes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_emergency_info_cards_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_emergency_info_cards")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_emergency_info_cards_site_id_id")),
        sa.UniqueConstraint("site_id", name=op.f("uq_emergency_info_cards_site_id")),
    )
    op.create_index(
        op.f("ix_emergency_info_cards_site_id"), "emergency_info_cards", ["site_id"], unique=False
    )
    op.create_table(
        "emergency_settings",
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("auto_purge", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
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
            "retention_days BETWEEN 1 AND 365", name=op.f("ck_emergency_settings_retention")
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_emergency_settings_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_emergency_settings")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_emergency_settings_site_id_id")),
        sa.UniqueConstraint("site_id", name=op.f("uq_emergency_settings_site_id")),
    )
    op.create_index(
        op.f("ix_emergency_settings_site_id"), "emergency_settings", ["site_id"], unique=False
    )
    op.create_table(
        "notification_template_versions",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("variants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("customized", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name=op.f("ck_notification_template_versions_version")),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_notification_template_versions_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_template_versions")),
        sa.UniqueConstraint(
            "site_id",
            "code",
            "version",
            name=op.f("uq_notification_template_versions_site_id_code_version"),
        ),
        sa.UniqueConstraint(
            "site_id", "id", name=op.f("uq_notification_template_versions_site_id_id")
        ),
    )
    op.create_index(
        op.f("ix_notification_template_versions_site_id"),
        "notification_template_versions",
        ["site_id"],
        unique=False,
    )
    op.create_table(
        "transparency_reports",
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("prepared_by", sa.Text(), nullable=False),
        sa.Column("approval_request_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
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
            "status IN ('draft', 'pending', 'published', 'rejected')",
            name=op.f("ck_transparency_reports_status"),
        ),
        sa.CheckConstraint("month BETWEEN 1 AND 12", name=op.f("ck_transparency_reports_month")),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_transparency_reports_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transparency_reports")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_transparency_reports_site_id_id")),
    )
    op.create_index(
        op.f("ix_transparency_reports_site_id"), "transparency_reports", ["site_id"], unique=False
    )
    op.create_index(
        "uq_transparency_reports_period",
        "transparency_reports",
        ["site_id", "year", "month"],
        unique=True,
        postgresql_where=sa.text("status <> 'rejected'"),
    )
    op.create_table(
        "poll_participants",
        sa.Column("poll_id", sa.Uuid(), nullable=False),
        sa.Column("person_id", sa.Uuid(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["site_id", "person_id"],
            ["persons.site_id", "persons.id"],
            name=op.f("fk_poll_participants_site_id_person_id_persons"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "poll_id"],
            ["polls.site_id", "polls.id"],
            name=op.f("fk_poll_participants_site_id_poll_id_polls"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_poll_participants_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_participants")),
        sa.UniqueConstraint(
            "poll_id", "person_id", name=op.f("uq_poll_participants_poll_id_person_id")
        ),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_poll_participants_site_id_id")),
    )
    op.create_index(
        op.f("ix_poll_participants_poll_id"), "poll_participants", ["poll_id"], unique=False
    )
    op.create_index(
        op.f("ix_poll_participants_site_id"), "poll_participants", ["site_id"], unique=False
    )
    op.create_table(
        "poll_questions",
        sa.Column("poll_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
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
            "kind IN ('single', 'multiple', 'rating', 'text')", name=op.f("ck_poll_questions_kind")
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "poll_id"],
            ["polls.site_id", "polls.id"],
            name=op.f("fk_poll_questions_site_id_poll_id_polls"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_poll_questions_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_questions")),
        sa.UniqueConstraint("poll_id", "order", name=op.f("uq_poll_questions_poll_id_order")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_poll_questions_site_id_id")),
    )
    op.create_index(op.f("ix_poll_questions_poll_id"), "poll_questions", ["poll_id"], unique=False)
    op.create_index(op.f("ix_poll_questions_site_id"), "poll_questions", ["site_id"], unique=False)
    op.create_table(
        "emergency_units",
        sa.Column("emergency_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("unit_name", sa.Text(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=True),
        sa.Column("block_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("muster_point", sa.Text(), nullable=True),
        sa.Column("responded_by", sa.Text(), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
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
            "method IS NULL OR method IN ('app', 'list')", name=op.f("ck_emergency_units_method")
        ),
        sa.CheckConstraint(
            "status IN ('none', 'safe', 'muster', 'help')", name=op.f("ck_emergency_units_status")
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "emergency_id"],
            ["emergencies.site_id", "emergencies.id"],
            name=op.f("fk_emergency_units_site_id_emergency_id_emergencies"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "unit_id"],
            ["units.site_id", "units.id"],
            name=op.f("fk_emergency_units_site_id_unit_id_units"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_emergency_units_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_emergency_units")),
        sa.UniqueConstraint(
            "emergency_id", "unit_id", name=op.f("uq_emergency_units_emergency_id_unit_id")
        ),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_emergency_units_site_id_id")),
    )
    op.create_index(
        op.f("ix_emergency_units_emergency_id"), "emergency_units", ["emergency_id"], unique=False
    )
    op.create_index(
        op.f("ix_emergency_units_site_id"), "emergency_units", ["site_id"], unique=False
    )
    op.create_table(
        "poll_ballots",
        sa.Column("poll_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=True),
        sa.Column("person_id", sa.Uuid(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["site_id", "person_id"],
            ["persons.site_id", "persons.id"],
            name=op.f("fk_poll_ballots_site_id_person_id_persons"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "poll_id"],
            ["polls.site_id", "polls.id"],
            name=op.f("fk_poll_ballots_site_id_poll_id_polls"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "unit_id"],
            ["units.site_id", "units.id"],
            name=op.f("fk_poll_ballots_site_id_unit_id_units"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_poll_ballots_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_ballots")),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_poll_ballots_site_id_id")),
    )
    op.create_index(op.f("ix_poll_ballots_poll_id"), "poll_ballots", ["poll_id"], unique=False)
    op.create_index(op.f("ix_poll_ballots_site_id"), "poll_ballots", ["site_id"], unique=False)
    op.create_index(
        "uq_poll_ballots_person",
        "poll_ballots",
        ["poll_id", "person_id"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NULL AND person_id IS NOT NULL"),
    )
    op.create_index(
        "uq_poll_ballots_unit",
        "poll_ballots",
        ["poll_id", "unit_id"],
        unique=True,
        postgresql_where=sa.text("unit_id IS NOT NULL"),
    )
    op.create_table(
        "poll_answers",
        sa.Column("ballot_id", sa.Uuid(), nullable=False),
        sa.Column("poll_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("option_id", sa.Uuid(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
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
            "rating IS NULL OR rating BETWEEN 1 AND 5", name=op.f("ck_poll_answers_rating")
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "ballot_id"],
            ["poll_ballots.site_id", "poll_ballots.id"],
            name=op.f("fk_poll_answers_site_id_ballot_id_poll_ballots"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "option_id"],
            ["poll_options.site_id", "poll_options.id"],
            name=op.f("fk_poll_answers_site_id_option_id_poll_options"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "poll_id"],
            ["polls.site_id", "polls.id"],
            name=op.f("fk_poll_answers_site_id_poll_id_polls"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "question_id"],
            ["poll_questions.site_id", "poll_questions.id"],
            name=op.f("fk_poll_answers_site_id_question_id_poll_questions"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_poll_answers_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_answers")),
        sa.UniqueConstraint(
            "ballot_id",
            "question_id",
            "option_id",
            name=op.f("uq_poll_answers_ballot_id_question_id_option_id"),
        ),
        sa.UniqueConstraint("site_id", "id", name=op.f("uq_poll_answers_site_id_id")),
    )
    op.create_index(op.f("ix_poll_answers_ballot_id"), "poll_answers", ["ballot_id"], unique=False)
    op.create_index(
        "ix_poll_answers_poll_question",
        "poll_answers",
        ["site_id", "poll_id", "question_id"],
        unique=False,
    )
    op.create_index(op.f("ix_poll_answers_site_id"), "poll_answers", ["site_id"], unique=False)
    op.execute(_ALL_SITES)
    op.add_column(
        "announcement_deliveries", sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "announcements",
        sa.Column("channels", sa.ARRAY(sa.Text()), server_default="{in_app}", nullable=False),
    )
    op.alter_column("announcements", "channels", server_default=None)
    op.add_column(
        "announcements", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "announcements",
        sa.Column("requested_publish_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "announcements", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "announcements",
        sa.Column("requires_ack", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("announcements", "requires_ack", server_default=None)
    op.add_column("announcements", sa.Column("ack_hours", sa.Integer(), nullable=True))
    op.add_column(
        "announcements", sa.Column("ack_deadline", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "announcements",
        sa.Column("sms_fallback", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.alter_column("announcements", "sms_fallback", server_default=None)
    op.add_column(
        "announcements", sa.Column("sms_fallback_ran_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("announcements", sa.Column("sms_fallback_count", sa.Integer(), nullable=True))
    op.add_column("announcements", sa.Column("sms_fallback_sent", sa.Integer(), nullable=True))
    op.create_index(
        "ix_announcements_site_scheduled",
        "announcements",
        ["site_id", "scheduled_for"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL AND cancelled_at IS NULL"),
    )
    op.create_check_constraint(
        op.f("ck_announcements_ack_hours"),
        "announcements",
        "ack_hours IS NULL OR ack_hours BETWEEN 1 AND 720",
    )
    op.create_check_constraint(
        op.f("ck_announcements_ack_rule"),
        "announcements",
        "NOT requires_ack OR ack_hours IS NOT NULL",
    )
    # Mevcut duyuruların kanalları teslim kayıtlarından (uygulama içi her zaman var)
    op.execute(
        "UPDATE announcements a SET channels = COALESCE((SELECT array_agg(DISTINCT d.channel "
        "ORDER BY d.channel) FROM announcement_deliveries d WHERE d.announcement_id = a.id "
        "AND d.site_id = a.site_id), '{in_app}')"
    )
    op.add_column(
        "polls",
        sa.Column("is_anonymous", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "polls",
        sa.Column("one_per_unit", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("polls", sa.Column("block_ids", sa.ARRAY(sa.Uuid()), nullable=True))
    # Eski tek soruluk anket → tek "tek seçim" sorusu; seçenekler o soruya bağlanır
    op.add_column("poll_options", sa.Column("question_id", sa.Uuid(), nullable=True))
    op.execute(
        'INSERT INTO poll_questions (id, site_id, poll_id, "order", text, kind, required, '
        "created_at) SELECT gen_random_uuid(), site_id, id, 1, question, 'single', true, "
        "created_at FROM polls"
    )
    op.execute(
        "UPDATE poll_options o SET question_id = q.id FROM poll_questions q "
        "WHERE q.poll_id = o.poll_id AND q.site_id = o.site_id"
    )
    op.alter_column("poll_options", "question_id", nullable=False)
    op.drop_constraint(op.f("uq_poll_options_poll_id_order"), "poll_options", type_="unique")
    op.create_index(
        op.f("ix_poll_options_question_id"), "poll_options", ["question_id"], unique=False
    )
    op.create_unique_constraint(
        op.f("uq_poll_options_question_id_order"), "poll_options", ["question_id", "order"]
    )
    op.create_foreign_key(
        op.f("fk_poll_options_site_id_question_id_poll_questions"),
        "poll_options",
        "poll_questions",
        ["site_id", "question_id"],
        ["site_id", "id"],
    )
    # Oylar → pusula + yanıt (pusula kimliği eski oy kimliği; bölüm başına, gizli)
    op.execute(
        "INSERT INTO poll_ballots (id, site_id, poll_id, unit_id, created_at) "
        "SELECT id, site_id, poll_id, unit_id, created_at FROM poll_votes"
    )
    op.execute(
        "INSERT INTO poll_answers (id, site_id, ballot_id, poll_id, question_id, option_id, "
        "created_at) SELECT gen_random_uuid(), v.site_id, v.id, v.poll_id, o.question_id, "
        "v.option_id, v.created_at FROM poll_votes v JOIN poll_options o "
        "ON o.id = v.option_id AND o.site_id = v.site_id"
    )
    disable_tenant_rls(op, "poll_votes")
    op.drop_index(op.f("ix_poll_votes_poll_id"), table_name="poll_votes")
    op.drop_index(op.f("ix_poll_votes_site_id"), table_name="poll_votes")
    op.drop_table("poll_votes")
    for table in TABLES:
        enable_tenant_rls(op, table)
    for table in IMMUTABLE:
        op.execute(
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION forbid_history_change()"
        )
    _action_check(_ACTIONS)


def downgrade() -> None:
    op.execute(_ALL_SITES)
    _action_check(_ACTIONS_0027)
    for table in IMMUTABLE:
        op.execute(f'DROP TRIGGER IF EXISTS {table}_immutable ON "{table}"')
    for table in reversed(TABLES):
        disable_tenant_rls(op, table)
    op.create_table(
        "poll_votes",
        sa.Column("poll_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("option_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("unit_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("site_id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column("id", sa.UUID(), autoincrement=False, nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            autoincrement=False,
            nullable=False,
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "option_id"],
            ["poll_options.site_id", "poll_options.id"],
            name=op.f("fk_poll_votes_site_id_option_id_poll_options"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "poll_id"],
            ["polls.site_id", "polls.id"],
            name=op.f("fk_poll_votes_site_id_poll_id_polls"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id", "unit_id"],
            ["units.site_id", "units.id"],
            name=op.f("fk_poll_votes_site_id_unit_id_units"),
        ),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name=op.f("fk_poll_votes_site_id_sites")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_poll_votes")),
        sa.UniqueConstraint(
            "poll_id",
            "unit_id",
            name=op.f("uq_poll_votes_poll_id_unit_id"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
        sa.UniqueConstraint(
            "site_id",
            "id",
            name=op.f("uq_poll_votes_site_id_id"),
            postgresql_include=[],
            postgresql_nulls_not_distinct=False,
        ),
    )
    op.create_index(op.f("ix_poll_votes_site_id"), "poll_votes", ["site_id"], unique=False)
    op.create_index(op.f("ix_poll_votes_poll_id"), "poll_votes", ["poll_id"], unique=False)
    enable_tenant_rls(op, "poll_votes")
    # Geri dönüş tek soruluk modele: yalnız bölüm adına verilmiş, ilk sorunun seçimi taşınır.
    # Çoklu soru, puan/metin yanıtı ve kişi başına pusulalar eski modelde yoktur (kaybolur).
    op.execute(
        "INSERT INTO poll_votes (id, site_id, poll_id, option_id, unit_id, created_at) "
        "SELECT DISTINCT ON (b.id) b.id, b.site_id, b.poll_id, a.option_id, b.unit_id, "
        "b.created_at FROM poll_ballots b JOIN poll_answers a ON a.ballot_id = b.id "
        "AND a.site_id = b.site_id JOIN poll_questions q ON q.id = a.question_id "
        'AND q.site_id = a.site_id WHERE b.unit_id IS NOT NULL AND q."order" = 1 '
        "AND a.option_id IS NOT NULL ORDER BY b.id, a.option_id"
    )
    op.drop_column("polls", "block_ids")
    op.drop_column("polls", "one_per_unit")
    op.drop_column("polls", "is_anonymous")
    op.drop_constraint(
        op.f("fk_poll_options_site_id_question_id_poll_questions"),
        "poll_options",
        type_="foreignkey",
    )
    op.drop_constraint(op.f("uq_poll_options_question_id_order"), "poll_options", type_="unique")
    op.drop_index(op.f("ix_poll_options_question_id"), table_name="poll_options")
    op.execute(
        "DELETE FROM poll_answers WHERE option_id IN (SELECT o.id FROM poll_options o "
        'JOIN poll_questions q ON q.id = o.question_id WHERE q."order" > 1)'
    )
    op.execute(
        "DELETE FROM poll_options o USING poll_questions q "
        'WHERE q.id = o.question_id AND q."order" > 1'
    )
    op.create_unique_constraint(
        op.f("uq_poll_options_poll_id_order"), "poll_options", ["poll_id", "order"]
    )
    op.drop_column("poll_options", "question_id")
    op.drop_constraint(op.f("ck_announcements_ack_rule"), "announcements", type_="check")
    op.drop_constraint(op.f("ck_announcements_ack_hours"), "announcements", type_="check")
    op.drop_index(
        "ix_announcements_site_scheduled",
        table_name="announcements",
        postgresql_where=sa.text("published_at IS NULL AND cancelled_at IS NULL"),
    )
    op.drop_column("announcements", "sms_fallback_sent")
    op.drop_column("announcements", "sms_fallback_count")
    op.drop_column("announcements", "sms_fallback_ran_at")
    op.drop_column("announcements", "sms_fallback")
    op.drop_column("announcements", "ack_deadline")
    op.drop_column("announcements", "ack_hours")
    op.drop_column("announcements", "requires_ack")
    op.drop_column("announcements", "cancelled_at")
    op.drop_column("announcements", "requested_publish_at")
    op.drop_column("announcements", "scheduled_for")
    op.drop_column("announcements", "channels")
    op.drop_column("announcement_deliveries", "acked_at")
    op.drop_index(op.f("ix_poll_answers_site_id"), table_name="poll_answers")
    op.drop_index("ix_poll_answers_poll_question", table_name="poll_answers")
    op.drop_index(op.f("ix_poll_answers_ballot_id"), table_name="poll_answers")
    op.drop_table("poll_answers")
    op.drop_index(
        "uq_poll_ballots_unit",
        table_name="poll_ballots",
        postgresql_where=sa.text("unit_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_poll_ballots_person",
        table_name="poll_ballots",
        postgresql_where=sa.text("unit_id IS NULL AND person_id IS NOT NULL"),
    )
    op.drop_index(op.f("ix_poll_ballots_site_id"), table_name="poll_ballots")
    op.drop_index(op.f("ix_poll_ballots_poll_id"), table_name="poll_ballots")
    op.drop_table("poll_ballots")
    op.drop_index(op.f("ix_emergency_units_site_id"), table_name="emergency_units")
    op.drop_index(op.f("ix_emergency_units_emergency_id"), table_name="emergency_units")
    op.drop_table("emergency_units")
    op.drop_index(op.f("ix_poll_questions_site_id"), table_name="poll_questions")
    op.drop_index(op.f("ix_poll_questions_poll_id"), table_name="poll_questions")
    op.drop_table("poll_questions")
    op.drop_index(op.f("ix_poll_participants_site_id"), table_name="poll_participants")
    op.drop_index(op.f("ix_poll_participants_poll_id"), table_name="poll_participants")
    op.drop_table("poll_participants")
    op.drop_index(
        "uq_transparency_reports_period",
        table_name="transparency_reports",
        postgresql_where=sa.text("status <> 'rejected'"),
    )
    op.drop_index(op.f("ix_transparency_reports_site_id"), table_name="transparency_reports")
    op.drop_table("transparency_reports")
    op.drop_index(
        op.f("ix_notification_template_versions_site_id"),
        table_name="notification_template_versions",
    )
    op.drop_table("notification_template_versions")
    op.drop_index(op.f("ix_emergency_settings_site_id"), table_name="emergency_settings")
    op.drop_table("emergency_settings")
    op.drop_index(op.f("ix_emergency_info_cards_site_id"), table_name="emergency_info_cards")
    op.drop_table("emergency_info_cards")
    op.drop_index(
        "uq_emergencies_one_active",
        table_name="emergencies",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_index(op.f("ix_emergencies_site_id"), table_name="emergencies")
    op.drop_table("emergencies")
