"""add manual job import and deduplication

Revision ID: 20260717_0006
Revises: 20260717_0005
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260717_0006"
down_revision: Union[str, Sequence[str], None] = "20260717_0005"
branch_labels = None
depends_on = None

MANUAL_SOURCE_ID = "00000000-0000-4000-8000-000000000006"
MANUAL_SOURCE_KEY = "manual-user-submissions"


def upgrade() -> None:
    op.drop_constraint("job_source_provider", "job_sources", type_="check")
    op.create_check_constraint(
        "job_source_provider",
        "job_sources",
        "provider IN ('tencent_smartsheet','user_submission')",
    )
    op.create_table(
        "user_job_submissions",
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column(
            "input_type",
            sa.Enum("url", "jd_text", name="job_submission_input_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("original_jd", sa.Text(), nullable=True),
        sa.Column("input_preview", sa.String(240), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "submitted", "promoted", "rejected", name="job_submission_status", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "deduplication_status",
            sa.Enum("pending", "succeeded", "failed", name="job_deduplication_status", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("deduplication_error_code", sa.String(80), nullable=True),
        sa.Column("promoted_job_id", sa.String(36), nullable=True),
        sa.Column("rejected_reason_code", sa.String(80), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["promoted_job_id"], ["job_postings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_job_submissions_user_status_updated",
        "user_job_submissions", ["user_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_user_job_submissions_promoted_job_id",
        "user_job_submissions", ["promoted_job_id"],
    )
    op.create_table(
        "job_duplicate_candidates",
        sa.Column("submission_id", sa.String(36), nullable=False),
        sa.Column("candidate_job_id", sa.String(36), nullable=False),
        sa.Column("generated_for_version", sa.Integer(), nullable=False),
        sa.Column("score_basis_points", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("score_components", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "score_basis_points >= 0 AND score_basis_points <= 10000",
            name="ck_job_duplicate_candidate_score",
        ),
        sa.ForeignKeyConstraint(["candidate_job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["submission_id"], ["user_job_submissions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id", "candidate_job_id", "generated_for_version",
            "algorithm_version", name="uq_job_duplicate_candidate_version",
        ),
    )
    op.create_index(
        "ix_job_duplicate_candidates_submission_version",
        "job_duplicate_candidates", ["submission_id", "generated_for_version"],
    )
    op.create_table(
        "job_source_links",
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column(
            "source_type",
            sa.Enum("tencent_smartsheet", "user_submission", name="job_source_link_type", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(36), nullable=True),
        sa.Column("submission_id", sa.String(36), nullable=True),
        sa.Column("source_record_ref", sa.String(200), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.CheckConstraint(
            "(source_type = 'tencent_smartsheet' AND source_id IS NOT NULL AND submission_id IS NULL) OR "
            "(source_type = 'user_submission' AND source_id IS NULL AND submission_id IS NOT NULL)",
            name="ck_job_source_link_reference",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_id"], ["user_job_submissions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "source_type", "source_record_ref", name="uq_job_source_link_record"),
    )
    op.create_index("ix_job_source_links_job_created", "job_source_links", ["job_id", "created_at"])
    op.execute(
        sa.text(
            "INSERT INTO job_sources "
            "(id, source_key, provider, name, file_id, sheet_id, mapper_version, enabled, created_at, updated_at) "
            "VALUES (:id, :source_key, 'user_submission', '用户手动提交', 'manual', 'manual', "
            "'manual-submission-v1', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ).bindparams(id=MANUAL_SOURCE_ID, source_key=MANUAL_SOURCE_KEY)
    )
    op.execute(
        """
        INSERT INTO job_source_links
            (id, job_id, source_type, source_id, submission_id, source_record_ref, normalized_url, created_at)
        SELECT UUID(), jp.id, 'tencent_smartsheet', jp.source_id, NULL,
               CONCAT(jp.source_id, ':', jp.external_record_id), jp.apply_url, UTC_TIMESTAMP()
        FROM job_postings AS jp
        JOIN job_sources AS js ON js.id = jp.source_id
        WHERE js.provider = 'tencent_smartsheet'
        """
    )


def downgrade() -> None:
    op.drop_table("job_source_links")
    op.drop_table("job_duplicate_candidates")
    op.drop_table("user_job_submissions")
    op.execute(
        f"DELETE jv FROM job_verifications jv JOIN job_postings jp ON jp.id = jv.job_id WHERE jp.source_id = '{MANUAL_SOURCE_ID}'"
    )
    op.execute(f"DELETE FROM job_postings WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM raw_job_records WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM job_sync_runs WHERE source_id = '{MANUAL_SOURCE_ID}'")
    op.execute(f"DELETE FROM job_sources WHERE id = '{MANUAL_SOURCE_ID}'")
    op.drop_constraint("job_source_provider", "job_sources", type_="check")
    op.create_check_constraint(
        "job_source_provider", "job_sources", "provider IN ('tencent_smartsheet')"
    )
