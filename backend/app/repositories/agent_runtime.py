"""SQL/ORM persistence for adaptive PEV runs.

Ownership, lifecycle transitions and agent decisions belong to the service layer.
These functions only read/write already-validated values and flush the current
transaction; callers decide when to commit.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db.base import utc_now
from backend.app.db.models import (
    AgentArtifact,
    AgentEvent,
    AgentPlan,
    AgentRun,
    AgentStep,
    AgentTurn,
)
from backend.app.domain.agent_runtime import (
    AgentRole,
    ComplexityLevel,
    RunStatus,
    StepStatus,
)

# Ceiling on the serialized size of a single event payload, in bytes. ``None``
# (the default in tests and any environment that never wires it) means events
# are persisted verbatim. When set, an oversize payload is replaced with a
# bounded stub so a runaway tool observation can never grow the event table or
# the SSE stream unboundedly. Configured once at startup by the application
# lifespan via ``set_event_payload_limit``; left as ``None`` in unit tests.
_EVENT_PAYLOAD_LIMIT: int | None = None


def set_event_payload_limit(limit: int | None) -> None:
    """Configure the byte ceiling for persisted event payloads (startup only)."""
    global _EVENT_PAYLOAD_LIMIT
    _EVENT_PAYLOAD_LIMIT = limit


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` unchanged, or a size-stub when it exceeds the ceiling."""
    if _EVENT_PAYLOAD_LIMIT is None:
        return payload
    serialized = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(serialized) <= _EVENT_PAYLOAD_LIMIT:
        return payload
    return {"_payload_truncated": True, "original_bytes": len(serialized)}


def create_run(
    db: Session,
    *,
    user_id: str,
    goal: str,
    allowed_skills: list[str],
    context_summary: dict[str, Any],
    budget_json: dict[str, Any],
    agent_version: str,
) -> AgentRun:
    """Insert a queued user-scoped run."""
    run = AgentRun(
        user_id=user_id,
        goal=goal,
        allowed_skills_json=allowed_skills,
        context_summary_json=context_summary,
        budget_json=budget_json,
        agent_version=agent_version,
    )
    db.add(run)
    db.flush()
    db.refresh(run)
    return run


def get_run_for_owner(
    db: Session,
    run_id: str,
    user_id: str,
    *,
    for_update: bool = False,
) -> AgentRun | None:
    """Fetch one owner-scoped run, optionally locking its lifecycle row.

    Resume/recover transitions must read and validate the status while holding
    the same row lock that protects the subsequent state mutation. SQLite
    ignores ``FOR UPDATE``; MySQL serializes concurrent callers here.
    """
    statement = select(AgentRun).where(
        AgentRun.id == run_id, AgentRun.user_id == user_id
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def list_runs_for_owner(db: Session, user_id: str, *, limit: int) -> list[AgentRun]:
    """Return one owner's recent runs without widening the ownership boundary."""
    return list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.user_id == user_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(limit)
        )
    )


def start_run(db: Session, run: AgentRun) -> AgentRun:
    """Persist a service-approved transition into active execution."""
    run.status = RunStatus.running
    run.started_at = utc_now()
    run.state_version += 1
    db.flush()
    return run


def append_user_response(db: Session, run: AgentRun, user_response: str) -> dict[str, Any]:
    """Append a bounded human reply to the Run's private task context."""
    context = dict(run.context_summary_json)
    responses = context.get("user_responses", [])
    safe_responses = list(responses) if isinstance(responses, list) else []
    safe_responses.append(user_response)
    context["user_responses"] = safe_responses[-10:]
    run.context_summary_json = context
    run.state_version += 1
    db.flush()
    return context


def set_run_complexity(
    db: Session, run: AgentRun, complexity: ComplexityLevel
) -> AgentRun:
    """Persist the Planner-selected operating level."""
    run.complexity = complexity
    db.flush()
    return run


