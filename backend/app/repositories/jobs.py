from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Literal

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Insert

from backend.app.db.models import (
    JobPosting,
    JobPostingStatus,
    JobSource,
    JobSourceProvider,
    JobSyncRun,
    JobSyncRunStatus,
    RawJobRecord,
)
from backend.app.services.job_mappers import BuiltinJobSource, NormalizedJobCandidate


LEASE_DURATION = timedelta(minutes=10)


class SyncConflictError(RuntimeError):
    error_code = "sync_conflict"


class SourceNotFoundError(LookupError):
    pass


class SourceDisabledError(RuntimeError):
    pass


class StaleSyncLeaseError(RuntimeError):
    pass


def ensure_builtin_sources(
    db: Session, definitions: Sequence[BuiltinJobSource]
) -> None:
    dialect_name = db.get_bind().dialect.name
    for definition in definitions:
        db.execute(
            _builtin_source_upsert_statement(
                definition,
                dialect_name=dialect_name,
            )
        )
        source = get_source(db, definition.source_key)
        if source is None:
            raise SourceNotFoundError(definition.source_key)
        source.name = definition.name
        source.file_id = definition.file_id
        source.sheet_id = definition.sheet_id
        source.mapper_version = definition.mapper_version
    db.flush()


def _builtin_source_upsert_statement(
    definition: BuiltinJobSource, *, dialect_name: str
) -> Insert:
    values = {
        "source_key": definition.source_key,
        "provider": JobSourceProvider.TENCENT_SMARTSHEET,
        "name": definition.name,
        "file_id": definition.file_id,
        "sheet_id": definition.sheet_id,
        "mapper_version": definition.mapper_version,
        "enabled": True,
    }
    if dialect_name == "mysql":
        statement = mysql_insert(JobSource).values(**values)
        return statement.on_duplicate_key_update(
            name=statement.inserted.name,
            file_id=statement.inserted.file_id,
            sheet_id=statement.inserted.sheet_id,
            mapper_version=statement.inserted.mapper_version,
        )
    if dialect_name == "sqlite":
        statement = sqlite_insert(JobSource).values(**values)
        return statement.on_conflict_do_update(
            index_elements=[JobSource.source_key],
            set_={
                "name": statement.excluded.name,
                "file_id": statement.excluded.file_id,
                "sheet_id": statement.excluded.sheet_id,
                "mapper_version": statement.excluded.mapper_version,
            },
        )
    raise NotImplementedError(
        f"atomic built-in source initialization is unsupported for {dialect_name}"
    )


def list_sources(db: Session) -> list[JobSource]:
    return list(db.scalars(select(JobSource).order_by(JobSource.source_key)))


def get_source(db: Session, source_key: str, *, lock: bool = False) -> JobSource | None:
    statement = select(JobSource).where(JobSource.source_key == source_key)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _comparable_datetime(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=timezone.utc)
    return value


def acquire_sync_run(db: Session, source_id: str, *, now: datetime) -> JobSyncRun:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None:
        raise SourceNotFoundError(source_id)
    if not source.enabled:
        raise SourceDisabledError(source.source_key)
    if (
        source.active_sync_run_id is not None
        and source.sync_lease_expires_at is not None
        and _comparable_datetime(source.sync_lease_expires_at, now) > now
    ):
        raise SyncConflictError(source.source_key)
    if source.active_sync_run_id is not None:
        expired = db.get(JobSyncRun, source.active_sync_run_id)
        if expired is not None and expired.status is JobSyncRunStatus.RUNNING:
            expired.status = JobSyncRunStatus.FAILED
            expired.error_code = "sync_lease_expired"
            expired.finished_at = now
    run = JobSyncRun(
        source_id=source.id,
        status=JobSyncRunStatus.RUNNING,
        started_at=now,
    )
    db.add(run)
    db.flush()
    source.active_sync_run_id = run.id
    source.sync_lease_expires_at = now + LEASE_DURATION
    db.flush()
    return run


def refresh_sync_lease(
    db: Session, source_id: str, run_id: str, *, now: datetime
) -> None:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None or source.active_sync_run_id != run_id:
        raise StaleSyncLeaseError(run_id)
    source.sync_lease_expires_at = now + LEASE_DURATION
    db.flush()


