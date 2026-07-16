from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, delete, func, or_, select
from sqlalchemy.orm import Session

from backend.app.db.models import (
    AuditEvent,
    JobPosting,
    JobSource,
    JobSyncRun,
    JobSyncRunStatus,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories import jobs
from backend.app.services.job_sync import JobSyncService
from backend.app.services.tencent_smartsheet import TencentSmartsheetGateway
SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_TENCENT_DOCS_TOKEN"),
    reason="requires TEST_TENCENT_DOCS_TOKEN",
)


def _migrate_to_head(database_url: str) -> None:
    env = {
        **os.environ,
        "APP_AUTH_SECRET": "test-secret-with-at-least-32-characters",
        "DATABASE_URL": database_url,
        "OBJECT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "REDIS_URL": "redis://localhost:6379/15",
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _source_ids(db: Session) -> list[str]:
    return list(
        db.scalars(select(JobSource.id).where(JobSource.source_key.in_(SOURCE_KEYS)))
    )


def _delete_source_rows(db: Session, source_ids: list[str]) -> None:
    if not source_ids:
        return
    run_ids = list(
        db.scalars(select(JobSyncRun.id).where(JobSyncRun.source_id.in_(source_ids)))
    )
    if run_ids:
        db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(run_ids)))
    db.execute(delete(JobPosting).where(JobPosting.source_id.in_(source_ids)))
    db.execute(delete(RawJobRecord).where(RawJobRecord.source_id.in_(source_ids)))
    db.execute(delete(JobSyncRun).where(JobSyncRun.source_id.in_(source_ids)))
    db.execute(delete(JobSource).where(JobSource.id.in_(source_ids)))


def _count_for_source(db: Session, model: type[object], source_id: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(model).where(model.source_id == source_id)
        )
        or 0
    )


def test_live_tencent_sources_sync_read_only_and_idempotently(
    destructive_mysql_url: str,
) -> None:
    _migrate_to_head(destructive_mysql_url)
    engine = create_engine(destructive_mysql_url, pool_pre_ping=True)
    admin_id = str(uuid.uuid4())

    try:
        with Session(engine, expire_on_commit=False) as db:
            _delete_source_rows(db, _source_ids(db))
            admin = User(
                id=admin_id,
                account=f"task7-live-{uuid.uuid4().hex}",
                nickname="Task 7 Live Gate",
                password_hash="unused",
                role=UserRole.ADMIN,
            )
            db.add(admin)
            db.commit()

            gateway = TencentSmartsheetGateway(
                token=os.environ["TEST_TENCENT_DOCS_TOKEN"]
            )
            service = JobSyncService(gateway)
            first_outcomes = {}
            for source_key in SOURCE_KEYS:
                outcome = service.sync(
                    db, source_key=source_key, actor_user_id=admin.id
                )
                first_outcomes[source_key] = outcome
                assert outcome.status is JobSyncRunStatus.SUCCEEDED
                source = jobs.get_source(db, source_key)
                assert source is not None
                assert (
                    _count_for_source(db, RawJobRecord, source.id)
                    == outcome.records_read
                )

            first_source = jobs.get_source(db, SOURCE_KEYS[0])
            second_source = jobs.get_source(db, SOURCE_KEYS[1])
            assert first_source is not None
            assert second_source is not None
            assert _count_for_source(db, JobPosting, first_source.id) == 0
            assert 0 < _count_for_source(db, JobPosting, second_source.id) <= (
                _count_for_source(db, RawJobRecord, second_source.id)
            )

            for source_key in SOURCE_KEYS:
                repeated = service.sync(
                    db, source_key=source_key, actor_user_id=admin.id
                )
                assert repeated.status is JobSyncRunStatus.SUCCEEDED
                assert repeated.raw_snapshots_created == 0
                assert repeated.records_read == first_outcomes[source_key].records_read
    finally:
        with Session(engine) as db:
            source_ids = _source_ids(db)
            _delete_source_rows(db, source_ids)
            db.execute(
                delete(AuditEvent).where(
                    or_(
                        AuditEvent.actor_user_id == admin_id,
                        AuditEvent.entity_id.in_(
                            select(JobSyncRun.id).where(
                                JobSyncRun.source_id.in_(source_ids)
                            )
                        ),
                    )
                )
            )
            db.execute(delete(User).where(User.id == admin_id))
            db.commit()
        engine.dispose()
