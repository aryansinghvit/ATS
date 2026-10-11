"""Phase 2: jobs, pipeline stages, candidates and job applications (all tenant-isolated by RLS).

Revision ID: 0002_jobs_candidates
Revises: 0001_foundation
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0002_jobs_candidates"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUSES = ("draft", "published", "paused", "closed", "archived")
APPLICATION_STATUSES = (
    "pending_extraction",
    "applied",
    "screening_invited",
    "screening_completed",
    "interview_scheduled",
    "interview_completed",
    "offered",
    "hired",
    "rejected",
    "withdrawn",
)
TENANT_TABLES = ("jobs", "pipeline_stages", "candidates", "job_applications")


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant_fk(table: str) -> sa.Column:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id", ondelete="CASCADE", name=f"fk_{table}_tenant_id_tenants"),
        nullable=False,
    )


def _timestamp(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb(name: str, default: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(name, postgresql.JSONB(), nullable=False, server_default=sa.text(default))


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*JOB_STATUSES, name="job_status").create(bind)
    postgresql.ENUM(*APPLICATION_STATUSES, name="application_status").create(bind)

    op.create_table(
        "jobs",
        _uuid_pk(),
        _tenant_fk("jobs"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("department", sa.String(100), nullable=False),
        sa.Column("location", sa.String(100), nullable=False),
        sa.Column(
            "remote_policy", sa.String(50), nullable=False, server_default=sa.text("'remote'")
        ),
        sa.Column(
            "employment_type", sa.String(30), nullable=False, server_default=sa.text("'full_time'")
        ),
        sa.Column("seniority_level", sa.String(50), nullable=True),
        sa.Column("salary_min", sa.Numeric(12, 2), nullable=True),
        sa.Column("salary_max", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True, server_default=sa.text("'USD'")),
        sa.Column("description_markdown", sa.Text(), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=False),
        _jsonb("taxonomy_json"),
        sa.Column(
            "status",
            postgresql.ENUM(name="job_status", create_type=False),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "creator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_jobs_creator_id_users"),
            nullable=True,
        ),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_tenant_job_slug"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_jobs_tenant_id_id"),
        sa.CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="ck_jobs_salary_range",
        ),
    )
    op.create_index("idx_jobs_tenant_status", "jobs", ["tenant_id", "status"])

    op.create_table(
        "pipeline_stages",
        _uuid_pk(),
        _tenant_fk("pipeline_stages"),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position_index", sa.Integer(), nullable=False),
        sa.Column("stage_type", sa.String(50), nullable=False),
        _jsonb("automated_actions_json"),
        _timestamp("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
            name="fk_pipeline_stages_tenant_job",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pipeline_stages_tenant_id_id"),
    )
    op.create_index("idx_pipeline_stages_job_pos", "pipeline_stages", ["job_id", "position_index"])

    op.create_table(
        "candidates",
        _uuid_pk(),
        _tenant_fk("candidates"),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("github_url", sa.Text(), nullable=True),
        sa.Column("portfolio_url", sa.Text(), nullable=True),
        sa.Column("location", sa.String(150), nullable=True),
        _jsonb("parsed_profile_json"),
        sa.Column("original_resume_s3_key", sa.Text(), nullable=False),
        sa.Column("resume_file_name", sa.String(255), nullable=False),
        sa.Column("resume_mime_type", sa.String(100), nullable=False),
        sa.Column("resume_file_size_bytes", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_candidate_email"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_candidates_tenant_id_id"),
    )
    op.create_index("idx_candidates_tenant_email", "candidates", ["tenant_id", "email"])

    op.create_table(
        "job_applications",
        _uuid_pk(),
        _tenant_fk("job_applications"),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_stage_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(name="application_status", create_type=False),
            nullable=False,
            server_default=sa.text("'pending_extraction'"),
        ),
        sa.Column("deterministic_match_score", sa.Numeric(5, 2), nullable=True),
        _jsonb("match_breakdown_json"),
        sa.Column("extractor_tracking_id", sa.String(100), nullable=True),
        sa.Column("resume_s3_key", sa.Text(), nullable=False),
        _timestamp("applied_at"),
        _timestamp("updated_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
            name="fk_job_applications_tenant_job",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_job_applications_tenant_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "current_stage_id"],
            ["pipeline_stages.tenant_id", "pipeline_stages.id"],
            name="fk_job_applications_tenant_stage",
        ),
        sa.UniqueConstraint("job_id", "candidate_id", name="uq_job_candidate_app"),
        sa.CheckConstraint(
            "deterministic_match_score IS NULL OR deterministic_match_score BETWEEN 0 AND 100",
            name="ck_job_applications_score_range",
        ),
    )
    op.create_index(
        "idx_applications_job_stage", "job_applications", ["job_id", "current_stage_id"]
    )
    op.create_index(
        "idx_applications_job_score",
        "job_applications",
        ["job_id", sa.text("deterministic_match_score DESC NULLS LAST")],
    )

    for table in TENANT_TABLES:
        for statement in enable_tenant_rls_sql(table):
            op.execute(statement)
    # Table privileges for the runtime role come from 0001's ALTER DEFAULT PRIVILEGES.


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        for statement in disable_tenant_rls_sql(table):
            op.execute(statement)
        op.drop_table(table)
    bind = op.get_bind()
    postgresql.ENUM(name="application_status").drop(bind)
    postgresql.ENUM(name="job_status").drop(bind)