def finish_sync_run(
    db: Session,
    source_id: str,
    run_id: str,
    *,
    status: JobSyncRunStatus,
    now: datetime,
    error_code: str | None,
) -> JobSyncRun:
    source = db.scalar(
        select(JobSource).where(JobSource.id == source_id).with_for_update()
    )
    if source is None or source.active_sync_run_id != run_id:
        raise StaleSyncLeaseError(run_id)
    run = db.get(JobSyncRun, run_id)
    if run is None:
        raise StaleSyncLeaseError(run_id)
    run.status = status
    run.error_code = error_code
    run.finished_at = now
    source.active_sync_run_id = None
    source.sync_lease_expires_at = None
    if status is JobSyncRunStatus.SUCCEEDED:
        source.last_successful_sync_at = now
    db.flush()
    return run


def insert_raw_snapshot(
    db: Session,
    *,
    source_id: str,
    external_record_id: str,
    raw_fields: list[dict[str, Any]],
    payload_hash: str,
    source_updated_at: datetime | None,
    observed_at: datetime,
) -> tuple[RawJobRecord, bool]:
    existing = db.scalar(
        select(RawJobRecord).where(
            RawJobRecord.source_id == source_id,
            RawJobRecord.external_record_id == external_record_id,
            RawJobRecord.payload_hash == payload_hash,
        )
    )
    if existing is not None:
        return existing, False
    record = RawJobRecord(
        source_id=source_id,
        external_record_id=external_record_id,
        payload_hash=payload_hash,
        raw_fields=raw_fields,
        source_updated_at=source_updated_at,
        observed_at=observed_at,
    )
    db.add(record)
    db.flush()
    return record, True


def candidate_payload(candidate: NormalizedJobCandidate) -> dict[str, Any]:
    return {
        "company_name": candidate.company_name,
        "title": candidate.title,
        "locations": list(candidate.locations),
        "recruitment_types": list(candidate.recruitment_types),
        "industries": list(candidate.industries),
        "apply_url": candidate.apply_url,
        "referral_code": candidate.referral_code,
        "deadline_text": candidate.deadline_text,
    }


def upsert_posting(
    db: Session,
    *,
    source: JobSource,
    raw_record: RawJobRecord,
    candidate: NormalizedJobCandidate,
) -> tuple[JobPosting, Literal["created", "updated", "unchanged"]]:
    payload = candidate_payload(candidate)
    posting = db.scalar(
        select(JobPosting)
        .where(
            JobPosting.source_id == source.id,
            JobPosting.external_record_id == raw_record.external_record_id,
        )
        .with_for_update()
    )
    if (
        posting is not None
        and posting.raw_record_id == raw_record.id
        and posting.mapper_version == source.mapper_version
    ):
        if not posting.source_candidate:
            posting.source_candidate = payload
            db.flush()
        return posting, "unchanged"
    source_values: dict[str, Any] = {
        "raw_record_id": raw_record.id,
        "source_updated_at": candidate.source_updated_at,
        "mapper_version": source.mapper_version,
        "source_candidate": payload,
    }
    canonical_values: dict[str, Any] = {
        "company_name": candidate.company_name,
        "title": candidate.title,
        "locations": candidate.locations,
        "recruitment_types": candidate.recruitment_types,
        "industries": candidate.industries,
        "apply_url": candidate.apply_url,
        "referral_code": candidate.referral_code,
        "deadline_text": candidate.deadline_text,
    }
    if posting is None:
        posting = JobPosting(
            source_id=source.id,
            external_record_id=raw_record.external_record_id,
            status=JobPostingStatus.PENDING_COMPLETION,
            **source_values,
            **canonical_values,
        )
        db.add(posting)
        db.flush()
        return posting, "created"
    for name, value in source_values.items():
        setattr(posting, name, value)
    if (
        posting.status is JobPostingStatus.PENDING_COMPLETION
        and posting.review_version == 0
    ):
        for name, value in canonical_values.items():
            setattr(posting, name, value)
    else:
        posting.source_changed_since_review = True
    db.flush()
    return posting, "updated"


