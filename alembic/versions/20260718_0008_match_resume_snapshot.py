"""create match_reports, resume_drafts, approved_resume_versions,
approved_resume_attachments, application_snapshots tables and
extend application_tasks with task_kind, simulation_scenario,
request_idempotency_key

Revision ID: 20260718_0008
Revises: 20260717_0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0008"
down_revision: Union[str, Sequence[str], None] = "20260717_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- 1. match_reports ---
    op.create_table(
        "match_reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_session_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("profile_version_id", sa.String(36), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("scoring_rule_version", sa.String(64), nullable=False),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("gaps", sa.JSON(), nullable=False),
        sa.Column("unknowns", sa.JSON(), nullable=False),
        sa.Column("risks", sa.JSON(), nullable=False),
        sa.Column("application_priority", sa.String(20), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("output_schema_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_session_id"], ["analysis_sessions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job_postings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["confirmed_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_match_reports_analysis_session_id",
        "match_reports",
        ["analysis_session_id"],
    )
    op.create_index("ix_match_reports_job_id", "match_reports", ["job_id"])
    op.create_index(
        "ix_match_reports_profile_version_id",
        "match_reports",
        ["profile_version_id"],
    )
    op.create_index(
        "ix_match_reports_created_at",
        "match_reports",
        ["created_at"],
    )
    op.create_check_constraint(
        "ck_match_reports_score",
        "match_reports",
        "score >= 0 AND score <= 100",
    )
    op.create_check_constraint(
        "ck_match_reports_application_priority",
        "match_reports",
        "application_priority IN ('high','medium','low','not_recommended')",
    )
    op.create_check_constraint(
        "ck_match_reports_status",
        "match_reports",
        "status IN ('completed','failed')",
    )

    # --- 2. resume_drafts ---
    op.create_table(
        "resume_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("match_report_id", sa.String(36), nullable=False),
        sa.Column("profile_version_id", sa.String(36), nullable=False),
        sa.Column("target_job_id", sa.String(36), nullable=False),
        sa.Column("diffs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["match_report_id"], ["match_reports.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["confirmed_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_job_id"], ["job_postings.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_report_id", name="uq_resume_drafts_match_report"),
    )
    op.create_index(
        "ix_resume_drafts_target_job_status",
        "resume_drafts",
        ["target_job_id", "status"],
    )
    op.create_check_constraint(
        "ck_resume_drafts_status",
        "resume_drafts",
        "status IN ('draft','approved','rejected')",
    )

    # --- 3. approved_resume_versions ---
    op.create_table(
        "approved_resume_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("profile_version_id", sa.String(36), nullable=False),
        sa.Column("target_job_id", sa.String(36), nullable=False),
        sa.Column("approved_facts", sa.JSON(), nullable=False),
        sa.Column("approved_diffs", sa.JSON(), nullable=False),
        sa.Column("attachment_refs", sa.JSON(), nullable=False),
        sa.Column("approval_idempotency_key", sa.String(96), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["resume_drafts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["confirmed_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_job_id"], ["job_postings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", name="uq_approved_resume_versions_draft"),
        sa.UniqueConstraint(
            "approved_by",
            "approval_idempotency_key",
            name="uq_approved_resume_versions_idempotency",
        ),
    )

    # --- 4. approved_resume_attachments ---
    op.create_table(
        "approved_resume_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("draft_id", sa.String(36), nullable=False),
        sa.Column("approved_resume_version_id", sa.String(36), nullable=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("plaintext_size", sa.BigInteger(), nullable=False),
        sa.Column("encryption_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_id"], ["resume_drafts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_resume_version_id"],
            ["approved_resume_versions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key", name="uq_approved_attachments_object_key"),
        sa.UniqueConstraint(
            "draft_id", "format", name="uq_approved_attachments_draft_format"
        ),
    )

    # --- 5. application_snapshots ---
    op.create_table(
        "application_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("approved_resume_version_id", sa.String(36), nullable=False),
        sa.Column("profile_version_id", sa.String(36), nullable=False),
        sa.Column("job_snapshot", sa.JSON(), nullable=False),
        sa.Column("profile_facts", sa.JSON(), nullable=False),
        sa.Column("dynamic_answers", sa.JSON(), nullable=False),
        sa.Column("local_sensitive_requirements", sa.JSON(), nullable=False),
        sa.Column("attachment_refs", sa.JSON(), nullable=False),
        sa.Column("gui_eligible", sa.Boolean(), nullable=False),
        sa.Column("job_status_at_snapshot", sa.String(20), nullable=False),
        sa.Column("job_review_version_at_snapshot", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(36), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["job_postings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_resume_version_id"],
            ["approved_resume_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["profile_version_id"],
            ["confirmed_profile_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_snapshots_user_created",
        "application_snapshots",
        ["user_id", "created_at"],
    )
    op.create_check_constraint(
        "ck_application_snapshots_status",
        "application_snapshots",
        "job_status_at_snapshot IN ('pending_completion','pending_review','verified','expired','rejected')",
    )

    # --- 6. Extend application_tasks ---
    # 6a. Add new columns
    op.add_column(
        "application_tasks",
        sa.Column("task_kind", sa.String(20), nullable=True),
    )
    op.add_column(
        "application_tasks",
        sa.Column("simulation_scenario", sa.String(100), nullable=True),
    )
    op.add_column(
        "application_tasks",
        sa.Column("request_idempotency_key", sa.String(96), nullable=True),
    )

    # 6b. Make target_job_id nullable (needed for simulation rows)
    op.alter_column(
        "application_tasks",
        "target_job_id",
        nullable=True,
        existing_type=sa.String(36),
    )

    # 6c. Data migration: classify exiting rows
    op.execute(
        "UPDATE application_tasks "
        "SET task_kind='application' "
        "WHERE snapshot_id IS NOT NULL"
    )
    op.execute(
        "UPDATE application_tasks "
        "SET task_kind='simulation', "
        "    simulation_scenario=target_job_id, "
        "    target_job_id=NULL "
        "WHERE snapshot_id IS NULL"
    )

    # 6d. Add FK constraints on application_tasks
    op.create_foreign_key(
        "fk_application_tasks_snapshot_id",
        "application_tasks",
        "application_snapshots",
        ["snapshot_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_application_tasks_target_job_id",
        "application_tasks",
        "job_postings",
        ["target_job_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 6e. Add CHECK constraint for task_kind validity
    op.create_check_constraint(
        "ck_application_tasks_kind",
        "application_tasks",
        "(task_kind = 'simulation' AND simulation_scenario IS NOT NULL "
        "AND target_job_id IS NULL AND snapshot_id IS NULL) "
        "OR (task_kind = 'application' AND simulation_scenario IS NULL "
        "AND target_job_id IS NOT NULL AND snapshot_id IS NOT NULL)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # --- 1. Revert application_tasks changes ---
    # Drop CHECK constraint
    op.drop_constraint(
        "ck_application_tasks_kind", "application_tasks", type_="check"
    )
    # Drop FK constraints
    op.drop_constraint(
        "fk_application_tasks_snapshot_id", "application_tasks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_application_tasks_target_job_id", "application_tasks", type_="foreignkey"
    )

    # Restore target_job_id from simulation_scenario for simulation tasks
    op.execute(
        "UPDATE application_tasks "
        "SET target_job_id = simulation_scenario "
        "WHERE task_kind = 'simulation'"
    )

    # Make target_job_id NOT NULL again
    op.alter_column(
        "application_tasks",
        "target_job_id",
        nullable=False,
        existing_type=sa.String(36),
    )

    # Drop new columns
    op.drop_column("application_tasks", "request_idempotency_key")
    op.drop_column("application_tasks", "simulation_scenario")
    op.drop_column("application_tasks", "task_kind")

    # --- 2. Drop new tables respecting FK order ---
    # application_snapshots and approved_resume_attachments reference
    # approved_resume_versions and resume_drafts, so drop them first.
    op.drop_table("approved_resume_attachments")
    op.drop_table("application_snapshots")
    op.drop_table("approved_resume_versions")
    op.drop_table("resume_drafts")
    op.drop_table("match_reports")
