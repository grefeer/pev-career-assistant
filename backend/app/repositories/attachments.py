from __future__ import annotations


from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from backend.app.db.models import ApprovedResumeAttachment


def reserve_or_reset_pending(
    db: Session,
    draft_id: str,
    user_id: str,
    format: str,
    object_key: str,
    content_type: str,
    encryption_version: str,
) -> ApprovedResumeAttachment:
    """Reserve or reset a pending attachment row for (draft_id, format).

    If a row already exists in a pending/failed state it is reset with the new
    values, preserving the deterministic object key for the format slot.
    Otherwise a new row is created.
    """
    existing = db.scalar(
        select(ApprovedResumeAttachment).where(
            ApprovedResumeAttachment.draft_id == draft_id,
            ApprovedResumeAttachment.format == format,
        )
    )
    if existing is not None:
        if existing.status in ("failed", "pending"):
            db.execute(
                sql_update(ApprovedResumeAttachment)
                .where(ApprovedResumeAttachment.id == existing.id)
                .values(
                    status="pending",
                    object_key=object_key,
                    content_type=content_type,
                    encryption_version=encryption_version,
                    plaintext_size=0,
                    approved_resume_version_id=None,
                    error_code=None,
                )
            )
            db.flush()
            refreshed = db.scalar(
                select(ApprovedResumeAttachment).where(
                    ApprovedResumeAttachment.id == existing.id
                )
            )
            assert refreshed is not None
            return refreshed
        return existing

    attachment = ApprovedResumeAttachment(
        draft_id=draft_id,
        user_id=user_id,
        format=format,
        object_key=object_key,
        content_type=content_type,
        plaintext_size=0,
        encryption_version=encryption_version,
        status="pending",
    )
    db.add(attachment)
    db.flush()
    return attachment


def mark_ready(
    db: Session,
    attachment_id: str,
    approved_version_id: str,
    plaintext_size: int,
) -> ApprovedResumeAttachment:
    """Mark an attachment as ready, linking it to an approved resume version."""
    db.execute(
        sql_update(ApprovedResumeAttachment)
        .where(ApprovedResumeAttachment.id == attachment_id)
        .values(
            status="ready",
            approved_resume_version_id=approved_version_id,
            plaintext_size=plaintext_size,
        )
    )
    db.flush()

    att = db.scalar(
        select(ApprovedResumeAttachment).where(
            ApprovedResumeAttachment.id == attachment_id
        )
    )
    assert att is not None, f"attachment {attachment_id} not found"
    return att


def mark_failed(
    db: Session,
    attachment_id: str,
    error_code: str,
) -> ApprovedResumeAttachment:
    """Mark an attachment as failed with an error code."""
    db.execute(
        sql_update(ApprovedResumeAttachment)
        .where(ApprovedResumeAttachment.id == attachment_id)
        .values(
            status="failed",
            error_code=error_code,
        )
    )
    db.flush()

    att = db.scalar(
        select(ApprovedResumeAttachment).where(
            ApprovedResumeAttachment.id == attachment_id
        )
    )
    assert att is not None, f"attachment {attachment_id} not found"
    return att


def get_by_draft(
    db: Session, draft_id: str, user_id: str
) -> list[ApprovedResumeAttachment]:
    """List attachments for a draft, scoped by user ownership."""
    return list(
        db.scalars(
            select(ApprovedResumeAttachment).where(
                ApprovedResumeAttachment.draft_id == draft_id,
                ApprovedResumeAttachment.user_id == user_id,
            )
        ).all()
    )
