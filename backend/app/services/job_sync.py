from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any
import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    AuditEvent,
    JobSyncRun,
    JobSyncRunStatus,
)
from backend.app.repositories import jobs
from backend.app.services.job_mappers import (
    BUILTIN_SOURCES,
    MAPPERS,
    SkippedRecord,
    SourceSchemaChangedError,
)
from backend.app.services.tencent_smartsheet import (
    MAX_PAGES,
    MAX_RECORDS,
    PAGE_SIZE,
    SmartsheetGateway,
    TencentGatewayError,
    TencentProtocolError,
)


@dataclass(frozen=True)
class SyncOutcome:
    run_id: str
    source_key: str
    status: JobSyncRunStatus
    pages_read: int
    records_read: int
    raw_snapshots_created: int
    postings_created: int
    postings_updated: int
    records_skipped_incomplete: int
    started_at: datetime
    finished_at: datetime


class JobSyncFailedError(RuntimeError):
    def __init__(self, run_id: str, status: JobSyncRunStatus, error_code: str):
        super().__init__(error_code)
        self.run_id = run_id
        self.status = status
        self.error_code = error_code


def canonical_payload_hash(field_values: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        field_values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class JobSyncService:
    def __init__(
        self,
        gateway: SmartsheetGateway,
        *,
        now: Callable[[], datetime] = utc_now,
        correlation_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.gateway = gateway
        self.now = now
        self.correlation_id_factory = correlation_id_factory

    def sync(self, db: Session, *, source_key: str, actor_user_id: str) -> SyncOutcome:
        if source_key not in MAPPERS:
            raise jobs.SourceNotFoundError(source_key)
        jobs.ensure_builtin_sources(db, BUILTIN_SOURCES)
        source = jobs.get_source(db, source_key)
        if source is None:
            db.rollback()
            raise jobs.SourceNotFoundError(source_key)
        run = jobs.acquire_sync_run(db, source.id, now=self.now())
        correlation_id = self.correlation_id_factory()
        source_id = source.id
        persisted_source_key = source.source_key
        file_id = source.file_id
        sheet_id = source.sheet_id
        run_id = run.id
        started_at = run.started_at
        db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                event_type="job_sync.started",
                entity_type="job_sync_run",
                entity_id=run_id,
                correlation_id=correlation_id,
                redacted_payload={
                    "source_key": persisted_source_key,
                    "run_id": run_id,
                },
            )
        )
        db.commit()
        mapper = MAPPERS[source_key]
        pages_read = 0
        records_read = 0
        raw_snapshots_created = 0
        postings_created = 0
        postings_updated = 0
        records_skipped_incomplete = 0
        try:
            mapper.validate_schema(self.gateway.list_fields(file_id, sheet_id))
            offset = 0
            expected_total: int | None = None
            while True:
                if pages_read >= MAX_PAGES:
                    raise TencentProtocolError("Tencent page limit exceeded")
                page = self.gateway.list_records(
                    file_id,
                    sheet_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                )
                if expected_total is None:
                    expected_total = page.total
                elif page.total != expected_total:
                    raise TencentProtocolError("Tencent total changed during sync")
                prospective_records_read = records_read + len(page.records)
                if prospective_records_read > MAX_RECORDS:
                    raise TencentProtocolError("Tencent record limit exceeded")
                if prospective_records_read > expected_total:
                    raise TencentProtocolError(
                        "Tencent records exceeded declared total"
                    )
                page_raw_snapshots_created = 0
                page_postings_created = 0
                page_postings_updated = 0
                page_records_skipped = 0
                for record in page.records:
                    raw, created = jobs.insert_raw_snapshot(
                        db,
                        source_id=source_id,
                        external_record_id=record.record_id,
                        raw_fields=record.field_values,
                        payload_hash=canonical_payload_hash(record.field_values),
                        source_updated_at=mapper.source_updated_at(record),
                        observed_at=self.now(),
                    )
                    if created:
                        page_raw_snapshots_created += 1
                    mapped = mapper.map(record)
                    if isinstance(mapped, SkippedRecord):
                        page_records_skipped += 1
                        continue
                    _posting, action = jobs.upsert_posting(
                        db,
                        source=source,
                        raw_record=raw,
                        candidate=mapped,
                    )
                    if action == "created":
                        page_postings_created += 1
                    elif action == "updated":
                        page_postings_updated += 1
                run = db.get(JobSyncRun, run_id)
                if run is None:
                    raise jobs.StaleSyncLeaseError(run_id)
                run.pages_read = pages_read + 1
                run.records_read = prospective_records_read
                run.raw_snapshots_created = (
                    raw_snapshots_created + page_raw_snapshots_created
                )
                run.postings_created = postings_created + page_postings_created
                run.postings_updated = postings_updated + page_postings_updated
                run.records_skipped_incomplete = (
                    records_skipped_incomplete + page_records_skipped
                )
                jobs.refresh_sync_lease(db, source_id, run_id, now=self.now())
                db.commit()
                pages_read += 1
                records_read = prospective_records_read
                raw_snapshots_created += page_raw_snapshots_created
                postings_created += page_postings_created
                postings_updated += page_postings_updated
                records_skipped_incomplete += page_records_skipped
                if not page.has_more:
                    if records_read != expected_total:
                        raise TencentProtocolError(
                            "Tencent total did not match records read"
                        )
                    break
                offset = page.next_offset

            finished = jobs.finish_sync_run(
                db,
                source_id,
                run_id,
                status=JobSyncRunStatus.SUCCEEDED,
                now=self.now(),
                error_code=None,
            )
            assert finished.finished_at is not None
            outcome = SyncOutcome(
                run_id=run_id,
                source_key=persisted_source_key,
                status=JobSyncRunStatus.SUCCEEDED,
                pages_read=pages_read,
                records_read=records_read,
                raw_snapshots_created=raw_snapshots_created,
                postings_created=postings_created,
                postings_updated=postings_updated,
                records_skipped_incomplete=records_skipped_incomplete,
                started_at=started_at,
                finished_at=finished.finished_at,
            )
            db.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="job_sync.finished",
                    entity_type="job_sync_run",
                    entity_id=run_id,
                    correlation_id=correlation_id,
                    redacted_payload={
                        "source_key": persisted_source_key,
                        "run_id": run_id,
                        "status": "succeeded",
                        "pages_read": pages_read,
                        "records_read": records_read,
                        "raw_snapshots_created": raw_snapshots_created,
                        "postings_created": postings_created,
                        "postings_updated": postings_updated,
                        "records_skipped_incomplete": records_skipped_incomplete,
                    },
                )
            )
            db.commit()
            return outcome
        except jobs.StaleSyncLeaseError:
            status, error_code = self._stale_failure(db, run_id=run_id)
            raise JobSyncFailedError(run_id, status, error_code) from None
        except (TencentGatewayError, SourceSchemaChangedError) as exc:
            status = self._finish_failure(
                db,
                source_id=source_id,
                source_key=persisted_source_key,
                run_id=run_id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code=exc.error_code,
                committed_pages=pages_read,
            )
            raise JobSyncFailedError(run_id, status, exc.error_code) from None
        except SQLAlchemyError:
            status = self._finish_failure(
                db,
                source_id=source_id,
                source_key=persisted_source_key,
                run_id=run_id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code="database_write_failed",
                committed_pages=pages_read,
            )
            raise JobSyncFailedError(run_id, status, "database_write_failed") from None
        except Exception:
            error_code = "job_sync_unexpected_error"
            status = self._finish_failure(
                db,
                source_id=source_id,
                source_key=persisted_source_key,
                run_id=run_id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code=error_code,
                committed_pages=pages_read,
            )
            raise JobSyncFailedError(run_id, status, error_code) from None

    def _finish_failure(
        self,
        db: Session,
        *,
        source_id: str,
        source_key: str,
        run_id: str,
        actor_user_id: str,
        correlation_id: str,
        error_code: str,
        committed_pages: int,
    ) -> JobSyncRunStatus:
        fallback_status = (
            JobSyncRunStatus.PARTIAL if committed_pages > 0 else JobSyncRunStatus.FAILED
        )
        self._rollback_safely(db)
        try:
            run = db.get(JobSyncRun, run_id)
            if run is None:
                return fallback_status
            if run.status is not JobSyncRunStatus.RUNNING:
                return run.status
            status = (
                JobSyncRunStatus.PARTIAL
                if run.pages_read > 0
                else JobSyncRunStatus.FAILED
            )
            pages_read = run.pages_read
            records_read = run.records_read
            jobs.finish_sync_run(
                db,
                source_id,
                run_id,
                status=status,
                now=self.now(),
                error_code=error_code,
            )
            db.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="job_sync.finished",
                    entity_type="job_sync_run",
                    entity_id=run_id,
                    correlation_id=correlation_id,
                    redacted_payload={
                        "source_key": source_key,
                        "run_id": run_id,
                        "status": status.value,
                        "pages_read": pages_read,
                        "records_read": records_read,
                        "error_code": error_code,
                    },
                )
            )
            db.commit()
            return status
        except Exception:
            self._rollback_safely(db)
            status, _error_code = self._read_failure_state(
                db,
                run_id=run_id,
                fallback_status=fallback_status,
                fallback_error_code=error_code,
            )
            return status

    def _stale_failure(
        self, db: Session, *, run_id: str
    ) -> tuple[JobSyncRunStatus, str]:
        self._rollback_safely(db)
        return self._read_failure_state(
            db,
            run_id=run_id,
            fallback_status=JobSyncRunStatus.FAILED,
            fallback_error_code="sync_lease_expired",
        )

    @staticmethod
    def _rollback_safely(db: Session) -> None:
        try:
            db.rollback()
        except Exception:
            pass

    @staticmethod
    def _read_failure_state(
        db: Session,
        *,
        run_id: str,
        fallback_status: JobSyncRunStatus,
        fallback_error_code: str,
    ) -> tuple[JobSyncRunStatus, str]:
        try:
            run = db.get(JobSyncRun, run_id)
            if run is not None and run.status is not JobSyncRunStatus.RUNNING:
                return run.status, run.error_code or fallback_error_code
        except Exception:
            pass
        return fallback_status, fallback_error_code
