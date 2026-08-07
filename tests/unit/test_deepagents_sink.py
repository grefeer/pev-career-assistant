from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from backend.app.db.models import DeepAgentsArtifact, DeepAgentsRun, User
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.checkpoints.sink import (
    flush_run,
    flush_run_with_retry,
)
from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness
from tests.unit.deepagents_testkit import ScriptedModel

_RUN = dict(
    run_id="run-1",
    user_id="user-1",
    thread_id="run-1",
    goal="帮我找后端岗位",
    allowed_skills=["job-discovery"],
    budget_dict={"max_agent_turns": 12},
    status="succeeded",
    plan_json={"steps": []},
    decisions=[{"role": "planner", "decision": "PLANNED"}],
    error_code=None,
    final_summary="ok",
    started_at=1723000000.0,
    finished_at=1723000060.0,
)

_ARTIFACTS = [
    {
        "artifact_id": "abc123",
        "kind": "public_page_evidence",
        "source_url": "https://example.com/jobs",
        "content_hash": "abc123",
        "payload": {"text": "excerpt"},
    }
]


def _seed_user(db_session, user_id: str) -> None:
    """Create the FK target for ``deepagents_runs.user_id``.

    The ``db_session`` fixture enforces SQLite foreign keys, so a flush of a
    run row requires the referenced ``users`` row to exist first.
    """
    db_session.add(
        User(
            id=user_id,
            account=f"{user_id}@sink.test",
            nickname=user_id,
            password_hash="not-a-real-password-hash",
        )
    )
    db_session.commit()


def test_flush_run_inserts_run_and_artifacts(db_session) -> None:
    _seed_user(db_session, "user-1")
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    assert run.status.value == "succeeded"
    # naive UTC: the in-memory SQLite fixture rejects tz-aware datetimes
    assert run.started_at is not None and run.started_at.tzinfo is None
    assert run.finished_at is not None and run.finished_at.tzinfo is None
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-1").all()
    assert len(artifacts) == 1
    assert artifacts[0].artifact_id == "abc123"


def test_flush_run_is_idempotent_upsert(db_session) -> None:
    _seed_user(db_session, "user-1")
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)
    flush_run(db_session, artifacts=_ARTIFACTS, **_RUN)  # second flush must not duplicate
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    assert run.status.value == "succeeded"
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-1").all()
    assert len(artifacts) == 1


def test_flush_run_with_retry_retries_transient_failures(db_session) -> None:
    _seed_user(db_session, "user-1")
    attempts = {"n": 0}

    def flaky_factory():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return db_session

    flush_run_with_retry(
        flaky_factory, retries=3, backoff_seconds=0, **_RUN, artifacts=_ARTIFACTS
    )
    assert attempts["n"] == 2
    assert db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").count() == 1


def test_flush_run_with_retry_gives_up_and_raises(db_session) -> None:
    def always_failing():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        flush_run_with_retry(
            always_failing, retries=2, backoff_seconds=0, **_RUN, artifacts=[]
        )


def test_flush_run_with_retry_zero_retries_is_noop(db_session) -> None:
    # retries=0: the loop never runs and the flush is skipped (defensive
    # last-error guard's fall-through branch, required for 100% coverage)
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return db_session

    flush_run_with_retry(factory, retries=0, backoff_seconds=0, **_RUN, artifacts=[])
    assert calls["n"] == 0
    assert db_session.query(DeepAgentsRun).count() == 0


def test_flush_run_accepts_none_timestamps(db_session) -> None:
    # interrupted runs flush with no timestamps -> NULL row fields
    _seed_user(db_session, "user-1")
    run_fields = {**_RUN, "started_at": None, "finished_at": None}
    flush_run(db_session, artifacts=[], **run_fields)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-1").one()
    assert run.started_at is None
    assert run.finished_at is None


# --- harness flush hook (spec §6.2): same scripted helpers as Task 2 ----

PLAN_FLUSH_JSON = json.dumps(
    {
        "task": {
            "goal": "帮我找后端岗位",
            "allowed_skills": ["job-discovery"],
            "context": {"candidate_urls": ["https://example.com/jobs"]},
            "budget": {
                "max_agent_turns": 12,
                "max_tool_calls": 24,
                "max_replans": 2,
                "max_wall_clock_seconds": 300,
            },
        },
        "created_by": "planner",
        "complexity": "L1",
        "success_criteria": ["找到至少 1 个匹配岗位"],
        "steps": [
            {
                "step_id": "discover",
                "objective": "抓取并提取 JD",
                "allowed_skills": ["job-discovery"],
                "success_criteria": [],
                "requires_verification": True,
            }
        ],
    },
    ensure_ascii=False,
)
VERIFIER_PASS_JSON = json.dumps(
    {"decision": "PASS", "rationale": "ok"}, ensure_ascii=False
)


