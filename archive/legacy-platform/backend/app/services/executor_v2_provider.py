from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from backend.app.api.executor_schemas import AdapterRef, ExecutorTaskPayloadV2
from backend.app.db.models import ApplicationSnapshot, ApplicationTask, SiteAdapter

logger = logging.getLogger(__name__)

EXECUTOR_MIN_VERSION = "0.1.0"


class ExecutorPayloadUnavailableError(RuntimeError):
    """Raised when a v2 payload cannot be generated for a task."""


class SnapshotExecutorPayloadProvider:
    """Builds v2 executor payloads from ApplicationSnapshot data.

    Used when ``task.task_kind == "application"``.  Reads the persisted
    snapshot (frozen at creation time) and constructs a v2 payload that
    contains only non-sensitive fields, semantic local-sensitive references,
    attachment IDs, and the resolved AdapterRef.

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

        Resolves the site adapter from the job's apply_url domain and
        injects an AdapterRef into the payload.  Raises
        ExecutorPayloadUnavailableError if no active adapter matches.

        Args:
            task: The ApplicationTask with ``task_kind == "application"``
                  and a non-null ``snapshot_id``.

        Returns:
            An ``ExecutorTaskPayloadV2`` with non-sensitive data and AdapterRef.

        Raises:
            ValueError: If the snapshot is missing or inconsistent.
            ExecutorPayloadUnavailableError: If no active adapter matches
                the apply_url domain.
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

        # Build non-sensitive fields
        non_sensitive_fields: dict[str, Any] = {}
        profile_facts = snapshot.profile_facts or {}
        for key, value in profile_facts.items():
            if not _is_local_sensitive_ref(key):
                non_sensitive_fields[key] = value

        # Build local-sensitive requirements
        local_sensitive_requirements: list[dict[str, Any]] = []
        for req in (snapshot.local_sensitive_requirements or []):
            local_sensitive_requirements.append({
                "field_key": req.get("field_key", ""),
                "category": req.get("category", ""),
                "local_reference": req.get("local_reference", ""),
            })

        # Attachment IDs
        attachment_ids: list[str] = list(snapshot.attachment_ids or [])

        # Target URL from job snapshot
        job_snapshot = snapshot.job_snapshot or {}
        apply_url = job_snapshot.get("apply_url", "")
        if not apply_url:
            raise ValueError(
                f"job snapshot for task {task.id} has no apply_url"
            )

        # Resolve adapter from the adapter frozen at dispatch time when
        # available; fall back to domain matching for older tasks.
        adapter_ref = _resolve_adapter(self._db, apply_url, task=task)
        if adapter_ref is None:
            raise ExecutorPayloadUnavailableError(
                f"no active site adapter for apply_url: {apply_url}"
            )

        return ExecutorTaskPayloadV2(
            task_id=task.id,
            state_version=task.state_version,
            snapshot_id=snapshot_id,
            target_url=apply_url,
            non_sensitive_fields=non_sensitive_fields,
            local_sensitive_requirements=local_sensitive_requirements,
            attachment_ids=attachment_ids,
            adapter=adapter_ref,
        )


def _resolve_adapter(
    db: Session, apply_url: str, *, task: ApplicationTask | None = None
) -> AdapterRef | None:
    """Find an active site adapter whose supported_domains match the apply_url.

    Returns None if no matching active adapter is found or if the matched
    adapter has its circuit breaker open.
    """
    from urllib.parse import urlparse

    hostname = urlparse(apply_url).hostname or ""
    if not hostname:
        return None

    if task is not None and task.adapter_id and task.adapter_version:
        adapter = (
            db.query(SiteAdapter)
            .filter(SiteAdapter.adapter_id == task.adapter_id)
            .filter(SiteAdapter.version == task.adapter_version)
            .filter(SiteAdapter.status == "active")
            .filter(SiteAdapter.circuit_breaker_open == False)  # noqa: E712
            .first()
        )
        if adapter is None:
            return None
        domains = adapter.supported_domains or []
        if any(_hostname_matches(hostname, d) for d in domains):
            return AdapterRef(
                adapter_id=adapter.adapter_id,
                version=adapter.version,
                min_engine_version=EXECUTOR_MIN_VERSION,
            )
        return None

    # Query all active adapters
    adapters = (
        db.query(SiteAdapter)
        .filter(SiteAdapter.status == "active")
        .filter(SiteAdapter.circuit_breaker_open == False)  # noqa: E712
        .all()
    )

    for adapter in adapters:
        domains = adapter.supported_domains or []
        if any(_hostname_matches(hostname, d) for d in domains):
            return AdapterRef(
                adapter_id=adapter.adapter_id,
                version=adapter.version,
                min_engine_version=EXECUTOR_MIN_VERSION,
            )

    return None


def _hostname_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


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
