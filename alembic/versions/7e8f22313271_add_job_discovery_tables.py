"""add job discovery tables

Revision ID: 7e8f22313271
Revises: 20260718_0011
Create Date: 2026-07-18 23:24:16.989424
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e8f22313271"
down_revision: Union[str, Sequence[str], None] = "20260718_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add job_discovery_tasks, job_discovery_evidence, and discovered_job_candidates."""
    op.create_table(
        "job_discovery_tasks",
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("raw_record_id", sa.String(36), nullable=False),
        sa.Column("external_record_id", sa.String(255), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "partial_success",
                "succeeded",
                "needs_manual_review",
                "failed",
                "cancelled",
                name="job_discovery_task_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "block_reason",
            sa.Enum(
                "login_required",
                "captcha",
                "anti_bot",
                "wechat_unavailable",
                "permission_denied",
                "invalid_url",
                "timeout",
                "budget_exceeded",
                "parse_failed",
                "unknown",
                name="discovery_block_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("budget_json", sa.JSON(), nullable=True),
        sa.Column("result_summary_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["raw_job_records.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["job_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "source_id",
            "external_record_id",
            "url_hash",
            "payload_hash",
            "agent_version",
            name="uq_job_discovery_tasks_source_record",
        ),
    )
    op.create_index(
        "ix_job_discovery_tasks_raw_record_id",
        "job_discovery_tasks",
        ["raw_record_id"],
    )
    op.create_index(
        "ix_job_discovery_tasks_status_lease_created",
        "job_discovery_tasks",
        ["status", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "job_discovery_evidence",
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("evidence_type", sa.String(32), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("text_excerpt", sa.Text(), nullable=True),
        sa.Column("storage_uri", sa.String(1024), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["job_discovery_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "evidence_type",
            "content_hash",
            name="uq_job_discovery_evidence_task_type_hash",
        ),
    )
    op.create_index(
        "ix_job_discovery_evidence_task_created",
        "job_discovery_evidence",
        ["task_id", "created_at"],
    )

    op.create_table(
        "discovered_job_candidates",
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("raw_record_id", sa.String(36), nullable=False),
        sa.Column("external_record_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("similarity_group_key", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_review",
                "approved",
                "rejected",
                "merged",
                "needs_manual_review",
                name="discovered_job_candidate_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("company_name", sa.String(256), nullable=True),
        sa.Column("department", sa.String(256), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("responsibilities", sa.Text(), nullable=True),
        sa.Column("requirements", sa.Text(), nullable=True),
        sa.Column("locations_json", sa.JSON(), nullable=True),
        sa.Column("recruitment_types_json", sa.JSON(), nullable=True),
        sa.Column("industries_json", sa.JSON(), nullable=True),
        sa.Column("apply_url", sa.String(2048), nullable=True),
        sa.Column("application_channel_json", sa.JSON(), nullable=True),
        sa.Column("deadline_text", sa.String(256), nullable=True),
        sa.Column("referral_code", sa.String(256), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=True),
        sa.Column("normalization_warnings_json", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_record_id"],
            ["raw_job_records.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["job_sources.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["job_discovery_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_discovered_job_candidates_source_record",
        "discovered_job_candidates",
        ["source_id", "external_record_id"],
    )
    op.create_index(
        "ix_discovered_job_candidates_status_group_created",
        "discovered_job_candidates",
        ["status", "similarity_group_key", "created_at"],
    )


def downgrade() -> None:
    """Drop job discovery tables."""
    op.drop_index(
        "ix_discovered_job_candidates_status_group_created",
        table_name="discovered_job_candidates",
    )
    op.drop_index(
        "ix_discovered_job_candidates_source_record",
        table_name="discovered_job_candidates",
    )
    op.drop_table("discovered_job_candidates")
    op.drop_index(
        "ix_job_discovery_evidence_task_created",
        table_name="job_discovery_evidence",
    )
    op.drop_table("job_discovery_evidence")
    op.drop_index(
        "ix_job_discovery_tasks_status_lease_created",
        table_name="job_discovery_tasks",
    )
    op.drop_index(
        "ix_job_discovery_tasks_raw_record_id",
        table_name="job_discovery_tasks",
    )
    op.drop_table("job_discovery_tasks")
