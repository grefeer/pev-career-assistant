"""add authoritative job sync schema

Revision ID: 20260715_0003
Revises: 20260715_0002
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260715_0003"
down_revision: Union[str, Sequence[str], None] = "20260715_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_sources",
        sa.Column("source_key", sa.String(length=100), nullable=False),
        sa.Column(
            "provider",
            sa.Enum(
                "tencent_smartsheet",
                name="job_source_provider",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("file_id", sa.String(length=100), nullable=False),
        sa.Column("sheet_id", sa.String(length=100), nullable=False),
        sa.Column("mapper_version", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_successful_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_sync_run_id", sa.String(length=36), nullable=True),
        sa.Column("sync_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", name="uq_job_sources_source_key"),
        sa.UniqueConstraint(
            "provider", "file_id", "sheet_id", name="uq_job_sources_location"
        ),
    )
    op.create_index(
        op.f("ix_job_sources_active_sync_run_id"),
        "job_sources",
        ["active_sync_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_job_sources_sync_lease_expires_at"),
        "job_sources",
        ["sync_lease_expires_at"],
        unique=False,
    )
    op.create_table(
        "job_sync_runs",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                name="job_sync_run_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("pages_read", sa.Integer(), nullable=False),
        sa.Column("records_read", sa.Integer(), nullable=False),
        sa.Column("raw_snapshots_created", sa.Integer(), nullable=False),
        sa.Column("postings_created", sa.Integer(), nullable=False),
        sa.Column("postings_updated", sa.Integer(), nullable=False),
        sa.Column("records_skipped_incomplete", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_job_sync_runs_source_started",
        "job_sync_runs",
        ["source_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "raw_job_records",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("external_record_id", sa.String(length=100), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_fields", sa.JSON(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "external_record_id",
            "payload_hash",
            name="uq_raw_job_records_snapshot",
        ),
    )
    op.create_index(
        "ix_raw_job_records_source_record",
        "raw_job_records",
        ["source_id", "external_record_id"],
        unique=False,
    )
    op.create_table(
        "job_postings",
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("external_record_id", sa.String(length=100), nullable=False),
        sa.Column("raw_record_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_completion",
                name="job_posting_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("recruitment_types", sa.JSON(), nullable=False),
        sa.Column("industries", sa.JSON(), nullable=False),
        sa.Column("apply_url", sa.Text(), nullable=False),
        sa.Column("referral_code", sa.String(length=255), nullable=True),
        sa.Column("deadline_text", sa.String(length=255), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mapper_version", sa.String(length=40), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["raw_record_id"], ["raw_job_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["job_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "external_record_id", name="uq_job_postings_source_record"
        ),
    )
    op.create_index(
        op.f("ix_job_postings_company_name"),
        "job_postings",
        ["company_name"],
        unique=False,
    )
    op.create_index(
        "ix_job_postings_status_updated",
        "job_postings",
        ["status", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("job_postings")
    op.drop_table("raw_job_records")
    op.drop_table("job_sync_runs")
    op.drop_table("job_sources")
