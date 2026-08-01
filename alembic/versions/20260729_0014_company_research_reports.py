"""company research reports

Revision ID: 20260729_0014
Revises: 20260725_0013
Create Date: 2026-07-29 12:00:00.000000

Adds the ``company_research_reports`` table backing the company-research
skill.  One row per user-scoped company research request: a company name, the
public careers/about URL it was built from, the extracted company profile and
open-position list (JSON), evidence references, and a lifecycle status that
mirrors the job-discovery vocabulary (queued -> running -> succeeded /
needs_manual_review / failed / cancelled).

Security gates are enforced in code, not here: a login/captcha/anti-bot wall
surfaces as ``needs_manual_review`` and is never bypassed.  No submission or
review_version column exists - company research is read-only research, never
an auto-submit.  This migration is decoupled from JobPosting /
discovered_job_candidates / the verified-only /jobs path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0014"
down_revision: Union[str, Sequence[str], None] = "20260725_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the company_research_reports table."""
    op.create_table(
        "company_research_reports",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(length=256), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_url_hash", sa.String(length=64), nullable=False),
        sa.Column("agent_version", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "needs_manual_review",
                "failed",
                "cancelled",
                name="company_research_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "block_reason",
            sa.Enum(
                "anti_bot",
                "login_required",
                "captcha",
                "no_evidence",
                "artifact_error",
                name="company_research_block_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("profile_json", sa.JSON(), nullable=True),
        sa.Column("openings_json", sa.JSON(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_research_user_status_created",
        "company_research_reports",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_company_research_url_hash",
        "company_research_reports",
        ["source_url_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the company_research_reports table."""
    op.drop_index(
        "ix_company_research_url_hash", table_name="company_research_reports"
    )
    op.drop_index(
        "ix_company_research_user_status_created",
        table_name="company_research_reports",
    )
    op.drop_table("company_research_reports")
