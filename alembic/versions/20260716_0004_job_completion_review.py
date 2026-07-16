"""add authoritative job completion review

Revision ID: 20260716_0004
Revises: 20260715_0003
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260716_0004"
down_revision: Union[str, Sequence[str], None] = "20260715_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("job_posting_status", "job_postings", type_="check")
    op.create_check_constraint(
        "job_posting_status",
        "job_postings",
        "status IN ('pending_completion','pending_review','verified','expired','rejected')",
    )
    op.add_column("job_postings", sa.Column("description_text", sa.Text(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column(
            "source_candidate",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("(JSON_OBJECT())"),
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column(
            "source_changed_since_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "job_postings",
        sa.Column("gui_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "job_postings",
        sa.Column("review_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("job_postings", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.add_column("job_postings", sa.Column("expired_at", sa.DateTime(timezone=True)))
    op.add_column("job_postings", sa.Column("rejected_at", sa.DateTime(timezone=True)))
    op.execute(
        """
        UPDATE job_postings
        SET source_candidate = JSON_OBJECT(
            'company_name', company_name,
            'title', title,
            'locations', locations,
            'recruitment_types', recruitment_types,
            'industries', industries,
            'apply_url', apply_url,
            'referral_code', referral_code,
            'deadline_text', deadline_text
        )
        """
    )
    op.alter_column("job_postings", "source_candidate", server_default=None)
    op.alter_column("job_postings", "source_changed_since_review", server_default=None)
    op.alter_column("job_postings", "gui_eligible", server_default=None)
    op.alter_column("job_postings", "review_version", server_default=None)
    op.create_table(
        "job_verifications",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(40), nullable=False),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("field_snapshot", sa.JSON(), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_verifications_actor_user_id",
        "job_verifications",
        ["actor_user_id"],
    )
    op.create_index(
        "ix_job_verifications_job_created",
        "job_verifications",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("job_verifications")
    op.execute("UPDATE job_postings SET status = 'pending_completion'")
    op.drop_constraint("job_posting_status", "job_postings", type_="check")
    op.create_check_constraint(
        "job_posting_status",
        "job_postings",
        "status IN ('pending_completion')",
    )
    for name in (
        "rejected_at",
        "expired_at",
        "verified_at",
        "review_version",
        "gui_eligible",
        "source_changed_since_review",
        "source_candidate",
        "description_text",
    ):
        op.drop_column("job_postings", name)
