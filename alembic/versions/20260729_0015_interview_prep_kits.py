"""interview prep kits

Revision ID: 20260729_0015
Revises: 20260729_0014
Create Date: 2026-07-29 13:00:00.000000

Adds the ``interview_prep_kits`` table backing the interview-prep skill.  One
row per user-scoped interview-prep request: the target job snapshot, optional
links to a verified JobPosting and a ConfirmedProfileVersion, the generated
prep content (JSON), a preferences/match snapshot used for personalization, and
a lifecycle status (generating -> ready / failed).

Like company research this is read-only study material - no submission and no
review_version column.  Failure is bounded to the LLM not producing parseable
output (``failed``); there is no anti-bot / captcha path because no crawl runs.
The target_job / profile_version links are ``SET NULL`` so deleting a job or
profile version never orphans a kit (its job_snapshot / content is retained).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0015"
down_revision: Union[str, Sequence[str], None] = "20260729_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the interview_prep_kits table."""
    op.create_table(
        "interview_prep_kits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_job_id",
            sa.String(length=36),
            sa.ForeignKey("job_postings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "profile_version_id",
            sa.String(length=36),
            sa.ForeignKey("confirmed_profile_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_snapshot", sa.JSON(), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "generating",
                "ready",
                "failed",
                name="interview_prep_kit_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("content_json", sa.JSON(), nullable=True),
        sa.Column("preferences_summary_json", sa.JSON(), nullable=True),
        sa.Column("match_analysis_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_interview_prep_user_status_created",
        "interview_prep_kits",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_interview_prep_kits_target_job_id",
        "interview_prep_kits",
        ["target_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_interview_prep_kits_profile_version_id",
        "interview_prep_kits",
        ["profile_version_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the interview_prep_kits table."""
    op.drop_index(
        "ix_interview_prep_kits_profile_version_id",
        table_name="interview_prep_kits",
    )
    op.drop_index(
        "ix_interview_prep_kits_target_job_id", table_name="interview_prep_kits"
    )
    op.drop_index(
        "ix_interview_prep_user_status_created", table_name="interview_prep_kits"
    )
    op.drop_table("interview_prep_kits")
