"""Phase 2: pgvector embeddings and push delivery retry state

Requires the pgvector extension. The bundled docker-compose already uses the
pgvector/pgvector image; on a hand-rolled Postgres install
`postgresql-<version>-pgvector` (Debian/Ubuntu) provides it.

Revision ID: 0002_pgvector
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.config import settings

revision = "0002_pgvector"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    dim = settings.embedding_dim

    # Phase 1 stored a plain float array. There is no meaningful conversion of
    # an arbitrary-length array into a fixed-width vector, and the rows are
    # derived data that the worker regenerates, so drop and recreate.
    op.drop_table("event_embeddings")
    op.create_table(
        "event_embeddings",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=48), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.dialects.postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    # Declare the real column type separately so the dimension comes from
    # config rather than being hard-coded into the migration's DDL.
    op.execute(f"ALTER TABLE event_embeddings ALTER COLUMN embedding TYPE vector({dim})")
    op.create_index(
        "ix_event_embeddings_content_hash", "event_embeddings", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_event_embeddings_provider", "event_embeddings", ["provider"], unique=False
    )

    # HNSW with cosine distance. Vectors are L2-normalised on write, so cosine
    # distance is 1 - similarity and the ORDER BY in similar_events() uses this.
    op.execute(
        """
        CREATE INDEX ix_event_embeddings_hnsw ON event_embeddings
        USING hnsw (embedding vector_cosine_ops)
        """
    )

    # Push delivery: when the next retry is due, so a failed send is retried
    # with backoff instead of being retried immediately or dropped.
    op.add_column(
        "notification_deliveries",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_deliveries_retry",
        "notification_deliveries",
        ["status", "next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_retry", table_name="notification_deliveries")
    op.drop_column("notification_deliveries", "next_retry_at")

    op.execute("DROP INDEX IF EXISTS ix_event_embeddings_hnsw")
    op.drop_index("ix_event_embeddings_provider", table_name="event_embeddings")
    op.drop_index("ix_event_embeddings_content_hash", table_name="event_embeddings")
    op.drop_table("event_embeddings")
    op.create_table(
        "event_embeddings",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("model", sa.String(length=96), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("embedding", sa.dialects.postgresql.ARRAY(sa.Float()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
    )
