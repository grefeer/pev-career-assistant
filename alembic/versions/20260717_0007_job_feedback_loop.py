"""add student job feedback loop

Revision ID: 20260717_0007
Revises: 20260717_0006
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0007"
down_revision: Union[str, Sequence[str], None] = "20260717_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_feedback",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('closed','application_channel_unavailable','content_changed','incorrect_information')",
            name="job_feedback_category",
        ),
        sa.CheckConstraint(
            "status IN ('open','accepted','resolved','rejected','withdrawn')",
            name="job_feedback_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "job_id", "category", name="uq_job_feedback_user_job_category"
        ),
    )
    op.create_index("ix_job_feedback_user_id", "job_feedback", ["user_id"])
    op.create_index(
        "ix_job_feedback_job_status_updated",
        "job_feedback",
        ["job_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_job_feedback_status_updated", "job_feedback", ["status", "updated_at"]
    )
    op.create_table(
        "job_feedback_events",
        sa.Column("feedback_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=True),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("feedback_version", sa.Integer(), nullable=False),
        sa.Column("redacted_snapshot", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "action IN ('submitted','updated','withdrawn','accepted','resolved','rejected')",
            name="job_feedback_action",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["job_feedback.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id", "idempotency_key",
            name="uq_job_feedback_events_actor_key",
        ),
    )
    op.create_index(
        "ix_job_feedback_events_actor_user_id",
        "job_feedback_events",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_job_feedback_events_feedback_created",
        "job_feedback_events",
        ["feedback_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("job_feedback_events")
    op.drop_table("job_feedback")
