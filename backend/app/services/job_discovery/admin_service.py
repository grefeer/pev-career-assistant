"""Admin-only job-discovery state transitions.

HTTP routes call this service so validation, promotion and auditing share one
transactional boundary instead of being duplicated beside request parsing.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.orm import Session

from backend.app.db.models import (
    AuditEvent,
    DiscoveredJobCandidate,
    DiscoveredJobCandidateStatus,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobPosting,
    JobPostingStatus,
    User,
)
from backend.app.repositories import job_discovery as repository


class JobDiscoveryAdminNotFoundError(LookupError):
    pass


class JobDiscoveryAdminConflictError(RuntimeError):
    pass


def _posting_external_record_id(candidate: DiscoveredJobCandidate) -> str:
    suffix = hashlib.sha256(candidate.idempotency_key.encode("utf-8")).hexdigest()[:16]
    return f"{candidate.external_record_id[:75]}::jd::{suffix}"


class JobDiscoveryAdminService:
    """Coordinates admin retry/review actions and their immutable audit records."""

    @staticmethod
    def list_tasks(
        db: Session, *, status: str | None = None,
    ) -> list[tuple[JobDiscoveryTask, str | None]]:
        return repository.list_tasks_with_source_name(db, status=status)

    @staticmethod
    def list_review_groups(db: Session) -> list[dict[str, object]]:
        return repository.list_review_groups(db)

    @staticmethod
    def source_name(db: Session, source_key: str) -> str | None:
        return repository.get_source_name(db, source_key)

    @staticmethod
    def _audit(db: Session, *, admin: User, action: str, entity_type: str, entity_id: str) -> None:
        repository.add_audit_event(db, AuditEvent(
            actor_user_id=admin.id,
            actor_device_id=None,
            event_type=f"job_discovery.{action}",
            entity_type=entity_type,
            entity_id=entity_id,
            correlation_id=str(uuid.uuid4()),
            redacted_payload={"action": action},
        ))

    def retry_task(self, db: Session, *, task_id: str, admin: User) -> JobDiscoveryTask:
        task = repository.get_task(db, task_id)
        if task is None:
            raise JobDiscoveryAdminNotFoundError("任务不存在。")
        if task.status is JobDiscoveryTaskStatus.running:
            raise JobDiscoveryAdminConflictError("任务正在运行，无法重试。")
        task.status = JobDiscoveryTaskStatus.queued
        task.attempt_count = 0
        task.last_error = None
        task.block_reason = None
        task.finished_at = None
        self._audit(db, admin=admin, action="retry", entity_type="job_discovery_task", entity_id=task.id)
        db.commit()
        db.refresh(task)
        return task

    def approve_candidate(
        self, db: Session, *, candidate_id: str, admin: User,
    ) -> DiscoveredJobCandidate:
        candidate = repository.get_candidate(db, candidate_id, lock=True)
        if candidate is None:
            raise JobDiscoveryAdminNotFoundError("候选记录不存在。")
        if candidate.status is not DiscoveredJobCandidateStatus.pending_review:
            raise JobDiscoveryAdminConflictError("候选记录状态不允许审批通过。")
        task = repository.get_task(db, candidate.task_id)
        if (
            task is None
            or task.status is not JobDiscoveryTaskStatus.succeeded
            or not bool((task.result_summary_json or {}).get("coverage_verified"))
            or not repository.has_durable_evidence(db, task.id)
        ):
            raise JobDiscoveryAdminConflictError("任务证据未完成或未持久化，不能审批发布。")

        candidate.status = DiscoveredJobCandidateStatus.approved
        external_record_id = _posting_external_record_id(candidate)
        posting = repository.get_posting_for_discovery_candidate(
            db, source_id=candidate.source_id, external_record_id=external_record_id,
        )
        if posting is None:
            posting = JobPosting(
                source_id=candidate.source_id,
                external_record_id=external_record_id,
                raw_record_id=candidate.raw_record_id,
                mapper_version="discovery-agent-v1",
                status=JobPostingStatus.PENDING_REVIEW,
                company_name=candidate.company_name or "",
                title=candidate.title or "",
                description_text=candidate.description_text,
                locations=candidate.locations_json or [],
                apply_url=candidate.apply_url or "",
            )
            db.add(posting)
        else:
            if candidate.title is not None:
                posting.title = candidate.title
            if candidate.company_name is not None:
                posting.company_name = candidate.company_name
            if candidate.description_text is not None:
                posting.description_text = candidate.description_text
            if candidate.locations_json is not None:
                posting.locations = candidate.locations_json
            if candidate.apply_url is not None:
                posting.apply_url = candidate.apply_url
            posting.status = JobPostingStatus.PENDING_REVIEW
        self._audit(db, admin=admin, action="approve", entity_type="discovered_job_candidate", entity_id=candidate.id)
        db.commit()
        db.refresh(candidate)
        return candidate

    def reject_candidate(
        self, db: Session, *, candidate_id: str, admin: User,
    ) -> DiscoveredJobCandidate:
        candidate = repository.get_candidate(db, candidate_id, lock=True)
        if candidate is None:
            raise JobDiscoveryAdminNotFoundError("候选记录不存在。")
        if candidate.status is not DiscoveredJobCandidateStatus.pending_review:
            raise JobDiscoveryAdminConflictError("候选记录状态不允许拒绝。")
        candidate.status = DiscoveredJobCandidateStatus.rejected
        self._audit(db, admin=admin, action="reject", entity_type="discovered_job_candidate", entity_id=candidate.id)
        db.commit()
        db.refresh(candidate)
        return candidate
