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
    JobSource,
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
        db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                event_type="job_sync.started",
                entity_type="job_sync_run",
                entity_id=run.id,
                correlation_id=correlation_id,
                redacted_payload={"source_key": source.source_key, "run_id": run.id},
            )
        )
        db.commit()
        mapper = MAPPERS[source_key]
        try:
            mapper.validate_schema(
                self.gateway.list_fields(source.file_id, source.sheet_id)
            )
            offset = 0
            expected_total: int | None = None
            while True:
                if run.pages_read >= MAX_PAGES:
                    raise TencentProtocolError("Tencent page limit exceeded")
                page = self.gateway.list_records(
                    source.file_id,
                    source.sheet_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                )
                if expected_total is None:
                    expected_total = page.total
                elif page.total != expected_total:
                    raise TencentProtocolError("Tencent total changed during sync")
                prospective_records_read = run.records_read + len(page.records)
                if prospective_records_read > MAX_RECORDS:
                    raise TencentProtocolError("Tencent record limit exceeded")
                if prospective_records_read > expected_total:
                    raise TencentProtocolError(
                        "Tencent records exceeded declared total"
                    )
                for record in page.records:
                    raw, created = jobs.insert_raw_snapshot(
                        db,
                        source_id=source.id,
                        external_record_id=record.record_id,
                        raw_fields=record.field_values,
                        payload_hash=canonical_payload_hash(record.field_values),
                        source_updated_at=mapper.source_updated_at(record),
                        observed_at=self.now(),
                    )
                    if created:
                        run.raw_snapshots_created += 1
                    mapped = mapper.map(record)
                    if isinstance(mapped, SkippedRecord):
                        run.records_skipped_incomplete += 1
                        continue
                    _posting, action = jobs.upsert_posting(
                        db,
                        source=source,
                        raw_record=raw,
                        candidate=mapped,
                    )
                    if action == "created":
                        run.postings_created += 1
                    elif action == "updated":
                        run.postings_updated += 1
                run.pages_read += 1
                run.records_read = prospective_records_read
                jobs.refresh_sync_lease(db, source.id, run.id, now=self.now())
                db.commit()
                if not page.has_more:
                    if run.records_read != expected_total:
                        raise TencentProtocolError(
                            "Tencent total did not match records read"
                        )
                    break
                offset = page.next_offset

            finished = jobs.finish_sync_run(
                db,
                source.id,
                run.id,
                status=JobSyncRunStatus.SUCCEEDED,
                now=self.now(),
                error_code=None,
            )
            db.add(
                AuditEvent(
                    actor_user_id=actor_user_id,
                    event_type="job_sync.finished",
                    entity_type="job_sync_run",
                    entity_id=run.id,
                    correlation_id=correlation_id,
                    redacted_payload={
                        "source_key": source.source_key,
                        "run_id": run.id,
                        "status": "succeeded",
                        "pages_read": run.pages_read,
                        "records_read": run.records_read,
                        "raw_snapshots_created": run.raw_snapshots_created,
                        "postings_created": run.postings_created,
                        "postings_updated": run.postings_updated,
                        "records_skipped_incomplete": (run.records_skipped_incomplete),
                    },
                )
            )
            db.commit()
            assert finished.finished_at is not None
            return SyncOutcome(
                run_id=finished.id,
                source_key=source.source_key,
                status=finished.status,
                pages_read=finished.pages_read,
                records_read=finished.records_read,
                raw_snapshots_created=finished.raw_snapshots_created,
                postings_created=finished.postings_created,
                postings_updated=finished.postings_updated,
                records_skipped_incomplete=finished.records_skipped_incomplete,
                started_at=finished.started_at,
                finished_at=finished.finished_at,
            )
        except (TencentGatewayError, SourceSchemaChangedError) as exc:
            self._finish_failure(
                db,
                source=source,
                run_id=run.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code=exc.error_code,
            )
            failed = db.get(JobSyncRun, run.id)
            assert failed is not None
            raise JobSyncFailedError(run.id, failed.status, exc.error_code) from None
        except SQLAlchemyError:
            self._finish_failure(
                db,
                source=source,
                run_id=run.id,
                actor_user_id=actor_user_id,
                correlation_id=correlation_id,
                error_code="database_write_failed",
            )
            failed = db.get(JobSyncRun, run.id)
            assert failed is not None
            raise JobSyncFailedError(
                run.id, failed.status, "database_write_failed"
            ) from None

    def _finish_failure(
        self,
        db: Session,
        *,
        source: JobSource,
        run_id: str,
        actor_user_id: str,
        correlation_id: str,
        error_code: str,
    ) -> None:
        db.rollback()
        run = db.get(JobSyncRun, run_id)
        if run is None:
            raise RuntimeError("authoritative sync run disappeared")
        status = (
            JobSyncRunStatus.PARTIAL if run.pages_read > 0 else JobSyncRunStatus.FAILED
        )
        jobs.finish_sync_run(
            db,
            source.id,
            run.id,
            status=status,
            now=self.now(),
            error_code=error_code,
        )
        db.add(
            AuditEvent(
                actor_user_id=actor_user_id,
                event_type="job_sync.finished",
                entity_type="job_sync_run",
                entity_id=run.id,
                correlation_id=correlation_id,
                redacted_payload={
                    "source_key": source.source_key,
                    "run_id": run.id,
                    "status": status.value,
                    "pages_read": run.pages_read,
                    "records_read": run.records_read,
                    "error_code": error_code,
                },
            )
        )
        db.commit()
