from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    DiscoveryBlockReason,
    JobDiscoveryEvidence,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
)


def create_or_get_task(
    db: Session,
    *,
    source_id: str,
    raw_record_id: str,
    external_record_id: str,
    source_key: str,
    source_url: str,
    url_hash: str,
    payload_hash: str,
    idempotency_key: str,
    agent_version: str,
) -> tuple[JobDiscoveryTask, bool]:
    existing = db.scalar(
        select(JobDiscoveryTask).where(
            JobDiscoveryTask.source_id == source_id,
            JobDiscoveryTask.external_record_id == external_record_id,
            JobDiscoveryTask.url_hash == url_hash,
            JobDiscoveryTask.payload_hash == payload_hash,
            JobDiscoveryTask.agent_version == agent_version,
        )
    )
    if existing is not None:
        return existing, False
    task = JobDiscoveryTask(
        source_id=source_id,
        raw_record_id=raw_record_id,
        external_record_id=external_record_id,
        source_key=source_key,
        source_url=source_url,
        url_hash=url_hash,
        payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        agent_version=agent_version,
        status=JobDiscoveryTaskStatus.queued,
    )
    db.add(task)
    db.flush()
    return task, True


def claim_next_task(
    db: Session,
    *,
    worker_id: str,
    lease_seconds: int,
) -> JobDiscoveryTask | None:
    now = utc_now()
    task = db.scalar(
        select(JobDiscoveryTask)
        .where(
            JobDiscoveryTask.attempt_count < JobDiscoveryTask.max_attempts,
            (
                (
                    (JobDiscoveryTask.status == JobDiscoveryTaskStatus.queued)
                    & (
                        (JobDiscoveryTask.lease_expires_at < now)
                        | (JobDiscoveryTask.lease_expires_at.is_(None))
                    )
                )
                | (
                    (JobDiscoveryTask.status == JobDiscoveryTaskStatus.running)
                    & (JobDiscoveryTask.lease_expires_at < now)
                )
            ),
        )
        .execution_options(populate_existing=True)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if task is None:
        return None
    if task.status is JobDiscoveryTaskStatus.running:
        task.attempt_count = task.attempt_count + 1
        task.last_error = None
    task.status = JobDiscoveryTaskStatus.running
    task.lease_owner = worker_id
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    task.started_at = now
    db.flush()
    return task


def mark_task_running(db: Session, task: JobDiscoveryTask) -> None:
    now = utc_now()
    task.status = JobDiscoveryTaskStatus.running
    task.started_at = now
    db.flush()


def mark_task_succeeded(
    db: Session,
    task: JobDiscoveryTask,
    result_summary_json: dict[str, Any] | None,
) -> None:
    task.status = JobDiscoveryTaskStatus.succeeded
    task.finished_at = utc_now()
    task.result_summary_json = result_summary_json
    db.flush()


def mark_task_partial_success(
    db: Session,
    task: JobDiscoveryTask,
    result_summary_json: dict[str, Any] | None,
) -> None:
    task.status = JobDiscoveryTaskStatus.partial_success
    task.finished_at = utc_now()
    task.result_summary_json = result_summary_json
    db.flush()


def mark_task_needs_manual_review(
    db: Session,
    task: JobDiscoveryTask,
    block_reason: DiscoveryBlockReason,
    result_summary_json: dict[str, Any] | None = None,
) -> None:
    task.status = JobDiscoveryTaskStatus.needs_manual_review
    task.finished_at = utc_now()
    task.block_reason = block_reason
    task.result_summary_json = result_summary_json
    db.flush()


def mark_task_failed(
    db: Session,
    task: JobDiscoveryTask,
    last_error: str,
) -> None:
    task.status = JobDiscoveryTaskStatus.failed
    task.finished_at = utc_now()
    task.last_error = last_error
    task.attempt_count = task.attempt_count + 1
    db.flush()


def upsert_evidence(
    db: Session,
    *,
    task_id: str,
    evidence_type: str,
    url: str | None,
    title: str | None,
    content_hash: str,
    text_excerpt: str | None,
    storage_uri: str | None,
    metadata_json: dict[str, Any] | None,
) -> JobDiscoveryEvidence:
    existing = db.scalar(
        select(JobDiscoveryEvidence).where(
            JobDiscoveryEvidence.task_id == task_id,
            JobDiscoveryEvidence.evidence_type == evidence_type,
            JobDiscoveryEvidence.content_hash == content_hash,
        )
    )
    if existing is not None:
        return existing
    evidence = JobDiscoveryEvidence(
        task_id=task_id,
        evidence_type=evidence_type,
        url=url,
        title=title,
        content_hash=content_hash,
        text_excerpt=text_excerpt,
        storage_uri=storage_uri,
        metadata_json=metadata_json,
    )
    db.add(evidence)
    db.flush()
    return evidence


def upsert_candidate(
    db: Session,
    *,
    task_id: str,
    source_id: str,
    raw_record_id: str,
    external_record_id: str,
    idempotency_key: str,
    similarity_group_key: str,
    title: str | None,
    company_name: str | None,
    department: str | None,
    description_text: str | None,
    responsibilities: str | None,
    requirements: str | None,
    locations_json: list[str] | None,
    recruitment_types_json: list[str] | None,
    industries_json: list[str] | None,
    apply_url: str | None,
    application_channel_json: dict[str, Any] | None,
    deadline_text: str | None,
    referral_code: str | None,
    confidence: float | None,
    evidence_refs_json: list[dict[str, Any]] | None,
    normalization_warnings_json: list[str] | None,
) -> DiscoveredJobCandidate:
    existing = db.scalar(
        select(DiscoveredJobCandidate).where(
            DiscoveredJobCandidate.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing
    candidate = DiscoveredJobCandidate(
        task_id=task_id,
        source_id=source_id,
        raw_record_id=raw_record_id,
        external_record_id=external_record_id,
        idempotency_key=idempotency_key,
        similarity_group_key=similarity_group_key,
        title=title,
        company_name=company_name,
        department=department,
        description_text=description_text,
        responsibilities=responsibilities,
        requirements=requirements,
        locations_json=locations_json,
        recruitment_types_json=recruitment_types_json,
        industries_json=industries_json,
        apply_url=apply_url,
        application_channel_json=application_channel_json,
        deadline_text=deadline_text,
        referral_code=referral_code,
        confidence=confidence,
        evidence_refs_json=evidence_refs_json,
        normalization_warnings_json=normalization_warnings_json,
    )
    db.add(candidate)
    db.flush()
    return candidate


def list_review_groups(
    db: Session,
) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(DiscoveredJobCandidate)
        .where(
            DiscoveredJobCandidate.status
            == DiscoveredJobCandidateStatus.pending_review,
        )
        .order_by(
            DiscoveredJobCandidate.similarity_group_key,
            DiscoveredJobCandidate.created_at,
        )
    ).all()

    groups: dict[str, list[DiscoveredJobCandidate]] = {}
    for row in rows:
        key = row.similarity_group_key
        if key not in groups:
            groups[key] = []
        groups[key].append(row)

    return [
        {
            "similarity_group_key": key,
            "candidates": candidates,
        }
        for key, candidates in sorted(groups.items())
    ]
