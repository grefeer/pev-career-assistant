"""MySQL sink: authoritative completion snapshots (spec §6.2).

Executed once per run completion (succeeded / failed / waiting_user /
cancelled).  Idempotent: the run row and each artifact row are upserted by
primary key so a retried flush never duplicates records.  Single
transaction per flush.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db.models import DeepAgentsArtifact, DeepAgentsRun


def _epoch_to_dt(value: float | None) -> datetime | None:
    if value is None:
        return None
    # naive UTC: the in-memory SQLite fixture rejects tz-aware datetimes
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)


def flush_run(
    session: Session,
    *,
    run_id: str,
    user_id: str,
    thread_id: str,
    goal: str,
    allowed_skills: list[str],
    budget_dict: dict[str, Any],
    status: str,
    plan_json: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    error_code: str | None,
    final_summary: str | None,
    started_at: float | None,
    finished_at: float | None,
    artifacts: list[dict[str, Any]],
) -> None:
    """Upsert the run snapshot and its artifacts in one transaction.

    ``status`` accepts a plain string or a ``RunStatus``; the column type
    coerces both.  ``started_at``/``finished_at`` are epoch floats (channel
    values are JSON-safe floats) and are converted here.
    """
    run = session.get(DeepAgentsRun, run_id)
    if run is None:
        run = DeepAgentsRun(id=run_id, thread_id=thread_id)
        session.add(run)
    run.user_id = user_id
    run.goal = goal
    run.allowed_skills_json = allowed_skills
    run.budget_json = budget_dict
    run.status = status
    run.plan_json = plan_json
    run.decisions_json = decisions
    run.error_code = error_code
    run.final_summary = final_summary
    run.started_at = _epoch_to_dt(started_at)
    run.finished_at = _epoch_to_dt(finished_at)

    for artifact in artifacts:
        artifact_id = artifact["artifact_id"]
        existing = session.execute(
            select(DeepAgentsArtifact).where(
                DeepAgentsArtifact.run_id == run_id,
                DeepAgentsArtifact.artifact_id == artifact_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = DeepAgentsArtifact(
                id=str(uuid.uuid4()), run_id=run_id, artifact_id=artifact_id
            )
            session.add(existing)
        existing.kind = artifact["kind"]
        existing.source_url = artifact.get("source_url")
        existing.content_hash = artifact["content_hash"]
        existing.payload_json = artifact["payload"]
    session.commit()


def flush_run_with_retry(
    session_factory: Callable[[], Session],
    *,
    retries: int = 3,
    backoff_seconds: float = 0.5,
    **run_fields: Any,
) -> None:
    """Flush with retry+backoff; raises the last error after exhaustion."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with session_factory() as session:
                flush_run(session, **run_fields)
            return
        except Exception as exc:  # noqa: BLE001 - external DB errors
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff_seconds * (2**attempt))
    if last_error is not None:
        raise last_error