def list_postings(
    db: Session,
    *,
    limit: int,
    offset: int,
    source_key: str | None,
    company: str | None,
    recruitment_type: str | None,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    filters: list[Any] = [JobPosting.status == JobPostingStatus.PENDING_COMPLETION]
    if source_key:
        filters.append(JobSource.source_key == source_key)
    if company:
        escaped = company.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(JobPosting.company_name.like(f"%{escaped}%", escape="\\"))
    if recruitment_type:
        if db.get_bind().dialect.name == "mysql":
            filters.append(
                func.json_contains(
                    JobPosting.recruitment_types, json.dumps(recruitment_type)
                )
                == 1
            )
        else:
            labels = (
                func.json_each(JobPosting.recruitment_types)
                .table_valued("key", "value")
                .alias("recruitment_labels")
            )
            filters.append(
                exists(
                    select(1)
                    .select_from(labels)
                    .where(labels.c.value == recruitment_type)
                )
            )
    total = (
        db.scalar(
            select(func.count()).select_from(JobPosting).join(JobSource).where(*filters)
        )
        or 0
    )
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(*filters)
        .order_by(JobPosting.updated_at.desc(), JobPosting.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return int(total), [(posting, source) for posting, source in db.execute(statement)]


def get_posting(db: Session, job_id: str) -> tuple[JobPosting, JobSource] | None:
    row = db.execute(
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.PENDING_COMPLETION,
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None


def list_public_postings(
    db: Session,
    *,
    limit: int,
    offset: int,
    source_key: str | None,
    company: str | None,
    recruitment_type: str | None,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    filters: list[Any] = [JobPosting.status == JobPostingStatus.VERIFIED]
    if source_key:
        filters.append(JobSource.source_key == source_key)
    if company:
        escaped = company.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(JobPosting.company_name.like(f"%{escaped}%", escape="\\"))
    if recruitment_type:
        if db.get_bind().dialect.name == "mysql":
            filters.append(
                func.json_contains(
                    JobPosting.recruitment_types, json.dumps(recruitment_type)
                )
                == 1
            )
        else:
            labels = (
                func.json_each(JobPosting.recruitment_types)
                .table_valued("key", "value")
                .alias("recruitment_labels")
            )
            filters.append(
                exists(
                    select(1)
                    .select_from(labels)
                    .where(labels.c.value == recruitment_type)
                )
            )
    total = (
        db.scalar(
            select(func.count()).select_from(JobPosting).join(JobSource).where(*filters)
        )
        or 0
    )
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(*filters)
        .order_by(JobPosting.updated_at.desc(), JobPosting.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return int(total), [(posting, source) for posting, source in db.execute(statement)]


def get_public_posting(db: Session, job_id: str) -> tuple[JobPosting, JobSource] | None:
    row = db.execute(
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(
            JobPosting.id == job_id,
            JobPosting.status == JobPostingStatus.VERIFIED,
        )
    ).one_or_none()
    return (row[0], row[1]) if row is not None else None


def list_review_queue(
    db: Session,
    *,
    statuses: set[JobPostingStatus],
    limit: int,
    offset: int,
) -> tuple[int, list[tuple[JobPosting, JobSource]]]:
    allowed = {
        JobPostingStatus.PENDING_COMPLETION,
        JobPostingStatus.PENDING_REVIEW,
        JobPostingStatus.REJECTED,
    }
    invalid = statuses - allowed
    if invalid:
        invalid_values = ", ".join(sorted(status.value for status in invalid))
        raise ValueError(f"invalid review queue status: {invalid_values}")
    selected = statuses or allowed
    filters = [JobPosting.status.in_(selected)]
    total = db.scalar(select(func.count()).select_from(JobPosting).where(*filters)) or 0
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(*filters)
        .order_by(JobPosting.updated_at.asc(), JobPosting.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return int(total), [(posting, source) for posting, source in db.execute(statement)]


def get_posting_for_review(
    db: Session, job_id: str, *, lock: bool = False
) -> tuple[JobPosting, JobSource] | None:
    statement = (
        select(JobPosting, JobSource)
        .join(JobSource)
        .where(JobPosting.id == job_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    row = db.execute(statement).one_or_none()
    return (row[0], row[1]) if row is not None else None
