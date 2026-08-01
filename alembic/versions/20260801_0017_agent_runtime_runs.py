"""adaptive PEV agent runtime records

Revision ID: 20260801_0017
Revises: 20260729_0016
Create Date: 2026-08-01 22:00:00.000000

Creates the MySQL-authoritative audit trail for the custom Planner–Executor–
Verifier runtime.  Redis/LangGraph checkpoints are intentionally not used for
these business records.  Payload JSON is constrained by application schemas to
safe summaries and artifact references; raw prompts, resume bytes and secrets
must not be stored here.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0017"
down_revision: Union[str, Sequence[str], None] = "20260729_0016"
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
    """Create durable PEV runs, plan revisions, steps, turns and events."""
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("allowed_skills_json", sa.JSON(), nullable=False),
        sa.Column("context_summary_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=False),
        sa.Column("complexity", _enum("L1", "L2", "L3", "L4", name="agent_complexity_level"), nullable=True),
        sa.Column("status", _enum("queued", "running", "waiting_user", "succeeded", "failed", "cancelled", name="agent_run_status"), nullable=False),
        sa.Column("final_summary", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"], unique=False)
    op.create_index("ix_agent_runs_status", "agent_runs", ["status"], unique=False)
    op.create_index("ix_agent_runs_user_status_created", "agent_runs", ["user_id", "status", "created_at"], unique=False)

    op.create_table(
        "agent_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("complexity", _enum("L1", "L2", "L3", "L4", name="agent_plan_complexity_level"), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("created_by", _enum("planner", "executor", "verifier", name="agent_role"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "revision", name="uq_agent_plans_run_revision"),
    )
    op.create_index("ix_agent_plans_run_revision", "agent_plans", ["run_id", "revision"], unique=False)

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.String(length=36), sa.ForeignKey("agent_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("allowed_skills_json", sa.JSON(), nullable=False),
        sa.Column("status", _enum("planned", "running", "succeeded", "failed", "skipped", name="agent_step_status"), nullable=False),
        sa.Column("input_summary_json", sa.JSON(), nullable=True),
        sa.Column("output_artifact_refs_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "sequence", name="uq_agent_steps_plan_sequence"),
    )
    op.create_index("ix_agent_steps_run_sequence", "agent_steps", ["run_id", "sequence"], unique=False)

    op.create_table(
        "agent_turns",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", _enum("planner", "executor", "verifier", name="agent_turn_role"), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "role", "turn_index", name="uq_agent_turns_run_role_index"),
    )
    op.create_index("ix_agent_turns_run_created", "agent_turns", ["run_id", "created_at"], unique=False)

    op.create_table(
        "agent_events",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_events_run_sequence"),
    )
    op.create_index("ix_agent_events_run_sequence", "agent_events", ["run_id", "sequence"], unique=False)


def downgrade() -> None:
    """Drop PEV runtime records in dependency order."""
    op.drop_index("ix_agent_events_run_sequence", table_name="agent_events")
    op.drop_table("agent_events")
    op.drop_index("ix_agent_turns_run_created", table_name="agent_turns")
    op.drop_table("agent_turns")
    op.drop_index("ix_agent_steps_run_sequence", table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_plans_run_revision", table_name="agent_plans")
    op.drop_table("agent_plans")
    op.drop_index("ix_agent_runs_user_status_created", table_name="agent_runs")
    op.drop_index("ix_agent_runs_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_table("agent_runs")
