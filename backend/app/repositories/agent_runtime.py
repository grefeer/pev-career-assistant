"""SQL/ORM persistence for adaptive PEV runs.

Ownership, lifecycle transitions and agent decisions belong to the service layer.
These functions only read/write already-validated values and flush the current
transaction; callers decide when to commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
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


def get_run_for_owner(db: Session, run_id: str, user_id: str) -> AgentRun | None:
    """Fetch exactly one run only if it belongs to the requesting user."""
    return db.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.user_id == user_id)
    )


def start_run(db: Session, run: AgentRun) -> AgentRun:
    """Persist a service-approved transition into active execution."""
    run.status = RunStatus.running
    run.started_at = utc_now()
    run.state_version += 1
    db.flush()
    return run


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
    artifact = AgentArtifact(
        run_id=run_id,
        step_id=step_id,
        artifact_type="public_job_page",
        source_url=source_url,
        content_hash=content_hash,
        content_json=content_json,
        created_by=AgentRole.executor,
    )
    db.add(artifact)
    db.flush()
    db.refresh(artifact)
    return artifact


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
    artifact = AgentArtifact(
        run_id=run_id,
        step_id=step_id,
        artifact_type=artifact_type,
        source_url=source_url,
        content_hash=content_hash,
        content_json=content_json,
        created_by=AgentRole.executor,
    )
    db.add(artifact)
    db.flush()
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
        payload_json=payload_json,
    )
    db.add(event)
    db.flush()
    db.refresh(event)
    return event


def list_events(db: Session, run_id: str) -> list[AgentEvent]:
    """Return a stable chronological run trace."""
    return list(
        db.scalars(
            select(AgentEvent)
            .where(AgentEvent.run_id == run_id)
            .order_by(AgentEvent.sequence.asc(), AgentEvent.id.asc())
        )
    )
