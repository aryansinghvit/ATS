from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class JobStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    PAUSED = "paused"
    CLOSED = "closed"
    ARCHIVED = "archived"


class EmploymentType(enum.StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    TEMPORARY = "temporary"
    INTERNSHIP = "internship"


class RemotePolicy(enum.StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"


job_status_enum = ENUM(
    JobStatus,
    name="job_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_tenant_job_slug"),
        # Target of composite foreign keys that pin child rows to the same tenant.
        UniqueConstraint("tenant_id", "id", name="uq_jobs_tenant_id_id"),
        Index("idx_jobs_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=False)
    location: Mapped[str] = mapped_column(String(100), nullable=False)
    remote_policy: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'remote'")
    )
    employment_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'full_time'")
    )
    seniority_level: Mapped[str | None] = mapped_column(String(50))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3), server_default=text("'USD'"))
    description_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    description_html: Mapped[str] = mapped_column(Text, nullable=False)
    # Required/preferred skills consumed by the M3 extractor's deterministic scoring.
    taxonomy_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum, nullable=False, server_default=text("'draft'")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cal.com / Calendly link injected into interview invitations (overrides the workspace link).
    scheduling_url: Mapped[str | None] = mapped_column(Text)
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class PipelineStage(Base):
    __tablename__ = "pipeline_stages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
            name="fk_pipeline_stages_tenant_job",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_pipeline_stages_tenant_id_id"),
        Index("idx_pipeline_stages_job_pos", "job_id", "position_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # NULL = tenant default
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # 'applied', 'screen', 'interview', 'offer', 'hired', 'rejected'
    stage_type: Mapped[str] = mapped_column(String(50), nullable=False)
    automated_actions_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __mapper_args__ = {"eager_defaults": True}
