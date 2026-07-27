"""personalized job discovery v1

Revision ID: 20260725_0013
Revises: 7e8f22313271, 20260722_0012
Create Date: 2026-07-25 12:00:00.000000

Merges the two prior heads (job-discovery tables + personal-mode memory) and
adds the owner-scoped pre-review delivery channel:
  - user_preferences: + role_synonyms, excluded_roles,
    personalized_discovery_min_score
  - personalized_discovery_runs: one user-scoped run per personalized discovery
  - personalized_discovery_recommendations: user-scoped pre-review delivery
    keyed by (user_id, canonical_job_key); candidate/task FKs are RESTRICT so a
    delivered recommendation keeps its evidence trace while it exists.
  - user_discovery_source_statuses: closed-reason explanation for a source that
    could not be recommended (no raw wall text / cookies / tokens).

Never mutates JobPosting / review_version / the verified-only /jobs path.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260725_0013"
down_revision: Union[str, Sequence[str], None] = (
    "7e8f22313271",
    "20260722_0012",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add personalized-discovery preference columns + owner-scoped tables."""

    # -- extend user_preferences -------------------------------------------
    op.add_column(
        "user_preferences",
        sa.Column("role_synonyms", sa.JSON(), nullable=True),
    )
    op.add_column(
        "user_preferences",
        sa.Column("excluded_roles", sa.JSON(), nullable=True),
    )
    op.add_column(
        "user_preferences",
        sa.Column(
            "personalized_discovery_min_score", sa.Float(), nullable=True
        ),
    )

    # -- personalized_discovery_runs ---------------------------------------
    op.create_table(
        "personalized_discovery_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("preference_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pdr_user_started",
        "personalized_discovery_runs",
        ["user_id", "started_at"],
        unique=False,
    )

    # -- personalized_discovery_recommendations ----------------------------
    op.create_table(
        "personalized_discovery_recommendations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=36),
            sa.ForeignKey(
                "discovered_job_candidates.id", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("job_discovery_tasks.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "last_run_id",
            sa.String(length=36),
            sa.ForeignKey(
                "personalized_discovery_runs.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column("canonical_job_key", sa.String(length=128), nullable=False),
        sa.Column("preference_version", sa.Integer(), nullable=False),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("relevance_reason", sa.Text(), nullable=True),
        sa.Column("matched_signals_json", sa.JSON(), nullable=True),
        sa.Column(
            "presentation_state",
            sa.Enum(
                "new",
                "viewed",
                "saved",
                "dismissed",
                "apply_clicked",
                name="recommendation_presentation_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "canonical_job_key",
            name="uq_pdr_user_canonical_job_key",
        ),
    )
    op.create_index(
        "ix_pdr_rec_user_score",
        "personalized_discovery_recommendations",
        ["user_id", "relevance_score"],
        unique=False,
    )
    op.create_index(
        "ix_pdr_rec_user_state",
        "personalized_discovery_recommendations",
        ["user_id", "presentation_state"],
        unique=False,
    )

    # -- user_discovery_source_statuses ------------------------------------
    op.create_table(
        "user_discovery_source_statuses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.String(length=36),
            sa.ForeignKey(
                "personalized_discovery_runs.id", ondelete="CASCADE"
            ),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(length=36),
            sa.ForeignKey("job_discovery_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("safe_source_url", sa.Text(), nullable=False),
        sa.Column(
            "reason_code",
            sa.Enum(
                "login_required",
                "captcha",
                "anti_bot",
                "authentication_required",
                "coverage_incomplete",
                "url_unsafe",
                "needs_manual_review",
                name="source_status_reason",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("retry_guidance", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "run_id",
            "task_id",
            "reason_code",
            name="uq_udss_user_run_task_reason",
        ),
    )
    op.create_index(
        "ix_udss_user_run",
        "user_discovery_source_statuses",
        ["user_id", "run_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop personalized-discovery tables, then preference columns."""
    op.drop_index("ix_udss_user_run", table_name="user_discovery_source_statuses")
    op.drop_table("user_discovery_source_statuses")

    op.drop_index("ix_pdr_rec_user_state", table_name="personalized_discovery_recommendations")
    op.drop_index("ix_pdr_rec_user_score", table_name="personalized_discovery_recommendations")
    op.drop_table("personalized_discovery_recommendations")

    op.drop_index("ix_pdr_user_started", table_name="personalized_discovery_runs")
    op.drop_table("personalized_discovery_runs")

    op.drop_column("user_preferences", "personalized_discovery_min_score")
    op.drop_column("user_preferences", "excluded_roles")
    op.drop_column("user_preferences", "role_synonyms")
