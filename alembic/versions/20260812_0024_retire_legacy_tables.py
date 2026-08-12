"""retire 14 legacy tables (sub-project 4)

Revision ID: 20260812_0024
Revises: 20260808_0023
Create Date: 2026-08-12 09:00:00.000000

Drops the retired job-discovery / site-adapter / personalized-discovery /
analysis-session schema (14 tables) and detaches the dead
``match_reports.analysis_session_id`` column (anonymous FK, index, column).
Downgrade rebuilds the 14 tables as EMPTY tables from the original migration
DDL (see per-table copy sources below) and restores the match_reports column
as nullable (NOT NULL re-add fails on non-empty tables; recovery path only).

Drop order = children before parents within the legacy cluster:
  recommendations/source_statuses -> runs/candidates/evidence -> trajectories
  -> tasks -> strategies -> independent tables -> analysis_sessions last.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260812_0024"
down_revision: Union[str, Sequence[str], None] = "20260808_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TABLES: tuple[str, ...] = (
    # children first, parents last (verified against models.py FKs)
    "personalized_discovery_recommendations",   # -> discovered_job_candidates, job_discovery_tasks
    "user_discovery_source_statuses",           # -> personalized_discovery_runs, job_discovery_tasks
    "personalized_discovery_runs",              # -> users
    "job_discovery_evidence",                   # -> job_discovery_tasks
    "discovered_job_candidates",                # -> job_discovery_tasks
    "job_discovery_trajectories",               # -> job_discovery_strategies, job_discovery_tasks
    "job_discovery_tasks",                      # -> job_sources, raw_job_records (active parents)
    "job_discovery_strategies",                 # no FKs
    "observed_sites",                           # no FKs
    "site_adapters",                            # no FKs
    "user_preferences",                         # -> users
    "user_job_interactions",                    # -> users, job_postings
    "job_relevance_scores",                     # -> users, job_postings, confirmed_profile_versions
    "analysis_sessions",                        # -> users (last: match_reports FK target)
)


def upgrade() -> None:
    """Detach match_reports, then drop the 14 legacy tables."""
    # --- 1. match_reports: drop the anonymous FK first, then index, then column ---
    # The FK is unnamed (0008 created it without a name), so resolve the actual
    # MySQL-generated constraint name at runtime.
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT constraint_name FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE table_schema = DATABASE() AND table_name = 'match_reports' "
            "AND referenced_table_name = 'analysis_sessions'"
        )
    )
    # offline (--sql) mode returns None (no live connection); the real upgrade
    # resolves the constraint name against the live database.
    rows = result.fetchall() if result is not None else []
    if rows:
        op.drop_constraint(rows[0][0], "match_reports", type_="foreignkey")
    op.drop_index("ix_match_reports_analysis_session_id", table_name="match_reports")
    op.drop_column("match_reports", "analysis_session_id")

    # --- 2. drop the 14 legacy tables (children before parents) ---
    for table in LEGACY_TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """Rebuild the 14 empty tables and restore the match_reports column.

    Each ``op.create_table(...)`` / ``op.create_index(...)`` /
    ``op.add_column(...)`` block below is copied VERBATIM from the source
    migration's ``upgrade()``, in the order: parents before children so
    intra-cluster FKs resolve.  Data-migration statements (INSERT/UPDATE)
    are skipped - only DDL.  Source line ranges per table:
      analysis_sessions               20260714_0001_platform_foundation.py          48-70
      job_relevance_scores            20260722_0012_personal_mode_memory.py         120-165
      user_job_interactions           20260722_0012_personal_mode_memory.py         75-118
      user_preferences                20260722_0012_personal_mode_memory.py         30-72
        + 0013 add_column blocks                                                    40-52
      site_adapters                   20260718_0009_site_adapters.py                21-56
        + 0010 add_column blocks                                                    19-35
      observed_sites                  20260718_0010_multi_site_extension.py         49-76
      job_discovery_strategies        ffc4f5917966_add_strategy_and_trajectory_tables.py 21-51
      job_discovery_tasks             7e8f22313271_add_job_discovery_tables.py      22-112
      job_discovery_trajectories      ffc4f5917966_add_strategy_and_trajectory_tables.py 52-76
      discovered_job_candidates       7e8f22313271_add_job_discovery_tables.py      143-226
      job_discovery_evidence          7e8f22313271_add_job_discovery_tables.py      112-143
      personalized_discovery_runs     20260725_0013_personalized_job_discovery_v1.py 56-75
      user_discovery_source_statuses  20260725_0013_personalized_job_discovery_v1.py 155-210
      personalized_discovery_recommendations 20260725_0013_personalized_job_discovery_v1.py 82-155
    """

    # --- 1. analysis_sessions (20260714_0001_platform_foundation.py 48-70) ---
    op.execute(
        "-- downgrade: rebuilding analysis_sessions from "
        "20260714_0001_platform_foundation.py lines 48-70 "
        "(create_table + 2 indexes; unique constraint uq_analysis_sessions_thread_id)"
    )
    op.create_table(
        "analysis_sessions",
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=96), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", name="uq_analysis_sessions_thread_id"),
    )
    op.create_index(
        op.f("ix_analysis_sessions_activated_at"),
        "analysis_sessions",
        ["activated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analysis_sessions_user_id"),
        "analysis_sessions",
        ["user_id"],
        unique=False,
    )

    # --- 2. job_relevance_scores (20260722_0012_personal_mode_memory.py 120-165) ---
    op.execute(
        "-- downgrade: rebuilding job_relevance_scores from "
        "20260722_0012_personal_mode_memory.py lines 120-165 "
        "(create_table + uq + index ix_job_relevance_scores_user_score)"
    )
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

    # --- 3. user_job_interactions (20260722_0012_personal_mode_memory.py 75-118) ---
    op.execute(
        "-- downgrade: rebuilding user_job_interactions from "
        "20260722_0012_personal_mode_memory.py lines 75-118 "
        "(create_table + uq + index ix_user_job_interactions_user_created)"
    )
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

    # --- 4. user_preferences (20260722_0012_personal_mode_memory.py 30-72) ---
    op.execute(
        "-- downgrade: rebuilding user_preferences from "
        "20260722_0012_personal_mode_memory.py lines 30-72 "
        "(create_table + uq_user_preferences_user + index; PLUS 0013 lines 40-52 "
        "add_column role_synonyms/excluded_roles/personalized_discovery_min_score)"
    )
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
    # columns added by 20260725_0013 (lines 40-52)
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

    # --- 5. site_adapters (20260718_0009_site_adapters.py 21-56) ---
    op.execute(
        "-- downgrade: rebuilding site_adapters from "
        "20260718_0009_site_adapters.py lines 21-56 "
        "(create_table + uq_site_adapters_adapter_id; PLUS 0010 lines 19-35 "
        "add_column rollout_stage/last_success_at/last_readonly_verified_at)"
    )
    op.create_table(
        "site_adapters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("adapter_id", sa.String(64), nullable=False, unique=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("supported_domains", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "circuit_breaker_open",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # columns added by 20260718_0010 (lines 19-35)
    op.add_column(
        "site_adapters",
        sa.Column(
            "rollout_stage", sa.String(24), nullable=False, server_default="readonly"
        ),
    )
    op.add_column(
        "site_adapters",
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "site_adapters",
        sa.Column("last_readonly_verified_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 6. observed_sites (20260718_0010_multi_site_extension.py 49-76) ---
    op.execute(
        "-- downgrade: rebuilding observed_sites from "
        "20260718_0010_multi_site_extension.py lines 49-76 "
        "(create_table + uq_observed_sites_site_code)"
    )
    op.create_table(
        "observed_sites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("site_code", sa.String(32), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("page_samples_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="observing"
        ),
        sa.Column(
            "adapter_kind", sa.String(16), nullable=False, server_default="observation"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- 7. job_discovery_strategies (ffc4f5917966_add_strategy_and_trajectory_tables.py 21-51) ---
    op.execute(
        "-- downgrade: rebuilding job_discovery_strategies from "
        "ffc4f5917966_add_strategy_and_trajectory_tables.py lines 21-51 "
        "(create_table + 3 indexes)"
    )
    op.create_table('job_discovery_strategies',
    sa.Column('url_pattern', sa.String(length=500), nullable=False),
    sa.Column('site_type', sa.String(length=50), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('adapter', sa.String(length=500), nullable=True),
    sa.Column('plan_yaml', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('total_runs', sa.Integer(), nullable=False),
    sa.Column('success_runs', sa.Integer(), nullable=False),
    sa.Column('fallback_runs', sa.Integer(), nullable=False),
    sa.Column('error_count', sa.Integer(), nullable=False),
    sa.Column('consecutive_ok', sa.Integer(), nullable=False),
    sa.Column('last_error_tool', sa.String(length=100), nullable=True),
    sa.Column('last_error_reason', sa.String(length=50), nullable=True),
    sa.Column('last_error_message', sa.Text(), nullable=True),
    sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('success_count', sa.Integer(), nullable=False),
    sa.Column('avg_duration_s', sa.Float(), nullable=True),
    sa.Column('degradation_threshold', sa.Integer(), nullable=False),
    sa.Column('recovery_threshold', sa.Integer(), nullable=False),
    sa.Column('last_health_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_discovery_strategies_pattern', 'job_discovery_strategies', ['url_pattern'], unique=False)
    op.create_index(op.f('ix_job_discovery_strategies_site_type'), 'job_discovery_strategies', ['site_type'], unique=False)
    op.create_index('ix_job_discovery_strategies_status', 'job_discovery_strategies', ['status'], unique=False)

    # --- 8. job_discovery_tasks (7e8f22313271_add_job_discovery_tables.py 22-112) ---
    op.execute(
        "-- downgrade: rebuilding job_discovery_tasks from "
        "7e8f22313271_add_job_discovery_tables.py lines 22-112 "
        "(create_table + 2 indexes + uq; enums job_discovery_task_status/discovery_block_reason)"
    )
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

    # --- 9. job_discovery_trajectories (ffc4f5917966_add_strategy_and_trajectory_tables.py 52-76) ---
    op.execute(
        "-- downgrade: rebuilding job_discovery_trajectories from "
        "ffc4f5917966_add_strategy_and_trajectory_tables.py lines 52-76 "
        "(create_table incl. FKs strategy_id + task_id + 2 indexes)"
    )
    op.create_table('job_discovery_trajectories',
    sa.Column('task_id', sa.String(length=36), nullable=True),
    sa.Column('strategy_id', sa.String(length=36), nullable=True),
    sa.Column('executor_type', sa.String(length=20), nullable=False),
    sa.Column('overall_status', sa.String(length=30), nullable=False),
    sa.Column('failed_at_step', sa.Integer(), nullable=True),
    sa.Column('failed_tool', sa.String(length=100), nullable=True),
    sa.Column('failed_params', sa.JSON(), nullable=True),
    sa.Column('failed_error_type', sa.String(length=100), nullable=True),
    sa.Column('failed_error_message', sa.Text(), nullable=True),
    sa.Column('failed_error_reason', sa.String(length=50), nullable=True),
    sa.Column('completed_steps', sa.JSON(), nullable=True),
    sa.Column('fallback_trace', sa.JSON(), nullable=True),
    sa.Column('clean_path', sa.JSON(), nullable=True),
    sa.Column('annotations', sa.JSON(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('url_pattern', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['job_discovery_strategies.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['task_id'], ['job_discovery_tasks.id'], name='fk_job_discovery_trajectories_task_id', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_job_discovery_trajectories_created', 'job_discovery_trajectories', ['created_at'], unique=False)
    op.create_index('ix_job_discovery_trajectories_url_pattern', 'job_discovery_trajectories', ['url_pattern'], unique=False)

    # --- 10. discovered_job_candidates (7e8f22313271_add_job_discovery_tables.py 143-226) ---
    op.execute(
        "-- downgrade: rebuilding discovered_job_candidates from "
        "7e8f22313271_add_job_discovery_tables.py lines 143-226 "
        "(create_table + indexes)"
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

    # --- 11. job_discovery_evidence (7e8f22313271_add_job_discovery_tables.py 112-143) ---
    op.execute(
        "-- downgrade: rebuilding job_discovery_evidence from "
        "7e8f22313271_add_job_discovery_tables.py lines 112-143 "
        "(create_table + indexes)"
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

    # --- 12. personalized_discovery_runs (20260725_0013_personalized_job_discovery_v1.py 56-75) ---
    op.execute(
        "-- downgrade: rebuilding personalized_discovery_runs from "
        "20260725_0013_personalized_job_discovery_v1.py lines 56-75 "
        "(create_table + index ix_pdr_user_started)"
    )
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

    # --- 13. user_discovery_source_statuses (20260725_0013_personalized_job_discovery_v1.py 155-210) ---
    op.execute(
        "-- downgrade: rebuilding user_discovery_source_statuses from "
        "20260725_0013_personalized_job_discovery_v1.py lines 155-210 "
        "(create_table + uq_udss_user_run_task_reason + index)"
    )
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

    # --- 14. personalized_discovery_recommendations (20260725_0013_personalized_job_discovery_v1.py 82-155) ---
    op.execute(
        "-- downgrade: rebuilding personalized_discovery_recommendations from "
        "20260725_0013_personalized_job_discovery_v1.py lines 82-155 "
        "(create_table incl. FKs + uq + 2 indexes)"
    )
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

    # --- restore match_reports column (nullable: recovery-safe on non-empty tables) ---
    op.add_column(
        "match_reports",
        sa.Column("analysis_session_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_match_reports_analysis_session_id",
        "match_reports",
        ["analysis_session_id"],
        unique=False,
    )
    # Anonymous FK: MySQL auto-names it (same as 0008) - resolvable by the
    # information_schema lookup in upgrade().
    op.create_foreign_key(
        None,
        "match_reports",
        "analysis_sessions",
        ["analysis_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
