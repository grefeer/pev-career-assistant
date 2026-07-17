from __future__ import annotations

from pathlib import Path


def test_feedback_migration_uses_portable_non_native_enum_downgrade() -> None:
    migration = Path("alembic/versions/20260717_0007_job_feedback_loop.py").read_text(
        encoding="utf-8"
    )
    assert "sa.CheckConstraint" in migration
    assert "DROP TYPE" not in migration
    assert '"user_id", "job_id", "category"' in migration
    assert '"actor_user_id", "idempotency_key"' in migration
    assert '"job_feedback_events"' in migration
    assert 'sa.Column("status"' in migration
    assert 'sa.Column("version"' in migration