@tool
def stub_discovery_tool(payload: str) -> str:
    """Test stub: return one piece of tool-produced evidence as observation JSON."""
    return json.dumps(
        {
            "tool_name": "stub",
            "status": "succeeded",
            "output": {
                "source_url": "https://example.com/jobs",
                "content_hash": "abc123",
                "candidates": [{"title": "后端工程师"}],
            },
        }
    )


def _scripted_factory(scripted: dict[str, list[str] | list[AIMessage]]):
    """Return a model_factory consuming one ScriptedModel per role."""

    def factory(role: str) -> ScriptedModel:
        return ScriptedModel(responses=list(scripted[role]))

    return factory


def _request(**overrides) -> AgentTaskRequest:
    values = dict(
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery"],
        context={"candidate_urls": ["https://example.com/jobs"]},
    )
    values.update(overrides)
    return AgentTaskRequest(**values)


def test_harness_flushes_completed_run(db_session) -> None:
    # full run() invoke with a wired session_factory -> row + artifact in MySQL
    # (run() seeds user_id="" so the flush FK target is the empty-id user)
    _seed_user(db_session, "")
    harness = DeepAgentsHarness(
        model_factory=_scripted_factory(
            {
                "planner": [PLAN_FLUSH_JSON],
                "executor": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "stub_discovery_tool",
                                "args": {"payload": "{}"},
                                "id": "call_1",
                            }
                        ],
                    ),
                    "evidence collected",
                ],
                "verifier": [VERIFIER_PASS_JSON],
            }
        ),
        tool_factory=lambda skill: (
            [stub_discovery_tool] if skill == "job-discovery" else []
        ),
        checkpointer=InMemorySaver(),
        session_factory=lambda: db_session,
    )
    final = harness.run(_request(), run_id="run-flush")
    assert final["run_status"] == "succeeded"
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-flush").one()
    assert run.status.value == "succeeded"
    assert run.final_summary == "所有步骤已通过验证"
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-flush").all()
    assert len(artifacts) == 1
    assert artifacts[0].content_hash == "abc123"
    assert artifacts[0].payload_json == {"text": ""}  # stub output has no visible_text


def test_harness_flush_hook_evidence_branches(db_session) -> None:
    # direct _flush_if_configured call: artifact_id/visible_text truthy + falsy
    _seed_user(db_session, "user-1")
    harness = DeepAgentsHarness(
        model_factory=lambda role: None,
        checkpointer=InMemorySaver(),
        session_factory=lambda: db_session,
    )
    final = {
        "run_id": "run-direct",
        "user_id": "user-1",
        "goal": "帮我找后端岗位",
        "allowed_skills": ["job-discovery"],
        "budget": DeepAgentsBudgets(
            max_agent_turns=12, max_tool_calls=24, max_replans=2, max_wall_clock_seconds=300
        ).to_dict(),
        "run_status": "waiting_user",
        "plan_json": {"steps": []},
        "decisions": [{"role": "verifier", "decision": "NEED_USER"}],
        "error_code": "needs_user",
        "final_summary": None,
        "finished_at": 1723000060.0,
        "evidence_store": [
            {
                "artifact_id": "hash-1",
                "source_url": "https://a.com",
                "content_hash": "hash-1",
                "visible_text": "JD 文本",
            },
            {"source_url": "https://b.com", "content_hash": "hash-2"},
        ],
    }
    harness._flush_if_configured(final, started_at=1723000000.0)
    run = db_session.query(DeepAgentsRun).filter_by(thread_id="run-direct").one()
    assert run.status.value == "waiting_user"
    assert run.error_code == "needs_user"
    artifacts = db_session.query(DeepAgentsArtifact).filter_by(run_id="run-direct").all()
    assert len(artifacts) == 2
    by_hash = {a.content_hash: a for a in artifacts}
    assert by_hash["hash-1"].artifact_id == "hash-1"  # artifact_id truthy branch
    assert by_hash["hash-1"].payload_json == {"text": "JD 文本"}  # visible_text truthy
    assert by_hash["hash-2"].artifact_id == "hash-2"  # artifact_id or content_hash
    assert by_hash["hash-2"].payload_json == {"text": ""}  # visible_text falsy branch
