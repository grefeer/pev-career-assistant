"""persist immutable PEV public evidence artifacts

Revision ID: 20260801_0018
Revises: 20260801_0017
Create Date: 2026-08-01 22:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0018"
down_revision: Union[str, Sequence[str], None] = "20260801_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the immutable, run-scoped public evidence store."""
    role = sa.Enum(
        "planner", "executor", "verifier",
        name="agent_artifact_role",
        native_enum=False,
        create_constraint=True,
    )
    op.create_table(
        "agent_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_id", sa.String(length=36), sa.ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("created_by", role, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("step_id", "content_hash", name="uq_agent_artifacts_step_content_hash"),
    )
    op.create_index("ix_agent_artifacts_run_created", "agent_artifacts", ["run_id", "created_at"], unique=False)


def downgrade() -> None:
    """Drop evidence artifacts before their parent PEV rows."""
    op.drop_index("ix_agent_artifacts_run_created", table_name="agent_artifacts")
    op.drop_table("agent_artifacts")