def finish_run(
    db: Session,
    run: AgentRun,
    *,
    status: RunStatus,
    final_summary: str | None = None,
    error_code: str | None = None,
) -> AgentRun:
    """Persist a service-approved terminal or user-waiting run result."""
    run.status = status
    run.final_summary = final_summary
    run.error_code = error_code
    if status in {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled}:
        run.finished_at = utc_now()
    run.state_version += 1
    db.flush()
    return run


def create_plan(
    db: Session,
    *,
    run_id: str,
    revision: int,
    complexity: ComplexityLevel,
    plan_json: dict[str, Any],
) -> AgentPlan:
    """Store one versioned Planner output."""
    plan = AgentPlan(
        run_id=run_id,
        revision=revision,
        complexity=complexity,
        plan_json=plan_json,
        created_by=AgentRole.planner,
    )
    db.add(plan)
    db.flush()
    db.refresh(plan)
    return plan


def create_step(
    db: Session,
    *,
    run_id: str,
    plan_id: str,
    sequence: int,
    objective: str,
    allowed_skills: list[str],
) -> AgentStep:
    """Store an ordered, still-unexecuted plan step."""
    step = AgentStep(
        run_id=run_id,
        plan_id=plan_id,
        sequence=sequence,
        objective=objective,
        allowed_skills_json=allowed_skills,
    )
    db.add(step)
    db.flush()
    db.refresh(step)
    return step


def finish_step(
    db: Session,
    step: AgentStep,
    *,
    status: StepStatus,
    output_artifact_refs: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
) -> AgentStep:
    """Write a service-approved step outcome without deciding that outcome."""
    step.status = status
    step.output_artifact_refs_json = output_artifact_refs
    step.error_code = error_code
    db.flush()
    return step


def create_evidence_artifact(
    db: Session,
    *,
    run_id: str,
    step_id: str,
    source_url: str,
    content_hash: str,
    content_json: dict[str, Any],
) -> AgentArtifact:
    """Store immutable, public tool output once for the producing step."""
    return create_artifact(
        db,
        run_id=run_id,
        step_id=step_id,
        artifact_type="public_job_page",
        source_url=source_url,
        content_hash=content_hash,
        content_json=content_json,
    )


def create_artifact(
    db: Session,
    *,
    run_id: str,
    step_id: str,
    artifact_type: str,
    source_url: str,
    content_hash: str,
    content_json: dict[str, Any],
) -> AgentArtifact:
    """Store one immutable, schema-validated Skill result artifact."""
    existing = db.scalar(
        select(AgentArtifact).where(
            AgentArtifact.step_id == step_id,
            AgentArtifact.artifact_type == artifact_type,
            AgentArtifact.content_hash == content_hash,
        )
    )
    if existing is not None:
        return existing

    artifact = AgentArtifact(
        run_id=run_id,
        step_id=step_id,
        artifact_type=artifact_type,
        source_url=source_url,
        content_hash=content_hash,
        content_json=content_json,
        created_by=AgentRole.executor,
    )
    try:
        with db.begin_nested():
            db.add(artifact)
            db.flush()
    except IntegrityError:  # pragma: no cover - concurrent-insert race; the pre-check above makes this unreachable in single-threaded tests, the unique constraint enforces it in production.
        existing = db.scalar(
            select(AgentArtifact).where(
                AgentArtifact.step_id == step_id,
                AgentArtifact.artifact_type == artifact_type,
                AgentArtifact.content_hash == content_hash,
            )
        )
        if existing is not None:
            return existing
        raise
    db.refresh(artifact)
    return artifact


def list_evidence_artifacts(db: Session, run_id: str) -> list[AgentArtifact]:
    """Return a run's immutable public evidence in production order."""
    return list(
        db.scalars(
            select(AgentArtifact)
            .where(AgentArtifact.run_id == run_id)
            .order_by(AgentArtifact.created_at.asc(), AgentArtifact.id.asc())
        )
    )


