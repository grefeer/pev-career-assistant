"""deepagents PEV runtime records

Revision ID: 20260807_0022
Revises: 20260806_0021
Create Date: 2026-08-07 12:00:00.000000

MySQL-authoritative completion snapshots for the deepagents-based PEV
runtime.  Redis holds only the in-flight execution checkpoint; completed
run records and evidence artifacts are the authoritative copy here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260807_0022"
down_revision: Union[str, Sequence[str], None] = "20260806_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enum(*values: str, name: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=True,
    )


def upgrade() -> None:
    """Create deepagents run snapshots and evidence artifacts."""
    op.create_table(
        "deepagents_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("allowed_skills_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            _enum(
                "queued", "running", "waiting_user", "succeeded", "failed",
                "cancelled", name="deepagents_run_status",
            ),
            nullable=False,
        ),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("decisions_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", name="uq_deepagents_runs_thread_id"),
    )
    op.create_index("ix_deepagents_runs_user_id", "deepagents_runs", ["user_id"], unique=False)
    op.create_index("ix_deepagents_runs_status", "deepagents_runs", ["status"], unique=False)

    op.create_table(
        "deepagents_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey("deepagents_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "artifact_id", name="uq_deepagents_artifacts_run_artifact"
        ),
    )
    op.create_index("ix_deepagents_artifacts_run_id", "deepagents_artifacts", ["run_id"], unique=False)


def downgrade() -> None:
    """Drop deepagents snapshots in dependency order."""
    op.drop_index("ix_deepagents_artifacts_run_id", table_name="deepagents_artifacts")
    op.drop_table("deepagents_artifacts")
    op.drop_index("ix_deepagents_runs_status", table_name="deepagents_runs")
    op.drop_index("ix_deepagents_runs_user_id", table_name="deepagents_runs")
    op.drop_table("deepagents_runs")
