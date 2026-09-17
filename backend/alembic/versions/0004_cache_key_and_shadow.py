"""Phase 4: analysis cache keyed by model+mode, and shadow analyses

Two changes.

**1. The analysis cache was keyed on content hash alone.** `claude_analyses` was
UNIQUE(content_hash) and `cached_analysis()` looked up by content hash only, so
the first COMPLETE row for a piece of text was reused forever. That is correct
for "the same text analysed twice by the same model", which is what the cache
was for -- but it also meant a canned `LLM_FAKE_MODE` result permanently
shadowed the real one. Turn the key on and every event analysed during the
offline week keeps its placeholder analysis for good, silently, with no error
and no cost to notice. The key becomes (content_hash, model, mode).

Existing rows are backfilled with mode='fake' where the model is the canned
scorer and 'live' otherwise, which is exactly the classification the new code
would have given them.

**2. shadow_analyses.** A separate table, not a flag on `claude_analyses`, so
that the guarantee "a shadow result can never become the analysis a signal is
built from" is enforced by there being no foreign key from anything that reads
analyses -- rather than by remembering to filter on a column.

Revision ID: 0004_cache_key_and_shadow
Revises: 0003_digest_quiet_hours
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_cache_key_and_shadow"
down_revision = "0003_digest_quiet_hours"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 1. cache key ----------------------------------------------------
    op.add_column(
        "claude_analyses",
        sa.Column("mode", sa.String(8), nullable=False, server_default="live"),
    )
    # Backfill: anything produced by the canned scorer is a fake-mode result.
    op.execute("UPDATE claude_analyses SET mode = 'fake' WHERE model = 'canned-mock'")

    op.drop_constraint("uq_claude_analyses_content_hash", "claude_analyses", type_="unique")
    op.create_unique_constraint(
        "uq_claude_analyses_content_model_mode",
        "claude_analyses",
        ["content_hash", "model", "mode"],
    )

    # --- 2. shadow analyses ----------------------------------------------
    op.create_table(
        "shadow_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(36),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="COMPLETE"),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("parsed", JSONB, nullable=True),
        sa.Column("validation_error", sa.Text(), nullable=True),
        sa.Column("event_type", sa.String(48), nullable=True),
        sa.Column("sentiment", sa.Float(), nullable=True),
        sa.Column("market_impact", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("time_horizon", sa.String(16), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("content_hash", "model", name="uq_shadow_content_model"),
    )


def downgrade() -> None:
    op.drop_table("shadow_analyses")
    op.drop_constraint(
        "uq_claude_analyses_content_model_mode", "claude_analyses", type_="unique"
    )
    # Collapsing back to one row per content hash: keep the newest.
    op.execute(
        """
        DELETE FROM claude_analyses a
        USING claude_analyses b
        WHERE a.content_hash = b.content_hash AND a.created_at < b.created_at
        """
    )
    op.create_unique_constraint(
        "uq_claude_analyses_content_hash", "claude_analyses", ["content_hash"]
    )
    op.drop_column("claude_analyses", "mode")
