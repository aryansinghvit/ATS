"""Phase 3: audit log, Kanban concurrency columns, extraction state and the stalled-job sweep.

Revision ID: 0003_extraction_pipeline
Revises: 0002_jobs_candidates
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0003_extraction_pipeline"
down_revision: str | None = "0002_jobs_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _app_role() -> str:
    from sqlalchemy.engine import make_url

    from app.core.config import get_settings

    return make_url(get_settings().DATABASE_URL).username or ""


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_audit_logs_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_audit_logs_actor_id_users"),
            nullable=True,
        ),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "idx_audit_logs_tenant_entity", "audit_logs", ["tenant_id", "entity_type", "entity_id"]
    )
    for statement in enable_tenant_rls_sql("audit_logs"):
        op.execute(statement)

    op.add_column("job_applications", sa.Column("extraction_error", sa.Text(), nullable=True))
    op.add_column(
        "job_applications",
        sa.Column(
            "extracted_profile_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "job_applications",
        sa.Column(
            "confirmed_profile_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "job_applications", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job_applications",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "job_applications",
        sa.Column(
            "stage_entered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_applications_pending_extraction",
        "job_applications",
        ["applied_at"],
        postgresql_where=sa.text("status = 'pending_extraction'"),
    )

    # Cross-tenant sweep for the extraction worker: exposes only (tenant_id, application_id) of
    # applications that never reached the M3 extractor, so they can be re-queued per tenant.
    op.execute(
        """
        CREATE FUNCTION app_list_stalled_extractions(p_older_than_seconds integer, p_limit integer)
        RETURNS TABLE (tenant_id uuid, application_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $fn$
            SELECT a.tenant_id, a.id
            FROM public.job_applications a
            WHERE a.status = 'pending_extraction'
              AND a.extractor_tracking_id IS NULL
              AND a.extraction_error IS NULL
              AND a.applied_at < now() - make_interval(secs => p_older_than_seconds)
            ORDER BY a.applied_at
            LIMIT p_limit
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION app_list_stalled_extractions(integer, integer) FROM PUBLIC")
    op.execute(
        f"GRANT EXECUTE ON FUNCTION app_list_stalled_extractions(integer, integer) TO {_app_role()}"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_list_stalled_extractions(integer, integer)")
    op.drop_index("idx_applications_pending_extraction", table_name="job_applications")
    for column in (
        "stage_entered_at",
        "version",
        "confirmed_at",
        "confirmed_profile_json",
        "extracted_profile_json",
        "extraction_error",
    ):
        op.drop_column("job_applications", column)
    for statement in disable_tenant_rls_sql("audit_logs"):
        op.execute(statement)
    op.drop_index("idx_audit_logs_tenant_entity", table_name="audit_logs")
    op.drop_table("audit_logs")
