"""SQL/ORM persistence for adaptive PEV runs.

Ownership, lifecycle transitions and agent decisions belong to the service layer.
These functions only read/write already-validated values and flush the current
transaction; callers decide when to commit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.db.models import AgentEvent, AgentPlan, AgentRun, AgentStep, AgentTurn
from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel


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
