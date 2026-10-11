"""Phase 7: candidate screening portal state, interviews, offer letters.

Revision ID: 0006_portal_interviews
Revises: 0005_collaboration
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0006_portal_interviews"
down_revision: str | None = "0005_collaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("screenings", sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("screenings", sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("job_applications", sa.Column("offer_letter_s3_key", sa.Text(), nullable=True))
    op.add_column(
        "job_applications",
        sa.Column(
            "offer_details_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.create_table(
        "interviews",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_interviews_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("application_id", UUID, nullable=False),
        sa.Column(
            "interviewer_id",
            UUID,
            sa.ForeignKey(
                "users.id", ondelete="SET NULL", name="fk_interviews_interviewer_id_users"
            ),
            nullable=True,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("meeting_url", sa.Text(), nullable=True),
        sa.Column("calendar_event_id", sa.String(255), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'scheduled'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["job_applications.tenant_id", "job_applications.id"],
            ondelete="CASCADE",
            name="fk_interviews_tenant_application",
        ),
        sa.UniqueConstraint("tenant_id", "calendar_event_id", name="uq_interviews_calendar_event"),
        sa.CheckConstraint("scheduled_end > scheduled_start", name="time_order"),
        sa.CheckConstraint("status IN ('scheduled', 'cancelled', 'completed')", name="status"),
    )
    op.create_index(
        "idx_interviews_application", "interviews", ["application_id", "scheduled_start"]
    )
    for statement in enable_tenant_rls_sql("interviews"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_tenant_rls_sql("interviews"):
        op.execute(statement)
    op.drop_table("interviews")
    op.drop_column("job_applications", "offer_details_json")
    op.drop_column("job_applications", "offer_letter_s3_key")
    op.drop_column("screenings", "opened_at")
    op.drop_column("screenings", "invited_at")
