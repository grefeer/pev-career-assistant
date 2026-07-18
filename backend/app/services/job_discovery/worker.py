"""Job Discovery Worker — polls the task queue and processes discovery tasks.

Usage:
    worker = JobDiscoveryWorker(SessionLocal, get_settings())
    worker.run_once()        # process a single task
    worker.run_loop()        # poll forever
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import asdict
from typing import Any, Callable

from langchain_core.messages import HumanMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import Settings
from backend.app.db.models import (
    DiscoveryBlockReason,
    JobDiscoveryTask,
    RawJobRecord,
)
from backend.app.repositories.job_discovery import (
    claim_next_task,
    mark_task_failed,
    mark_task_needs_manual_review,
    mark_task_partial_success,
    mark_task_succeeded,
    upsert_candidate,
    upsert_evidence,
)
from backend.app.services.job_discovery.deepagents_runner import (
    build_discovery_supervisor_agent,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_worker_id() -> str:
    """Build a unique worker identifier from hostname and PID."""
    return f"{socket.gethostname()}::{os.getpid()}"


def _parse_agent_result(result_raw: dict[str, Any]) -> DiscoveryRunResult:
    """Parse a ``DiscoveryRunResult`` from the agent's invocation output.

    Tries, in order:
    1. ``structured_response`` key (deepagents ``response_format`` output).
    2. The dict itself if it carries a ``status`` key.
    3. Last message content parsed as JSON.
    4. Fallback to a failed result.
    """
    # Strategy 1 — structured_response from deepagents
    structured = result_raw.get("structured_response")
    if isinstance(structured, dict) and "status" in structured:
        return DiscoveryRunResult(**structured)

    # Strategy 2 — direct dict with status key
    if isinstance(result_raw, dict) and "status" in result_raw:
        return DiscoveryRunResult(**result_raw)

    # Strategy 3 — parse last message content as JSON
    messages = result_raw.get("messages", [])
    if messages:
        last = messages[-1]
        content: str | None = None
        if hasattr(last, "content") and last.content:
            content = last.content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "status" in parsed:
                    return DiscoveryRunResult(**parsed)
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

    return DiscoveryRunResult(
        status="failed",
        summary="Could not parse structured output from agent result",
    )


def _persist_evidence(
    db: Session,
    task: JobDiscoveryTask,
    evidence_list: list[dict[str, Any]] | list[PageEvidence],
) -> None:
    """Persist a list of evidence items for a task.

    Accepts either ``PageEvidence`` dataclass instances or plain dicts
    (as produced by the agent's structured output).
    """
    for ev in evidence_list:
        if isinstance(ev, dict):
            upsert_evidence(
                db,
                task_id=task.id,
                evidence_type=ev.get("evidence_type", ""),
                url=ev.get("url"),
                title=ev.get("title"),
                content_hash=ev.get("content_hash", ""),
                text_excerpt=ev.get("text_excerpt"),
                storage_uri=ev.get("storage_uri"),
                metadata_json=ev.get("metadata"),
            )
        else:
            upsert_evidence(
                db,
                task_id=task.id,
                evidence_type=ev.evidence_type,
                url=ev.url,
                title=ev.title,
                content_hash=ev.content_hash,
                text_excerpt=ev.text_excerpt,
                storage_uri=None,
                metadata_json=ev.metadata,
            )


def _persist_candidates(
    db: Session,
    task: JobDiscoveryTask,
    candidates_list: list[dict[str, Any]] | list[NormalizedJobCandidate],
) -> None:
    """Persist a list of candidates for a task.

    Accepts either ``NormalizedJobCandidate`` dataclass instances or plain
    dicts (as produced by the agent's structured output / ``package_candidates``).
    """
    for cand in candidates_list:
        if isinstance(cand, dict):
            upsert_candidate(
                db,
                task_id=task.id,
                source_id=cand.get("source_id", task.source_id),
                raw_record_id=cand.get("raw_record_id", task.raw_record_id),
                external_record_id=cand.get("external_record_id", task.external_record_id),
                idempotency_key=cand.get("idempotency_key", ""),
                similarity_group_key=cand.get("similarity_group_key", ""),
                title=cand.get("title"),
                company_name=cand.get("company_name"),
                department=cand.get("department"),
                description_text=cand.get("description_text"),
                responsibilities=cand.get("responsibilities"),
                requirements=cand.get("requirements"),
                locations_json=cand.get("locations"),
                recruitment_types_json=cand.get("recruitment_types"),
                industries_json=cand.get("industries"),
                apply_url=cand.get("apply_url"),
                application_channel_json=cand.get("application_channel_json"),
                deadline_text=cand.get("deadline_text"),
                referral_code=cand.get("referral_code"),
                confidence=cand.get("confidence"),
                evidence_refs_json=cand.get("evidence_refs"),
                normalization_warnings_json=cand.get("normalization_warnings"),
            )
        else:
            upsert_candidate(
                db,
                task_id=task.id,
                source_id=task.source_id,
                raw_record_id=task.raw_record_id,
                external_record_id=task.external_record_id,
                idempotency_key="",
                similarity_group_key="",
                title=cand.title,
                company_name=cand.company_name,
                department=cand.department,
                description_text=cand.description_text or None,
                responsibilities=cand.responsibilities or None,
                requirements=cand.requirements or None,
                locations_json=cand.locations if cand.locations else None,
                recruitment_types_json=cand.recruitment_types if cand.recruitment_types else None,
                industries_json=cand.industries if cand.industries else None,
                apply_url=cand.apply_url,
                application_channel_json=cand.application_channel_json,
                deadline_text=cand.deadline_text,
                referral_code=cand.referral_code,
                confidence=cand.confidence,
                evidence_refs_json=cand.evidence_refs if cand.evidence_refs else None,
                normalization_warnings_json=cand.normalization_warnings if cand.normalization_warnings else None,
            )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class JobDiscoveryWorker:
    """Polls the ``job_discovery_tasks`` queue and processes tasks via the agent.

    Each task is claimed with a lease, processed by the Discovery Supervisor
    Agent, and the result is persisted (evidence, candidates) before the
    lease is released.

    Parameters
    ----------
    db_factory:
        A callable that returns a new SQLAlchemy ``Session``.
    settings:
        Application settings (provides lease timeout, agent model, etc.).
    """

    def __init__(
        self,
        db_factory: Callable[[], Session],
        settings: Settings,
    ) -> None:
        self.db_factory = db_factory
        self.settings = settings
        self.worker_id = _build_worker_id()

    def run_once(self) -> int:
        """Claim and process one discovery task.

        Returns
        -------
        int
            ``1`` if a task was claimed and processed, ``0`` if the queue
            was empty.
        """
        db = self.db_factory()
        task: JobDiscoveryTask | None = None
        try:
            # 1. Claim a task from the queue
            task = claim_next_task(
                db,
                worker_id=self.worker_id,
                lease_seconds=self.settings.job_discovery_task_timeout_seconds,
            )
            if task is None:
                return 0

            # 2. Load the raw record for record_fields
            raw_record = db.scalar(
                select(RawJobRecord).where(RawJobRecord.id == task.raw_record_id)
            )
            record_fields: list[dict[str, Any]] = (
                raw_record.raw_fields if raw_record else []
            )

            # 3. Build task input
            task_input = DiscoveryTaskInput(
                source_id=task.source_id,
                raw_record_id=task.raw_record_id,
                external_record_id=task.external_record_id,
                source_key=task.source_key,
                source_url=task.source_url,
                url_hash=task.url_hash,
                record_fields=record_fields,
            )

            # 4. Build and run the agent
            agent = build_discovery_supervisor_agent(settings=self.settings)
            result_raw = agent.invoke({
                "messages": [
                    HumanMessage(
                        content=json.dumps(asdict(task_input), ensure_ascii=False)
                    )
                ]
            })

            # 5. Parse the result
            result = _parse_agent_result(result_raw)

            # 6. Persist evidence and candidates
            _persist_evidence(db, task, result.evidence)
            _persist_candidates(db, task, result.candidates)

            # 7. Mark task according to status
            summary_json: dict[str, Any] = {
                "summary": result.summary,
                "evidence_count": len(result.evidence),
                "candidate_count": len(result.candidates),
            }

            if result.status in ("succeeded", "partial_success"):
                if result.status == "succeeded":
                    mark_task_succeeded(db, task, result_summary_json=summary_json)
                else:
                    mark_task_partial_success(
                        db, task, result_summary_json=summary_json
                    )
            elif result.status == "needs_manual_review":
                block_reason = DiscoveryBlockReason.unknown
                if result.block_reason is not None:
                    try:
                        block_reason = DiscoveryBlockReason(result.block_reason)
                    except ValueError:
                        pass
                mark_task_needs_manual_review(
                    db,
                    task,
                    block_reason=block_reason,
                    result_summary_json=summary_json,
                )
            else:
                mark_task_failed(
                    db, task, last_error=result.summary or "Agent returned failed status"
                )

            db.commit()
            return 1

        except Exception as exc:
            if task is not None:
                try:
                    mark_task_failed(db, task, last_error=str(exc))
                    db.commit()
                except Exception:
                    db.rollback()
            return 0
        finally:
            db.close()

    def run_loop(self, *, poll_interval: float = 10.0) -> None:
        """Continuously poll and process tasks until interrupted.

        Parameters
        ----------
        poll_interval:
            Seconds to sleep between polls when the queue is empty.
        """
        try:
            while True:
                processed = self.run_once()
                if processed == 0:
                    time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass
