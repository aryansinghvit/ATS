"""Phase 6: scorecards, team notes, notifications, AI screenings, email workflows, resurfacing.

Revision ID: 0005_collaboration
Revises: 0004_talent_pool
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0005_collaboration"
down_revision: str | None = "0004_talent_pool"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB()
TABLES = (
    "scorecards",
    "candidate_notes",
    "notifications",
    "screenings",
    "email_templates",
    "email_outbox",
    "job_talent_matches",
)
RECOMMENDATIONS = ("strong_yes", "yes", "neutral", "no", "strong_no")


def _app_role() -> str:
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    return make_url(get_settings().DATABASE_URL).username or ""


def _id() -> sa.Column:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _tenant(table: str) -> sa.Column:
    return sa.Column(
        "tenant_id",
        UUID,
        sa.ForeignKey("tenants.id", ondelete="CASCADE", name=f"fk_{table}_tenant_id_tenants"),
        nullable=False,
    )


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, JSONB, nullable=False, server_default=sa.text(default))


def _application_fk(table: str, *, nullable: bool = False) -> list:
    return [
        sa.Column("application_id", UUID, nullable=nullable),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["job_applications.tenant_id", "job_applications.id"],
            ondelete="CASCADE",
            name=f"fk_{table}_tenant_application",
        ),
    ]


def upgrade() -> None:
    # Composite-FK targets so child rows can never point at another tenant's records.
    op.create_unique_constraint(
        "uq_job_applications_tenant_id_id", "job_applications", ["tenant_id", "id"]
    )
    op.add_column("jobs", sa.Column("scheduling_url", sa.Text(), nullable=True))

    op.create_table(
        "scorecards",
        _id(),
        _tenant("scorecards"),
        *_application_fk("scorecards"),
        sa.Column(
            "evaluator_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="RESTRICT", name="fk_scorecards_evaluator_id_users"),
            nullable=False,
        ),
        sa.Column("overall_recommendation", sa.String(20), nullable=True),
        _jsonb("scores_json"),
        sa.Column("notes_markdown", sa.Text(), nullable=True),
        sa.Column("is_submitted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _ts("submitted_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("application_id", "evaluator_id", name="uq_scorecard_evaluator"),
        sa.CheckConstraint(
            "overall_recommendation IS NULL OR overall_recommendation IN "
            f"({', '.join(repr(r) for r in RECOMMENDATIONS)})",
            name="recommendation",
        ),
        sa.CheckConstraint(
            "NOT is_submitted OR (overall_recommendation IS NOT NULL AND submitted_at IS NOT NULL)",
            name="submitted_complete",
        ),
    )

    op.create_table(
        "candidate_notes",
        _id(),
        _tenant("candidate_notes"),
        *_application_fk("candidate_notes"),
        sa.Column(
            "author_id",
            UUID,
            sa.ForeignKey(
                "users.id", ondelete="RESTRICT", name="fk_candidate_notes_author_id_users"
            ),
            nullable=False,
        ),
        sa.Column("content_markdown", sa.Text(), nullable=False),
        _jsonb("mentions_json", "'[]'::jsonb"),
        _ts("created_at"),
    )
    op.create_index(
        "idx_candidate_notes_application", "candidate_notes", ["application_id", "created_at"]
    )

    op.create_table(
        "notifications",
        _id(),
        _tenant("notifications"),
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_notifications_user_id_users"),
            nullable=False,
        ),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", UUID, nullable=True),
        _ts("read_at", nullable=True),
        _ts("created_at"),
    )
    op.create_index("idx_notifications_user", "notifications", ["user_id", "read_at", "created_at"])

    screening_status = postgresql.ENUM(
        "pending", "opened", "completed", "expired", name="screening_status"
    )
    screening_status.create(op.get_bind())
    op.create_table(
        "screenings",
        _id(),
        _tenant("screenings"),
        *_application_fk("screenings"),
        # SHA-256 of the candidate access token; the raw token is never stored.
        sa.Column("access_token", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="screening_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        _ts("expires_at"),
        _jsonb("questions_json", "'[]'::jsonb"),
        _jsonb("responses_json", "'[]'::jsonb"),
        _jsonb("ai_evaluation_json"),
        sa.Column(
            "generation_source", sa.String(20), nullable=False, server_default=sa.text("'ai'")
        ),
        sa.Column(
            "created_by",
            UUID,
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_screenings_created_by_users"),
            nullable=True,
        ),
        _ts("submitted_at", nullable=True),
        _ts("created_at"),
    )
    op.create_index("idx_screenings_app", "screenings", ["application_id"])

    op.create_table(
        "email_templates",
        _id(),
        _tenant("email_templates"),
        sa.Column("key", sa.String(50), nullable=False),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_by",
            UUID,
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_email_templates_updated_by_users"
            ),
            nullable=True,
        ),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_email_templates_tenant_key"),
    )

    op.create_table(
        "email_outbox",
        _id(),
        _tenant("email_outbox"),
        *_application_fk("email_outbox", nullable=True),
        sa.Column("template_key", sa.String(50), nullable=False),
        sa.Column("trigger", sa.String(50), nullable=False),
        sa.Column("to_email", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        _ts("sent_at", nullable=True),
        _ts("created_at"),
        sa.CheckConstraint("status IN ('queued', 'sent', 'cancelled', 'failed')", name="status"),
    )
    op.create_index("idx_email_outbox_due", "email_outbox", ["status", "scheduled_for"])
    op.create_index("idx_email_outbox_application", "email_outbox", ["application_id"])

    op.create_table(
        "job_talent_matches",
        _id(),
        _tenant("job_talent_matches"),
        sa.Column("job_id", UUID, nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("similarity", sa.Numeric(5, 4), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'suggested'")),
        _ts("created_at"),
        _ts("updated_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
            name="fk_job_talent_matches_tenant_job",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_job_talent_matches_tenant_candidate",
        ),
        sa.UniqueConstraint("job_id", "candidate_id", name="uq_job_talent_match"),
        sa.CheckConstraint("status IN ('suggested', 'invited', 'dismissed')", name="status"),
    )

    for table in TABLES:
        for statement in enable_tenant_rls_sql(table):
            op.execute(statement)

    # Cross-tenant outbox sweep for the communications worker (exposes ids only).
    op.execute(
        """
        CREATE FUNCTION app_due_emails(p_limit integer)
        RETURNS TABLE (tenant_id uuid, email_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
            SELECT o.tenant_id, o.id FROM public.email_outbox o
            WHERE o.status = 'queued' AND o.scheduled_for <= now()
            ORDER BY o.scheduled_for
            LIMIT p_limit
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app_due_emails(integer) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION app_due_emails(integer) TO {_app_role()}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_due_emails(integer)")
    for table in reversed(TABLES):
        for statement in disable_tenant_rls_sql(table):
            op.execute(statement)
        op.drop_table(table)
    postgresql.ENUM(name="screening_status").drop(op.get_bind())
    op.drop_column("jobs", "scheduling_url")
    op.drop_constraint("uq_job_applications_tenant_id_id", "job_applications", type_="unique")
