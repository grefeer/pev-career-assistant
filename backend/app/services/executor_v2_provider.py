from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.api.executor_schemas import ExecutorTaskPayloadV2
from backend.app.db.models import ApplicationSnapshot, ApplicationTask

logger = logging.getLogger(__name__)


class SnapshotExecutorPayloadProvider:
    """Builds v2 executor payloads from ApplicationSnapshot data.

    Used when ``task.task_kind == "application"``.  Reads the persisted
    snapshot (frozen at creation time) and constructs a v2 payload that
    contains only non-sensitive fields, semantic local-sensitive references,
    and attachment IDs.

    The v2 payload NEVER includes:
      - object keys (S3/MinIO paths)
      - full profile snapshots
      - passwords, cookies, captcha
      - local-sensitive plaintext
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def payload_for(self, task: ApplicationTask) -> ExecutorTaskPayloadV2:
        """Build a v2 payload for an application task.

        Args:
            task: The ApplicationTask with ``task_kind == "application"``
                  and a non-null ``snapshot_id``.

        Returns:
            An ``ExecutorTaskPayloadV2`` with non-sensitive data only.

        Raises:
            ValueError: If the snapshot is missing, not found, or the
                        task is inconsistent.
        """
        snapshot_id = task.snapshot_id
        if not snapshot_id:
            raise ValueError(
                f"application task {task.id} has no snapshot_id"
            )

        snapshot = (
            self._db.query(ApplicationSnapshot)
            .filter(ApplicationSnapshot.id == snapshot_id)
            .first()
        )
        if snapshot is None:
            raise ValueError(
                f"snapshot {snapshot_id} not found for task {task.id}"
            )

        # Build non-sensitive fields -- these are the profile facts that
        # were validated as non-sensitive at snapshot creation time.
        non_sensitive_fields: dict[str, Any] = {}
        profile_facts = snapshot.profile_facts or {}
        for key, value in profile_facts.items():
            if not _is_local_sensitive_ref(key):
                non_sensitive_fields[key] = value

        # Build local-sensitive requirements -- semantic references only,
        # never plaintext values.
        local_sensitive_requirements: list[dict[str, Any]] = []
        for req in (snapshot.local_sensitive_requirements or []):
            local_sensitive_requirements.append({
                "field_key": req.get("field_key", ""),
                "category": req.get("category", ""),
                "local_reference": req.get("local_reference", ""),
            })

        # Attachment IDs -- pass through from the snapshot.
        attachment_ids: list[str] = list(snapshot.attachment_ids or [])

        # Target URL comes from the job snapshot's apply_url.
        job_snapshot = snapshot.job_snapshot or {}
        apply_url = job_snapshot.get("apply_url", "")
        if not apply_url:
            raise ValueError(
                f"job snapshot for task {task.id} has no apply_url"
            )

        return ExecutorTaskPayloadV2(
            task_id=task.id,
            state_version=task.state_version,
            snapshot_id=snapshot_id,
            target_url=apply_url,
            non_sensitive_fields=non_sensitive_fields,
            local_sensitive_requirements=local_sensitive_requirements,
            attachment_ids=attachment_ids,
        )


def _is_local_sensitive_ref(key: str) -> bool:
    """Rough check for local-sensitive fields based on naming pattern.

    The authoritative source is ``field_classification.ALLOWED_FIELDS``,
    but here we match the categories used in
    ``confirmed_profile_version.local_sensitive_references``.
    """
    local_keys = {
        "id_number", "family_members", "emergency_contact",
        "home_address", "passport_number", "bank_account",
        "political_status", "marital_status",
    }
    return key in local_keys
