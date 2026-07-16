from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from pathlib import Path
import uuid

from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text


BUSINESS_TABLES = {
    "users",
    "analysis_sessions",
    "devices",
    "application_tasks",
    "application_events",
    "audit_events",
    "job_sources",
    "job_sync_runs",
    "raw_job_records",
    "job_postings",
    "job_verifications",
}
PROFILE_TABLES = {
    "profiles",
    "resume_assets",
    "resume_imports",
    "profile_field_evidence",
    "profile_field_decisions",
    "confirmed_profile_versions",
}
ALEMBIC_TABLES = {"alembic_version"}
HEAD_REVISION = "20260717_0005"
BUSINESS_TABLES |= PROFILE_TABLES


def _alembic_env(database_url: str) -> dict[str, str]:
    return {
        **os.environ,
        "APP_AUTH_SECRET": "test-secret-with-at-least-32-characters",
        "DATABASE_URL": database_url,
        "OBJECT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "REDIS_URL": "redis://localhost:6379/15",
    }


def _run_alembic(*args: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _column_sets(items: list[dict[str, object]]) -> set[tuple[str, ...]]:
    return {tuple(item["column_names"]) for item in items}


def test_alembic_offline_accepts_percent_encoded_database_url() -> None:
    env = _alembic_env("mysql+pymysql://migration_user:p%40ss@127.0.0.1/migration_test")
    _run_alembic("upgrade", "head", "--sql", env=env)


def test_alembic_online_accepts_percent_encoded_database_url(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "encoded%40.db").as_posix()
    env = _alembic_env(f"sqlite+pysqlite:///{database_path}")
    _run_alembic("current", env=env)


def test_mysql_migration_upgrade_and_downgrade(
    destructive_mysql_url: str,
) -> None:
    env = _alembic_env(destructive_mysql_url)
    engine = create_engine(env["DATABASE_URL"])

    try:
        _run_alembic("downgrade", "base", env=env)
        assert _tables(engine) <= ALEMBIC_TABLES
        assert _current_revision(engine) is None

        try:
            _run_alembic("upgrade", "20260715_0003", env=env)
            assert _current_revision(engine) == "20260715_0003"
            source_id = str(uuid.uuid4())
            raw_id = str(uuid.uuid4())
            posting_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            original_raw_fields = [
                {"field": "招聘岗位", "value": "迁移前来源岗位"},
                {"field": "投递链接", "value": "https://example.com/pre-0004"},
            ]
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO job_sources (
                            source_key, provider, name, file_id, sheet_id,
                            mapper_version, enabled, id, created_at, updated_at
                        ) VALUES (
                            :source_key, 'tencent_smartsheet', :name, :file_id,
                            :sheet_id, 'v1', 1, :id, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "source_key": f"migration-gate-{source_id}",
                        "name": "Migration Gate Source",
                        "file_id": f"file-{source_id}",
                        "sheet_id": f"sheet-{source_id}",
                        "id": source_id,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO raw_job_records (
                            source_id, external_record_id, payload_hash, raw_fields,
                            observed_at, id
                        ) VALUES (
                            :source_id, :external_record_id, :payload_hash,
                            :raw_fields, :observed_at, :id
                        )
                        """
                    ),
                    {
                        "source_id": source_id,
                        "external_record_id": "migration-record",
                        "payload_hash": "9" * 64,
                        "raw_fields": json.dumps(original_raw_fields, ensure_ascii=False),
                        "observed_at": now,
                        "id": raw_id,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO job_postings (
                            source_id, external_record_id, raw_record_id, status,
                            company_name, title, locations, recruitment_types,
                            industries, apply_url, referral_code, deadline_text,
                            mapper_version, id, created_at, updated_at
                        ) VALUES (
                            :source_id, 'migration-record', :raw_record_id,
                            'pending_completion', '迁移前公司', '迁移前岗位',
                            :locations, :recruitment_types, :industries,
                            'https://example.com/pre-0004', 'PRE-REFERRAL',
                            '2026-12-31', 'v1', :id, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "source_id": source_id,
                        "raw_record_id": raw_id,
                        "locations": json.dumps(["上海"], ensure_ascii=False),
                        "recruitment_types": json.dumps(["实习"], ensure_ascii=False),
                        "industries": json.dumps(["软件"], ensure_ascii=False),
                        "id": posting_id,
                        "created_at": now,
                        "updated_at": now,
                    },
                )

            _run_alembic("upgrade", "20260716_0004", env=env)
            assert _tables(engine) == BUSINESS_TABLES | ALEMBIC_TABLES
            assert _current_revision(engine) == HEAD_REVISION
            device_columns = {
                column["name"] for column in inspect(engine).get_columns("devices")
            }
            assert {"expires_at", "credential_rotated_at"} <= device_columns
            inspector = inspect(engine)
            job_columns = {
                column["name"]
                for column in inspector.get_columns("job_postings")
            }
            assert {"review_version", "source_candidate", "gui_eligible"} <= job_columns
            assert "job_verifications" in inspector.get_table_names()
            assert {
                ("source_key",),
                ("provider", "file_id", "sheet_id"),
            } <= _column_sets(inspector.get_unique_constraints("job_sources"))
            assert {
                ("source_id", "external_record_id", "payload_hash")
            } <= _column_sets(inspector.get_unique_constraints("raw_job_records"))
            assert {("source_id", "external_record_id")} <= _column_sets(
                inspector.get_unique_constraints("job_postings")
            )
            assert {("source_id", "started_at")} <= _column_sets(
                inspector.get_indexes("job_sync_runs")
            )
            assert {("source_id", "external_record_id")} <= _column_sets(
                inspector.get_indexes("raw_job_records")
            )
            assert {("status", "updated_at")} <= _column_sets(
                inspector.get_indexes("job_postings")
            )
            assert {("job_id", "created_at")} <= _column_sets(
                inspector.get_indexes("job_verifications")
            )
            with engine.connect() as connection:
                migrated = connection.execute(
                    text(
                        """
                        SELECT status, review_version, source_changed_since_review,
                               gui_eligible,
                               JSON_UNQUOTE(JSON_EXTRACT(source_candidate, '$.company_name')),
                               JSON_UNQUOTE(JSON_EXTRACT(source_candidate, '$.title')),
                               JSON_UNQUOTE(JSON_EXTRACT(source_candidate, '$.apply_url')),
                               JSON_UNQUOTE(JSON_EXTRACT(source_candidate, '$.locations[0]'))
                        FROM job_postings WHERE id = :posting_id
                        """
                    ),
                    {"posting_id": posting_id},
                ).one()
            assert migrated == (
                "pending_completion",
                0,
                0,
                0,
                "迁移前公司",
                "迁移前岗位",
                "https://example.com/pre-0004",
                "上海",
            )

            verification_id = str(uuid.uuid4())
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE job_postings
                        SET status = 'verified', review_version = 1,
                            description_text = '人工核验 JD', gui_eligible = 1,
                            verified_at = :verified_at, title = '人工核验岗位'
                        WHERE id = :posting_id
                        """
                    ),
                    {"verified_at": now, "posting_id": posting_id},
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO job_verifications (
                            job_id, actor_user_id, action, from_status, to_status,
                            review_version, field_snapshot, created_at, id
                        ) VALUES (
                            :job_id, NULL, 'verified', 'pending_review', 'verified',
                            1, :field_snapshot, :created_at, :id
                        )
                        """
                    ),
                    {
                        "job_id": posting_id,
                        "field_snapshot": json.dumps(
                            {"title": "人工核验岗位"}, ensure_ascii=False
                        ),
                        "created_at": now,
                        "id": verification_id,
                    },
                )

            _run_alembic("downgrade", "20260715_0003", env=env)
            assert _current_revision(engine) == "20260715_0003"
            downgraded_inspector = inspect(engine)
            assert "job_verifications" not in downgraded_inspector.get_table_names()
            assert {
                "description_text",
                "source_candidate",
                "source_changed_since_review",
                "gui_eligible",
                "review_version",
                "verified_at",
                "expired_at",
                "rejected_at",
            }.isdisjoint(
                {
                    column["name"]
                    for column in downgraded_inspector.get_columns("job_postings")
                }
            )
            with engine.connect() as connection:
                downgraded = connection.execute(
                    text(
                        """
                        SELECT status, title FROM job_postings WHERE id = :posting_id
                        """
                    ),
                    {"posting_id": posting_id},
                ).one()
                raw_fields = connection.execute(
                    text("SELECT raw_fields FROM raw_job_records WHERE id = :raw_id"),
                    {"raw_id": raw_id},
                ).scalar_one()
            assert downgraded == ("pending_completion", "人工核验岗位")
            decoded_raw_fields = (
                json.loads(raw_fields) if isinstance(raw_fields, str) else raw_fields
            )
            assert decoded_raw_fields == original_raw_fields
        finally:
            _run_alembic("downgrade", "base", env=env)
            assert _tables(engine) <= ALEMBIC_TABLES
            assert _current_revision(engine) is None
    finally:
        engine.dispose()
