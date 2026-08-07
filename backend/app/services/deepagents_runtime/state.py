"""Channel state of the external DeepAgents PEV harness graph.

Every value must be JSON-serializable: LangGraph persists channel values
to the checkpointer, so budget counters survive resume and are never
reset (only the wall-clock window refreshes, per CLAUDE.md).
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, TypedDict

from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets


class DeepAgentsState(TypedDict):
    run_id: str
    user_id: str
    goal: str
    allowed_skills: list[str]
    context: dict[str, Any]
    budget: dict[str, Any]  # DeepAgentsBudgets.to_dict() payload
    plan_json: dict[str, Any] | None  # ExecutionPlan.model_dump(mode="json")
    step_index: int
    retry_count: int  # consecutive RETRY_EXECUTOR decisions on the current step
    stalled_decisions: int  # consecutive no-progress executor decisions
    evidence_store: Annotated[list[dict[str, Any]], operator.add]
    decisions: Annotated[list[dict[str, Any]], operator.add]
    run_status: str | None
    error_code: str | None
    final_summary: str | None
    started_at: float  # epoch seconds, set by the orchestrator
    finished_at: float | None


def build_initial_state(
    *,
    run_id: str,
    user_id: str,
    goal: str,
    allowed_skills: list[str],
    context: dict[str, Any],
    budgets: DeepAgentsBudgets,
) -> DeepAgentsState:
    """Build the complete first-state for a fresh run (every channel present)."""
    return {
        "run_id": run_id,
        "user_id": user_id,
        "goal": goal,
        "allowed_skills": list(allowed_skills),
        "context": context,
        "budget": budgets.to_dict(),
        "plan_json": None,
        "step_index": 0,
        "retry_count": 0,
        "stalled_decisions": 0,
        "evidence_store": [],
        "decisions": [],
        "run_status": None,
        "error_code": None,
        "final_summary": None,
        "started_at": time.time(),
        "finished_at": None,
    }