def create_turn(
    db: Session,
    *,
    run_id: str,
    role: AgentRole,
    turn_index: int,
    decision_json: dict[str, Any],
    model_name: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    context_manifest: dict[str, Any] | None = None,
) -> AgentTurn:
    """Append a privacy-safe role decision summary."""
    turn = AgentTurn(
        run_id=run_id,
        role=role,
        turn_index=turn_index,
        decision_json=decision_json,
        model_name=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        context_manifest=context_manifest,
    )
    db.add(turn)
    db.flush()
    db.refresh(turn)
    return turn


def append_event(
    db: Session,
    *,
    run_id: str,
    event_type: str,
    payload_json: dict[str, Any],
) -> AgentEvent:
    """Append the next run-local event sequence number in this transaction."""
    # Locking the parent makes concurrent MySQL appenders serialize their
    # sequence allocation. SQLite ignores ``FOR UPDATE`` but remains adequate
    # for this single-session unit fixture.
    db.scalar(select(AgentRun.id).where(AgentRun.id == run_id).with_for_update())
    latest = db.scalar(
        select(func.max(AgentEvent.sequence)).where(AgentEvent.run_id == run_id)
    )
    event = AgentEvent(
        run_id=run_id,
        sequence=int(latest or 0) + 1,
        event_type=event_type,
        payload_json=_bounded_payload(payload_json),
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event


def list_events(
    db: Session, run_id: str, *, after_sequence: int = 0
) -> list[AgentEvent]:
    """Return durable events with ``sequence > after_sequence`` in chronological order.

    ``after_sequence`` defaults to 0 (all events, since sequences start at 1) so
    callers that want the whole trace are unaffected. The SSE poll loop passes
    its cursor so each poll returns only newly appended events instead of
    re-reading the whole run trace; ``ix_agent_events_run_sequence`` covers the
    ``(run_id, sequence)`` predicate.
    """
    return list(
        db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.run_id == run_id,
                AgentEvent.sequence > after_sequence,
            )
            .order_by(AgentEvent.sequence.asc(), AgentEvent.id.asc())
        )
    )


def count_plans(db: Session, run_id: str) -> int:
    """Count persisted revisions so a resumed Run keeps unique plan numbers."""
    return int(db.scalar(select(func.count()).where(AgentPlan.run_id == run_id)) or 0)


def list_plans(db: Session, run_id: str) -> list[AgentPlan]:
    """Return a Run's immutable Planner revisions in ascending order."""
    return list(
        db.scalars(
            select(AgentPlan)
            .where(AgentPlan.run_id == run_id)
            .order_by(AgentPlan.revision.asc(), AgentPlan.id.asc())
        )
    )


def count_turns(db: Session, run_id: str) -> int:
    """Count durable model decisions already consumed by one Run."""
    return int(db.scalar(select(func.count()).where(AgentTurn.run_id == run_id)) or 0)


def model_usage_totals(db: Session, run_id: str) -> tuple[int, int, int]:
    """Return durable model request count and measured input/output tokens."""
    rows = db.scalars(select(AgentTurn).where(AgentTurn.run_id == run_id)).all()
    return (
        len(rows),
        sum(turn.input_tokens or 0 for turn in rows),
        sum(turn.output_tokens or 0 for turn in rows),
    )


def count_tool_decisions(db: Session, run_id: str) -> int:
    """Count persisted Agent-selected tool calls without database-specific JSON SQL."""
    return sum(
        turn.decision_json.get("action") == "call_tool"
        for turn in db.scalars(select(AgentTurn).where(AgentTurn.run_id == run_id))
    )


def turn_indices_by_role(db: Session, run_id: str) -> dict[AgentRole, int]:
    """Return the latest durable sequence number for each PEV role."""
    indices = {role: 0 for role in AgentRole}
    for turn in db.scalars(select(AgentTurn).where(AgentTurn.run_id == run_id)):
        indices[turn.role] = max(indices[turn.role], turn.turn_index)
    return indices
