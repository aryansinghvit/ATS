from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
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
from sqlalchemy.dialects.postgresql import ENUM, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApplicationStatus(enum.StrEnum):
    PENDING_EXTRACTION = "pending_extraction"
    APPLIED = "applied"
    SCREENING_INVITED = "screening_invited"
    SCREENING_COMPLETED = "screening_completed"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_COMPLETED = "interview_completed"
    OFFERED = "offered"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


application_status_enum = ENUM(
    ApplicationStatus,
    name="application_status",
    create_type=False,
    values_callable=lambda members: [member.value for member in members],
)


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_tenant_candidate_email"),
        UniqueConstraint("tenant_id", "id", name="uq_candidates_tenant_id_id"),
        Index("idx_candidates_tenant_email", "tenant_id", "email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    github_url: Mapped[str | None] = mapped_column(Text)
    portfolio_url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(150))
    parsed_profile_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    original_resume_s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    resume_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    resume_mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resume_file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)


class JobApplication(Base):
    __tablename__ = "job_applications"
    __table_args__ = (
        # Composite keys guarantee the job, candidate and stage belong to the same tenant.
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
            name="fk_job_applications_tenant_job",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_job_applications_tenant_candidate",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "current_stage_id"],
            ["pipeline_stages.tenant_id", "pipeline_stages.id"],
            name="fk_job_applications_tenant_stage",
        ),
        UniqueConstraint("job_id", "candidate_id", name="uq_job_candidate_app"),
        Index("idx_applications_job_stage", "job_id", "current_stage_id"),
        Index(
            "idx_applications_job_score",
            "job_id",
            text("deterministic_match_score DESC NULLS LAST"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    current_stage_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[ApplicationStatus] = mapped_column(
        application_status_enum, nullable=False, server_default=text("'pending_extraction'")
    )
    deterministic_match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    match_breakdown_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    extractor_tracking_id: Mapped[str | None] = mapped_column(String(100))
    # The resume submitted with *this* application (what the M3 extractor will parse).
    resume_s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Set when extraction could not complete (dispatch rejected or M3 reported a failure).
    extraction_error: Mapped[str | None] = mapped_column(Text)
    # The candidate profile M3 parsed from *this* application's resume (raw §6.3 envelope).
    extracted_profile_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    # What the candidate verified in the confirmation step (always kept per application).
    confirmed_profile_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Generated offer letter (PDF in object storage) and the terms it was generated from.
    offer_letter_s3_key: Mapped[str | None] = mapped_column(Text)
    offer_details_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
    # Optimistic-concurrency token for Kanban moves; bumped on every stage change.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    stage_entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __mapper_args__ = {"eager_defaults": True}


class CandidateEmbedding(Base):
    """Semantic talent-pool vector (project.md Module 5): one 384-d bge-small vector per candidate.

    ``search_tsv`` is a generated full-text column used alongside the HNSW cosine index for
    hybrid (keyword + semantic) ranking.
    """

    __tablename__ = "candidate_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_candidate_embeddings_tenant_candidate",
        ),
        UniqueConstraint("candidate_id", name="uq_candidate_embedding"),
        Index(
            "idx_candidate_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("idx_candidate_embeddings_tsv", "search_tsv", postgresql_using="gin"),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content_chunk: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    search_tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content_chunk)", persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
