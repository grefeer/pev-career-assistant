"""align wave2 tables with current ORM models

Revision ID: 20260718_0011
Revises: 20260718_0010
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260718_0011"
down_revision: Union[str, Sequence[str], None] = "20260718_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Bring Wave 2 tables into line with the checked-in ORM models."""
    op.add_column("match_reports", sa.Column("user_id", sa.String(36), nullable=True))
    op.add_column(
        "match_reports",
        sa.Column("job_verification_id", sa.String(36), nullable=True),
    )
    op.add_column("match_reports", sa.Column("job_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "match_reports",
        sa.Column("request_idempotency_key", sa.String(96), nullable=True),
    )
    op.add_column("match_reports", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column("match_reports", sa.Column("score_components", sa.JSON(), nullable=True))
    op.add_column("match_reports", sa.Column("error_code", sa.String(80), nullable=True))
    op.add_column(
        "match_reports",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "match_reports",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.alter_column("match_reports", "score", existing_type=sa.Integer(), nullable=True)
    op.alter_column("match_reports", "strengths", existing_type=sa.JSON(), nullable=True)
    op.alter_column("match_reports", "gaps", existing_type=sa.JSON(), nullable=True)
    op.alter_column("match_reports", "unknowns", existing_type=sa.JSON(), nullable=True)
    op.alter_column("match_reports", "risks", existing_type=sa.JSON(), nullable=True)
    op.alter_column(
        "match_reports",
        "application_priority",
        existing_type=sa.String(20),
        nullable=True,
    )
    op.alter_column(
        "match_reports",
        "recommendation",
        existing_type=sa.Text(),
        type_=sa.JSON(),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_match_reports_user_id",
        "match_reports",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_match_reports_job_verification_id",
        "match_reports",
        "job_verifications",
        ["job_verification_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_match_reports_user_id_request_idempotency_key",
        "match_reports",
        ["user_id", "request_idempotency_key"],
    )
    op.create_index(
        "ix_match_reports_user_id_created_at",
        "match_reports",
        ["user_id", "created_at"],
    )

    with op.batch_alter_table("resume_drafts") as batch_op:
        batch_op.drop_constraint("ck_resume_drafts_status", type_="check")
        batch_op.create_check_constraint(
            "ck_resume_drafts_status",
            "status IN ('generating','draft','approved','rejected','failed')",
        )
    op.add_column("resume_drafts", sa.Column("user_id", sa.String(36), nullable=True))
    op.add_column(
        "resume_drafts",
        sa.Column("request_idempotency_key", sa.String(96), nullable=True),
    )
    op.add_column("resume_drafts", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column("resume_drafts", sa.Column("error_code", sa.String(80), nullable=True))
    op.add_column(
        "resume_drafts",
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("resume_drafts", "diffs", existing_type=sa.JSON(), nullable=True)
    op.create_foreign_key(
        "fk_resume_drafts_user_id",
        "resume_drafts",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_resume_drafts_user_id_request_idempotency_key",
        "resume_drafts",
        ["user_id", "request_idempotency_key"],
    )

    op.add_column(
        "application_snapshots",
        sa.Column("request_idempotency_key", sa.String(96), nullable=True),
    )
    op.add_column(
        "application_snapshots",
        sa.Column("request_hash", sa.String(64), nullable=True),
    )
    op.add_column("application_snapshots", sa.Column("attachment_ids", sa.JSON(), nullable=True))
    op.create_unique_constraint(
        "uq_application_snapshots_user_id_request_idempotency_key",
        "application_snapshots",
        ["user_id", "request_idempotency_key"],
    )


def downgrade() -> None:
    """Undo the Wave 2 schema alignment."""
    op.drop_constraint(
        "uq_application_snapshots_user_id_request_idempotency_key",
        "application_snapshots",
        type_="unique",
    )
    op.drop_column("application_snapshots", "attachment_ids")
    op.drop_column("application_snapshots", "request_hash")
    op.drop_column("application_snapshots", "request_idempotency_key")

    op.drop_constraint(
        "uq_resume_drafts_user_id_request_idempotency_key",
        "resume_drafts",
        type_="unique",
    )
    op.drop_constraint("fk_resume_drafts_user_id", "resume_drafts", type_="foreignkey")
    op.alter_column("resume_drafts", "diffs", existing_type=sa.JSON(), nullable=False)
    op.drop_column("resume_drafts", "state_version")
    op.drop_column("resume_drafts", "error_code")
    op.drop_column("resume_drafts", "request_hash")
    op.drop_column("resume_drafts", "request_idempotency_key")
    op.drop_column("resume_drafts", "user_id")
    with op.batch_alter_table("resume_drafts") as batch_op:
        batch_op.drop_constraint("ck_resume_drafts_status", type_="check")
        batch_op.create_check_constraint(
            "ck_resume_drafts_status",
            "status IN ('draft','approved','rejected')",
        )

    op.drop_index("ix_match_reports_user_id_created_at", table_name="match_reports")
    op.drop_constraint(
        "uq_match_reports_user_id_request_idempotency_key",
        "match_reports",
        type_="unique",
    )
    op.drop_constraint(
        "fk_match_reports_job_verification_id",
        "match_reports",
        type_="foreignkey",
    )
    op.drop_constraint("fk_match_reports_user_id", "match_reports", type_="foreignkey")
    op.alter_column(
        "match_reports",
        "recommendation",
        existing_type=sa.JSON(),
        type_=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "match_reports",
        "application_priority",
        existing_type=sa.String(20),
        nullable=False,
    )
    op.alter_column("match_reports", "risks", existing_type=sa.JSON(), nullable=False)
    op.alter_column("match_reports", "unknowns", existing_type=sa.JSON(), nullable=False)
    op.alter_column("match_reports", "gaps", existing_type=sa.JSON(), nullable=False)
    op.alter_column("match_reports", "strengths", existing_type=sa.JSON(), nullable=False)
    op.alter_column("match_reports", "score", existing_type=sa.Integer(), nullable=False)
    op.drop_column("match_reports", "completed_at")
    op.drop_column("match_reports", "started_at")
    op.drop_column("match_reports", "error_code")
    op.drop_column("match_reports", "score_components")
    op.drop_column("match_reports", "request_hash")
    op.drop_column("match_reports", "request_idempotency_key")
    op.drop_column("match_reports", "job_snapshot")
    op.drop_column("match_reports", "job_verification_id")
    op.drop_column("match_reports", "user_id")
