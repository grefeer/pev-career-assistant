"""Task eligibility checks for creating/dispatching application tasks.

Shared across:
- ``ApplicationSnapshotService.create_application_task``
- ``assign_and_dispatch_task``
- Eligibility query API endpoint
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.db.models import (
    ApplicationSnapshot,
    ApprovedResumeAttachment,
    ApprovedResumeVersion,
    JobPosting,
)


def check_task_eligibility(
    db: Session, user_id: str, snapshot_id: str
) -> tuple[bool, str | None]:
    """Check whether an application task can be created for the given snapshot.

    Returns ``(can_create_task, reason_code)`` where *can_create_task* is
    ``True`` only when all eligibility gates pass, and *reason_code* is
    ``None`` on success or a stable string identifier on failure.

    Eligibility gates (in order):
        1.  Snapshot exists and belongs to the user (``not_found``)
        2.  Snapshot-level ``gui_eligible`` flag is ``True``
            (``snapshot_gui_not_eligible``)
        3.  Referenced job posting exists and is ``verified``
            (``snapshot_job_expired``)
        4.  Job-level ``gui_eligible`` flag is ``True``
            (``snapshot_gui_not_eligible``)
        5.  Job ``review_version`` matches the value stored in the snapshot
            (``snapshot_version_stale``)
        6.  Approved resume version still exists
            (``snapshot_version_stale``)
        7.  All attachments for the approved resume version have status
            ``"ready"`` (``snapshot_version_stale``)
    """
    # ── Gate 1: snapshot exists + ownership ────────────────────────────────
    snapshot = (
        db.query(ApplicationSnapshot)
        .filter(
            ApplicationSnapshot.id == snapshot_id,
            ApplicationSnapshot.user_id == user_id,
        )
        .first()
    )
    if snapshot is None:
        return False, "not_found"

    # ── Gate 2: snapshot gui_eligible ──────────────────────────────────────
    if not snapshot.gui_eligible:
        return False, "snapshot_gui_not_eligible"

    # ── Gate 3: current job state ──────────────────────────────────────────
    job = (
        db.query(JobPosting)
        .filter(JobPosting.id == snapshot.job_id)
        .first()
    )
    if job is None or job.status != "verified":
        return False, "snapshot_job_expired"

    # ── Gate 4: job gui_eligible ───────────────────────────────────────────
    if not job.gui_eligible:
        return False, "snapshot_gui_not_eligible"

    # ── Gate 5: review version match ───────────────────────────────────────
    if job.review_version != snapshot.job_review_version_at_snapshot:
        return False, "snapshot_version_stale"

    # ── Gate 6: approved resume version exists ─────────────────────────────
    arv = (
        db.query(ApprovedResumeVersion)
        .filter(ApprovedResumeVersion.id == snapshot.approved_resume_version_id)
        .first()
    )
    if arv is None:
        return False, "snapshot_version_stale"

    # ── Gate 7: all attachments ready ──────────────────────────────────────
    attachments = (
        db.query(ApprovedResumeAttachment)
        .filter(
            ApprovedResumeAttachment.approved_resume_version_id == arv.id,
        )
        .all()
    )
    if any(a.status != "ready" for a in attachments):
        return False, "snapshot_version_stale"

    # ── All gates passed ───────────────────────────────────────────────────
    return True, None
