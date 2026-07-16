"""add job feedback table

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
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "closed",
                "application_channel_unavailable",
                "content_changed",
                "incorrect_information",
                name="job_feedback_category",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_job_feedback_idempotency_key"),
    )
    op.create_index(
        "ix_job_feedback_job_created",
        "job_feedback",
        ["job_id", "created_at"],
    )
    op.create_index(
        "ix_job_feedback_user_created",
        "job_feedback",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_job_feedback_user_created", table_name="job_feedback")
    op.drop_index("ix_job_feedback_job_created", table_name="job_feedback")
    op.drop_constraint("uq_job_feedback_idempotency_key", "job_feedback", type_="unique")
    op.drop_table("job_feedback")
    op.execute("DROP TYPE IF EXISTS job_feedback_category")
