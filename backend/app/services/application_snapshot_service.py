"""ApplicationSnapshotService — creation of application snapshots and tasks.

Two operations in the dual-transaction pattern:
- ``create_snapshot``: validate inputs, insert in one short tx, idempotent
- ``create_application_task``: create a CREATED task under a snapshot, idempotent
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.models import (
    ApplicationSnapshot,
    ApplicationTask,
    ApplicationTaskStatus,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    JobPosting,
    ResumeDraft,
)
from backend.app.repositories import snapshots as snapshots_repo
from backend.app.services.idempotency import check_idempotency, compute_request_hash
from backend.app.services.snapshot_validators import (
    ApplicationSnapshotContent,
    validate_snapshot_content,
)
from backend.app.services.task_eligibility_service import check_task_eligibility

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = "1.0"

TASK_KIND_APPLICATION = "application"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_job_snapshot(job: JobPosting) -> dict[str, Any]:
    """Build a stable job-snapshot dict from a JobPosting row."""
    return {
        "id": job.id,
        "company_name": job.company_name,
        "title": job.title,
        "description_text": job.description_text,
        "locations": job.locations,
        "recruitment_types": job.recruitment_types,
        "industries": job.industries,
        "apply_url": job.apply_url,
    }


def _load_arv_or_raise(
    db: Session, user_id: str, arv_id: str
) -> ApprovedResumeVersion:
    """Load ApprovedResumeVersion and verify it belongs to ``user_id``.

    Raises ``ValueError`` with ``"approved_resume_version_not_found"`` if the
    version does not exist or is owned by a different user.
    """
    arv = (
        db.query(ApprovedResumeVersion)
        .filter(ApprovedResumeVersion.id == arv_id)
        .first()
    )
    if arv is None:
        raise ValueError("approved_resume_version_not_found")

    draft = (
        db.query(ResumeDraft)
        .filter(ResumeDraft.id == arv.draft_id)
        .first()
    )
    if draft is None or draft.user_id != user_id:
        raise ValueError("approved_resume_version_not_found")

    return arv


def _load_job_or_raise(db: Session, job_id: str) -> JobPosting:
    """Load JobPosting by id or raise ``ValueError``."""
    job = db.query(JobPosting).filter(JobPosting.id == job_id).first()
    if job is None:
        raise ValueError(f"job_not_found: {job_id}")
    return job


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_snapshot(
    db: Session,
    user_id: str,
    job_id: str,
    approved_resume_version_id: str,
    dynamic_answers: list[dict[str, Any]],
    local_sensitive_requirements: list[dict[str, Any]],
    idempotency_key: str,
) -> ApplicationSnapshot:
    """Create an application snapshot in a single short transaction.

    Steps:
        1. Compute request hash from all semantic inputs.
        2. Resolve idempotency (same key + hash -> return existing).
        3. Load and validate owned inputs (job, ARV, attachments).
        4. Build ``ApplicationSnapshotContent`` and run validators.
        5. Insert ``ApplicationSnapshot`` with creation-time fields
           (``gui_eligible``, ``job_status_at_snapshot``,
           ``job_review_version_at_snapshot``).
        6. On unique-constraint race: rollback, reload, return only if hash
           matches.

    Returns:
        The newly created (or existing) ``ApplicationSnapshot``.

    Raises:
        ValueError: On not-found inputs or idempotency-key conflict.
        SnapshotValidationError: On invalid snapshot content.
    """
    # ── 1. Compute request hash ───────────────────────────────────────────
    request_data: dict[str, Any] = {
        "job_id": job_id,
        "approved_resume_version_id": approved_resume_version_id,
        "dynamic_answers": dynamic_answers,
        "local_sensitive_requirements": local_sensitive_requirements,
    }
    request_hash = compute_request_hash(request_data)

    # ── 2. Idempotency check ─────────────────────────────────────────────
    existing, is_duplicate = check_idempotency(
        db, ApplicationSnapshot, user_id, idempotency_key, request_hash,
    )
    if is_duplicate:
        return existing

    # ── 3. Load owned inputs ─────────────────────────────────────────────
    job = _load_job_or_raise(db, job_id)
    arv = _load_arv_or_raise(db, user_id, approved_resume_version_id)

    attachment_records = (
        db.query(ApprovedResumeAttachment)
        .filter(
            ApprovedResumeAttachment.approved_resume_version_id
            == approved_resume_version_id,
        )
        .all()
    )
    attachment_ids = [a.id for a in attachment_records]

    # ── 4. Build content & validate ──────────────────────────────────────
    job_snapshot = _build_job_snapshot(job)
    content = ApplicationSnapshotContent(
        job_snapshot=job_snapshot,
        profile_facts=arv.approved_facts,
        dynamic_answers=dynamic_answers,
        local_sensitive_requirements=local_sensitive_requirements,
        attachment_ids=attachment_ids,
    )
    validate_snapshot_content(content)

    # ── 5. Insert in one short tx ────────────────────────────────────────
    try:
        snapshot = snapshots_repo.create(
            db=db,
            user_id=user_id,
            job_id=job_id,
            approved_resume_version_id=approved_resume_version_id,
            profile_version_id=arv.profile_version_id,
            job_snapshot=job_snapshot,
            profile_facts=arv.approved_facts,
            request_idempotency_key=idempotency_key,
            request_hash=request_hash,
            dynamic_answers=dynamic_answers,
            local_sensitive_requirements=local_sensitive_requirements,
            attachment_ids=attachment_ids,
            gui_eligible=job.gui_eligible,
            job_status_at_snapshot=job.status.value,
            job_review_version_at_snapshot=job.review_version,
            created_by=user_id,
            schema_version=SNAPSHOT_SCHEMA_VERSION,
        )
        db.commit()
        return snapshot

    # ── 6. Unique-constraint race ────────────────────────────────────────
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(ApplicationSnapshot)
            .filter(
                ApplicationSnapshot.user_id == user_id,
                ApplicationSnapshot.request_idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None and existing.request_hash == request_hash:
            return existing
        raise


def create_application_task(
    db: Session,
    user_id: str,
    snapshot_id: str,
    idempotency_key: str,
    device_id: str | None = None,
) -> ApplicationTask:
    """Create a new ``ApplicationTask`` in ``CREATED`` status (no dispatch).

    Steps:
        1. Load snapshot, verify ownership and eligibility.
        2. Check application-level idempotency (same user + key).
        3. Create ``ApplicationTask`` with ``task_kind="application"``.

    Returns:
        The newly created (or existing) ``ApplicationTask``.

    Raises:
        ValueError: On not-found snapshot, cross-user access,
            or eligibility failure.
    """
    # ── 1. Load snapshot + ownership ─────────────────────────────────────
    snapshot = (
        db.query(ApplicationSnapshot)
        .filter(
            ApplicationSnapshot.id == snapshot_id,
            ApplicationSnapshot.user_id == user_id,
        )
        .first()
    )
    if snapshot is None:
        raise ValueError("snapshot_not_found")

    # ── 2. Eligibility check ─────────────────────────────────────────────
    can_create, reason = check_task_eligibility(db, user_id, snapshot_id)
    if not can_create:
        raise ValueError(f"task_not_eligible: {reason}")

    # ── 3. Idempotency (application-level) ───────────────────────────────
    existing_task = (
        db.query(ApplicationTask)
        .filter(
            ApplicationTask.user_id == user_id,
            ApplicationTask.request_idempotency_key == idempotency_key,
        )
        .first()
    )
    if existing_task is not None:
        return existing_task

    # ── 4. Create task ───────────────────────────────────────────────────
    task = ApplicationTask(
        user_id=user_id,
        target_job_id=snapshot.job_id,
        snapshot_id=snapshot_id,
        device_id=device_id,
        status=ApplicationTaskStatus.CREATED,
        state_version=0,
        task_kind=TASK_KIND_APPLICATION,
        request_idempotency_key=idempotency_key,
    )
    db.add(task)
    db.commit()
    return task
