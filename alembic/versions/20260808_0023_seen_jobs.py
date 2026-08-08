"""cross-run job dedup ledger

Revision ID: 20260808_0023
Revises: 20260807_0022
Create Date: 2026-08-08 09:00:00.000000

Lightweight, FK-free table recording every normalized job identity ever
captured (C3, docs/findjobs-optimization-plan.zh-CN.md §7).  Later runs
skip jobs already seen and re-reporting them as new; rows are pruned by
TTL from the application layer (seen_jobs.prune_expired).  No foreign keys
by design so the ledger never participates in the job-discovery task
cascade and cannot be orphaned by task deletion.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260808_0023"
down_revision: Union[str, Sequence[str], None] = "20260807_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the seen_jobs dedup ledger."""
    op.create_table(
        "seen_jobs",
        sa.Column("job_id", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_seen_jobs_source_hash", "seen_jobs", ["source", "content_hash"], unique=False)
    op.create_index("ix_seen_jobs_last_seen", "seen_jobs", ["last_seen"], unique=False)


def downgrade() -> None:
    """Drop the dedup ledger."""
    op.drop_index("ix_seen_jobs_last_seen", table_name="seen_jobs")
    op.drop_index("ix_seen_jobs_source_hash", table_name="seen_jobs")
    op.drop_table("seen_jobs")
