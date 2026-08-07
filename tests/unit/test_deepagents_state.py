from __future__ import annotations

from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.state import build_initial_state


def test_initial_state_contains_every_channel() -> None:
    budgets = DeepAgentsBudgets(
        max_agent_turns=2, max_tool_calls=2, max_replans=1, max_wall_clock_seconds=60
    )
    state = build_initial_state(
        run_id="run-1",
        user_id="user-1",
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery", "job-matching"],
        context={"candidate_urls": ["https://example.com/jobs"]},
        budgets=budgets,
    )
    assert set(state) == {
        "run_id", "user_id", "goal", "allowed_skills", "context", "budget",
        "plan_json", "step_index", "retry_count", "stalled_decisions",
        "evidence_store", "decisions", "run_status", "error_code",
        "final_summary", "started_at", "finished_at",
    }
    assert state["run_status"] is None
    assert state["plan_json"] is None
    assert state["evidence_store"] == []
    assert state["budget"]["turns_used"] == 0
    assert state["started_at"] > 0.0  # epoch anchor set by the builder
