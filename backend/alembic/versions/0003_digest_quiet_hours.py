"""Phase 3: make the digest's quiet-hours override a user setting

Until now a digest always ignored quiet hours. That is the right default -- a
summary you scheduled for 07:00 is not an interruption -- but it was hard-coded,
so someone whose quiet hours genuinely mean "nothing at all, ever" had no way to
say so. The column defaults to true, which preserves the existing behaviour for
every row that already exists.

Revision ID: 0003_digest_quiet_hours
Revises: 0002_pgvector
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_digest_quiet_hours"
down_revision = "0002_pgvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_preferences",
        sa.Column(
            "digest_ignores_quiet_hours",
            sa.Boolean(),
            nullable=False,
            # server_default so existing rows are backfilled in one statement
            # rather than left NULL for the application to reinterpret.
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("notification_preferences", "digest_ignores_quiet_hours")
