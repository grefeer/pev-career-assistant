from __future__ import annotations

"""
Reconcile orphan attachments.

Scans for ApprovedResumeAttachment records in ``pending`` or ``failed`` status
that are not linked to an ApprovedResumeVersion, deletes the associated
encrypted objects from storage, and updates the records.

Usage:
    python scripts/reconcile_attachments.py
"""

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import boto3

from backend.app.db.models import ApprovedResumeAttachment
from backend.app.db.session import SessionLocal
from backend.app.services.storage import EncryptedObjectStore, S3BlobStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_object_store() -> EncryptedObjectStore | None:
    """Create an EncryptedObjectStore from environment variables."""
    endpoint = os.getenv("S3_ENDPOINT") or os.getenv("TEST_S3_ENDPOINT")
    access_key = os.getenv("S3_ACCESS_KEY") or os.getenv("TEST_S3_ACCESS_KEY")
    secret_key = os.getenv("S3_SECRET_KEY") or os.getenv("TEST_S3_SECRET_KEY")
    bucket = os.getenv("S3_BUCKET") or os.getenv("TEST_S3_BUCKET", "career-assistant-storage-test")
    enc_key = os.getenv("OBJECT_ENCRYPTION_KEY")

    if not all([endpoint, access_key, secret_key, enc_key]):
        logger.warning("S3 or encryption key not configured -- skipping storage cleanup")
        return None

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
    )
    blob_store = S3BlobStore(client, bucket)
    return EncryptedObjectStore(blob_store, enc_key)


def reconcile() -> int:
    """Reconcile orphan attachments. Returns count of records reconciled."""
    db = SessionLocal()
    object_store = _get_object_store()

    try:
        orphans = (
            db.query(ApprovedResumeAttachment)
            .filter(
                ApprovedResumeAttachment.status.in_(["pending", "failed"]),
                ApprovedResumeAttachment.approved_resume_version_id.is_(None),
            )
            .all()
        )

        logger.info("Found %d orphan attachment(s)", len(orphans))

        for att in orphans:
            # Delete the encrypted object from storage if we have access
            if object_store is not None:
                try:
                    object_store.delete(att.object_key)
                    logger.info("Deleted object %s", att.object_key)
                except Exception:
                    logger.warning("Could not delete object %s", att.object_key)

            # Mark as reconciled (set status to failed if pending)
            if att.status == "pending":
                att.status = "failed"
                logger.info("Marked attachment %s as failed (orphan)", att.id)

        db.commit()
        return len(orphans)
    finally:
        db.close()


def main() -> None:
    count = reconcile()
    logger.info("Reconciliation complete: %d attachment(s) processed", count)


if __name__ == "__main__":
    main()
