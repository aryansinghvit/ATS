"""Phase 5: semantic talent pool — candidate_embeddings with pgvector HNSW + full-text search.

Revision ID: 0004_talent_pool
Revises: 0003_extraction_pipeline
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql

revision: str = "0004_talent_pool"
down_revision: str | None = "0003_extraction_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 384  # BAAI/bge-small-en-v1.5


def upgrade() -> None:
    op.create_table(
        "candidate_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "tenants.id", ondelete="CASCADE", name="fk_candidate_embeddings_tenant_id_tenants"
            ),
            nullable=False,
        ),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_chunk", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content_chunk)", persisted=True),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
            name="fk_candidate_embeddings_tenant_candidate",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_candidate_embedding"),
    )
    # HNSW (cosine) for approximate nearest-neighbour search; GIN for keyword ranking.
    op.create_index(
        "idx_candidate_embeddings_hnsw",
        "candidate_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "idx_candidate_embeddings_tsv",
        "candidate_embeddings",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index("idx_candidate_embeddings_tenant", "candidate_embeddings", ["tenant_id"])
    for statement in enable_tenant_rls_sql("candidate_embeddings"):
        op.execute(statement)


def downgrade() -> None:
    for statement in disable_tenant_rls_sql("candidate_embeddings"):
        op.execute(statement)
    op.drop_table("candidate_embeddings")
