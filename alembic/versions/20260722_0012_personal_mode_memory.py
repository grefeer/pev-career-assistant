"""personal mode memory tables

Revision ID: 20260722_0012
Revises: ffc4f5917966
Create Date: 2026-07-22 12:00:00.000000

Adds the personal-mode application-assistant memory layer:
  - user_preferences: what the user is looking for (complements profile facts)
  - user_job_interactions: behavioral log feeding the relevance feedback loop
  - job_relevance_scores: cached cheap-ranker output keyed by profile+pref
    version, so the expensive per-job MatchService only runs on ranked top-N.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0012"
down_revision: Union[str, Sequence[str], None] = "ffc4f5917966"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create personal-mode memory tables."""

    # -- user_preferences ---------------------------------------------------
    op.create_table(
        "user_preferences",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("desired_roles", sa.JSON(), nullable=True),
        sa.Column("target_cities", sa.JSON(), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("excluded_companies", sa.JSON(), nullable=True),
        sa.Column("excluded_industries", sa.JSON(), nullable=True),
        sa.Column("preferred_industries", sa.JSON(), nullable=True),
        sa.Column("preferred_recruitment_types", sa.JSON(), nullable=True),
        sa.Column(
            "work_mode",
            sa.Enum(
                "onsite",
                "hybrid",
                "remote",
                name="work_mode_preference",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=True,
        ),
        sa.Column("is_active_search", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_user_preferences_user"),
    )
    op.create_index(
        op.f("ix_user_preferences_user_id"),
        "user_preferences",
        ["user_id"],
        unique=True,
    )

    # -- user_job_interactions ----------------------------------------------
    op.create_table(
        "user_job_interactions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.String(length=36),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "interaction_type",
            sa.Enum(
                "viewed",
                "dismissed",
                "saved",
                "hidden",
                "clicked_apply",
                name="job_interaction_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "job_id", "interaction_type",
            name="uq_user_job_interactions_user_job_type",
        ),
    )
    op.create_index(
        "ix_user_job_interactions_user_created",
        "user_job_interactions",
        ["user_id", "created_at"],
        unique=False,
    )

    # -- job_relevance_scores ------------------------------------------------
    op.create_table(
        "job_relevance_scores",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.String(length=36),
            sa.ForeignKey("job_postings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "profile_version_id",
            sa.String(length=36),
            sa.ForeignKey(
                "confirmed_profile_versions.id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column("preferences_version", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("matched_signals_json", sa.JSON(), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "profile_version_id", "preferences_version",
            name="uq_job_relevance_scores_job_profile_prefs",
        ),
    )
    op.create_index(
        "ix_job_relevance_scores_user_score",
        "job_relevance_scores",
        ["user_id", "score"],
        unique=False,
    )


def downgrade() -> None:
    """Drop personal-mode memory tables."""
    op.drop_index(
        "ix_job_relevance_scores_user_score",
        table_name="job_relevance_scores",
    )
    op.drop_table("job_relevance_scores")
    op.drop_index(
        "ix_user_job_interactions_user_created",
        table_name="user_job_interactions",
    )
    op.drop_table("user_job_interactions")
    op.drop_index(
        op.f("ix_user_preferences_user_id"),
        table_name="user_preferences",
    )
    op.drop_table("user_preferences")
