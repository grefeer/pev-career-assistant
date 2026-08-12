"""Round 5 O5: structured extraction remains usable after page compression."""

from __future__ import annotations

from backend.app.db.models import User, UserRole
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.runtime import AgentRuntime
from tests.unit.test_agent_runtime import _create_running_step


def _user() -> User:
    return User(
        id="round5-o5-user",
        account="round5-o5@example.test",
        nickname="round5-o5",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def test_structured_job_details_are_projected_as_bounded_evidence(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    for index in range(5):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{index}",
            content_hash=f"page-{index}" * 10,
            content_json={"visible_text": "page body " * 2_000},
        )
    structured = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/structured",
        content_hash="structured" * 7,
        content_json={
            "candidates": [
                {
                    "title": "大模型应用开发工程师",
                    "company_name": "公开招聘公司",
                    "locations": ["南京"],
                    "responsibilities": "负责 RAG 与 Agent 工作流开发",
                    "requirements": "熟悉 Python、LangChain 和 FastAPI",
                }
            ]
        },
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]
    target = next(item for item in evidence if item["artifact_id"] == structured.id)

    assert "visible_text" not in target
    assert target["source_url"] == "https://jobs.example/structured"


def test_malformed_structured_artifact_is_not_projected(db_session) -> None:
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    malformed = run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/malformed",
        content_hash="malformed" * 8,
        content_json={"candidates": ["not-a-candidate"]},
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    assert all(
        item["artifact_id"] != malformed.id
        for item in projected.context["observed_public_evidence"]
    )
