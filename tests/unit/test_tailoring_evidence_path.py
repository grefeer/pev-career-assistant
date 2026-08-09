"""Candidate C: tailoring evidence path — collapsed-pointer resolution tests.

C008 regression: a long discovery step collapses older artifacts in the 48k
decision projection to identifier-only pointers (``artifact_id`` /
``source_url``, never ``visible_text``). That used to make
``build-resume-tailoring-brief`` fail with ``target_evidence_incomplete``.
The tailoring tool now resolves collapsed pointers against the run's
structured extraction candidates, and the runtime preserves the full candidate
JD text (plus the source evidence artifact id) for that resolution.
"""

from __future__ import annotations

import pytest

from backend.app.db.models import User, UserRole
from backend.app.domain.agent_runtime import AgentRole, ComplexityLevel
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import (
    AgentBudget,
    AgentTaskRequest,
    ExecutionPlan,
    PlanStep,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.resume_tailoring import (
    BuildResumeTailoringBriefInput,
    ResumeTailoringError,
    build_resume_tailoring_brief,
)


def _brief_context(
    *,
    evidence: list[dict[str, object]],
    candidates: object | None = None,
    facts: dict[str, object] | None = None,
) -> ToolContext:
    """Build a ToolContext with the tailoring evidence and facts keys set."""
    metadata: dict[str, object] = {
        "observed_public_evidence": evidence,
        "confirmed_profile_facts": (
            facts if facts is not None else {"skills": ["Python", "RAG"]}
        ),
    }
    if candidates is not None:
        metadata["structured_job_candidates"] = candidates
    return ToolContext(user_id="user-a", run_id="run-a", metadata=metadata)


def _collapsed_pointer(
    artifact_id: str = "art-old", source_url: str = "https://jobs.example/old"
) -> dict[str, str]:
    """One identifier-only evidence line as produced by the projection budget."""
    return {
        "artifact_id": artifact_id,
        "source_url": source_url,
        "content_hash": "a" * 64,
    }


def _brief(
    context: ToolContext,
    *,
    target_artifact_id: str = "art-old",
    target_keywords: list[str] | None = None,
):
    return build_resume_tailoring_brief(
        context,
        BuildResumeTailoringBriefInput(
            target_artifact_id=target_artifact_id,
            target_keywords=target_keywords or ["Python", "RAG"],
        ),
    )


def test_tailoring_brief_resolves_collapsed_target_by_candidate_artifact_id() -> None:
    """A structured candidate whose own artifact_id matches the pointer wins."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "art-old",
            "source_url": "https://jobs.example/apply/1",
            "title": "AI Agent 开发工程师",
            "full_text": "要求 Python、RAG 和 Agent 开发经验。",
        }],
    )

    result = _brief(context, target_keywords=["Python", "RAG", "Agent"])

    assert result.supported_keywords == ["python", "rag"]
    assert result.missing_keywords == ["agent"]
    assert result.source_url == "https://jobs.example/apply/1"
    assert result.target_title == "AI Agent 开发工程师"


def test_tailoring_brief_resolves_collapsed_target_via_evidence_artifact_id() -> None:
    """The extraction artifact id matches when the candidate has its own id."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "structured-1",
            "source_artifact_id": "art-old",
            "source_url": "https://jobs.example/apply/1",
            "title": "AI Agent 开发工程师",
            "full_text": "负责 RAG 系统与 Python 服务开发。",
        }],
    )

    result = _brief(context)

    assert result.supported_keywords == ["python", "rag"]
    assert result.source_url == "https://jobs.example/apply/1"
    assert result.target_title == "AI Agent 开发工程师"


def test_tailoring_brief_resolves_collapsed_target_by_source_url() -> None:
    """A candidate without any artifact-id link still matches on source_url."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "structured-1",
            "source_url": "https://jobs.example/old",
            "full_text": "要求 Python 与 Rust。",
        }],
    )

    result = _brief(context, target_keywords=["Python", "Rust"])

    assert result.supported_keywords == ["python"]
    assert result.missing_keywords == ["rust"]


def test_tailoring_brief_preserves_incomplete_failure_without_structured_candidates() -> None:
    """No candidates (absent, empty, or non-matching) keeps the old failure."""
    without_key = _brief_context(evidence=[_collapsed_pointer()])
    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(without_key)
    empty = _brief_context(evidence=[_collapsed_pointer()], candidates=[])
    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(empty)
    non_matching = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "other",
            "source_url": "https://jobs.example/other",
            "full_text": "要求 Rust。",
        }],
    )
    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(non_matching)
    # A pointer without a source_url cannot resolve either.
    no_source = _brief_context(
        evidence=[{"artifact_id": "art-old"}],
        candidates=[{
            "artifact_id": "other",
            "source_url": "https://jobs.example/other",
            "full_text": "要求 Rust。",
        }],
    )
    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(no_source)


def test_tailoring_brief_normal_path_uses_visible_text_even_when_candidates_exist() -> None:
    """A full artifact keeps the existing visible-text path; no fallback."""
    context = _brief_context(
        evidence=[{
            "artifact_id": "jd",
            "source_url": "https://jobs.example/jd",
            "content_hash": "b" * 64,
            "title": "前端工程师",
            "visible_text": "要求 Python 与 Vue。",
        }],
        candidates=[{
            "artifact_id": "jd",
            "source_url": "https://jobs.example/jd",
            "full_text": "要求 Rust。",
        }],
    )

    result = _brief(
        context, target_artifact_id="jd", target_keywords=["Python", "Vue", "Rust"]
    )

    # The visible-text path wins (Python/Vue are in the page text, Rust is
    # not in the JD at all so it cannot be a gap), never the candidate text.
    assert result.supported_keywords == ["python"]
    assert result.missing_keywords == ["vue"]
    assert result.target_title == "前端工程师"


def test_tailoring_brief_fallback_builds_text_from_sections_without_full_text() -> None:
    """Candidates without full_text degrade to their bounded sections."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "art-old",
            "source_url": "https://jobs.example/old",
            "title": "AI Agent 开发工程师",
            "company_name": "某公司",
            "responsibilities": "负责 RAG 系统设计",
            "requirements": "要求 Python 与 Rust。",
        }],
    )

    result = _brief(context, target_keywords=["Python", "RAG", "Rust"])

    assert result.supported_keywords == ["python", "rag"]
    assert result.missing_keywords == ["rust"]


def test_tailoring_brief_rejects_structured_candidate_without_any_text() -> None:
    """A matched candidate with no usable text still means incomplete evidence."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{
            "artifact_id": "art-old",
            "source_url": "https://jobs.example/old",
            "title": 9,
        }],
    )

    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(context)


def test_tailoring_brief_rejects_structured_candidate_without_source_url() -> None:
    """A matched candidate must still carry its traceable source_url."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[{"artifact_id": "art-old", "full_text": "要求 Python。"}],
    )

    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(context)


def test_tailoring_brief_whitespace_visible_text_falls_back_to_structured_candidate() -> None:
    """Whitespace-only visible_text is treated as a collapsed pointer."""
    context = _brief_context(
        evidence=[{
            "artifact_id": "art-old",
            "source_url": "https://jobs.example/old",
            "visible_text": "   ",
        }],
        candidates=[{
            "artifact_id": "art-old",
            "source_url": "https://jobs.example/old",
            "full_text": "要求 Python。",
        }],
    )

    result = _brief(context, target_keywords=["Python"])

    assert result.supported_keywords == ["python"]


def test_tailoring_brief_rejects_non_str_source_url_with_visible_text() -> None:
    """A full artifact with a non-string source_url keeps failing immediately."""
    context = _brief_context(
        evidence=[{
            "artifact_id": "jd",
            "source_url": 123,
            "visible_text": "要求 Python。",
        }],
        candidates=[{
            "artifact_id": "jd",
            "source_url": "https://jobs.example/jd",
            "full_text": "要求 Python。",
        }],
    )

    with pytest.raises(ResumeTailoringError, match="target_evidence_incomplete"):
        _brief(context, target_artifact_id="jd")


def test_tailoring_brief_skips_non_dict_structured_candidates() -> None:
    """Malformed candidate entries are skipped, not fatal."""
    context = _brief_context(
        evidence=[_collapsed_pointer()],
        candidates=[
            "junk",
            42,
            {
                "artifact_id": "art-old",
                "source_url": "https://jobs.example/old",
                "full_text": "要求 Python。",
            },
        ],
    )

    result = _brief(context, target_keywords=["Python"])

    assert result.supported_keywords == ["python"]


def _create_running_step(db_session, user: User):
    """Persist a minimal running run/plan/step for projection tests."""
    task = AgentTaskRequest(
        goal="验证 tailoring 证据路径",
        allowed_skills=["job-discovery", "resume-tailoring"],
        private_context={"confirmed_profile_facts": {"skills": ["Python", "RAG"]}},
        budget=AgentBudget(max_agent_turns=4, max_tool_calls=4, max_replans=0),
    )
    plan_step = PlanStep(
        step_id="discover",
        objective="提取公开岗位",
        allowed_skills=["job-discovery"],
        requires_verification=False,
    )
    plan = ExecutionPlan(
        task=task,
        created_by=AgentRole.planner,
        complexity=ComplexityLevel.L2,
        success_criteria=["有证据"],
        steps=[plan_step],
    )
    run = run_repository.create_run(
        db_session,
        user_id=user.id,
        goal=task.goal,
        allowed_skills=task.allowed_skills,
        context_summary={},
        budget_json=task.budget.model_dump(mode="json"),
        agent_version="pev-test",
    )
    run_repository.start_run(db_session, run)
    stored_plan = run_repository.create_plan(
        db_session,
        run_id=run.id,
        revision=1,
        complexity=plan.complexity,
        plan_json=plan.model_dump(mode="json"),
    )
    step = run_repository.create_step(
        db_session,
        run_id=run.id,
        plan_id=stored_plan.id,
        sequence=1,
        objective=plan_step.objective,
        allowed_skills=plan_step.allowed_skills,
    )
    return run, task, step


def test_tool_context_structured_candidates_preserve_full_text_and_evidence_id(
    db_session,
) -> None:
    """The tool-side candidate projection keeps full JD text and the source id."""
    user = User(
        id="user-a",
        account="user-a@example.test",
        nickname="user-a",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, step = _create_running_step(db_session, user)
    evidence = run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/list",
        content_hash="e" * 64,
        content_json={"title": "校招列表", "visible_text": "岗位列表正文"},
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/list",
        content_hash="s" * 64,
        content_json={"candidates": [
            {
                "title": "提前批-Agent开发工程师-NOMI",
                "apply_url": "https://jobs.example/apply/1",
                "company_name": 42,
                "locations": ["北京、上海"],
                "responsibilities": 42,
                "requirements": "x" * 700,
                "evidence_refs": [
                    "junk",
                    {"artifact_id": 123},
                    {
                        "artifact_id": evidence.id,
                        "source_url": "https://jobs.example/list",
                        "content_hash": "e" * 64,
                    },
                ],
            },
            {
                "title": "无证据引用岗位",
                "requirements": "要求 Python。",
            },
            {
                "title": "证据引用不可用岗位",
                "requirements": "要求 Rust。",
                "evidence_refs": [
                    "junk",
                    {"artifact_id": 123},
                ],
            },
        ]},
    )

    context = AgentRuntime._tool_context(
        user_id=user.id, run_id=run.id, task=task, db=db_session
    )

    candidates = context.metadata["structured_job_candidates"]
    assert candidates[0]["source_artifact_id"] == evidence.id
    # The bounded section key keeps its historical 600-char cap.
    assert candidates[0]["requirements"] == "x" * 600
    # The full-text key preserves the complete candidate JD text.
    assert candidates[0]["full_text"] == "\n".join(
        ["提前批-Agent开发工程师-NOMI", "北京、上海", "x" * 700]
    )
    # A candidate without evidence_refs carries no source artifact id.
    assert candidates[1]["source_artifact_id"] is None
    assert candidates[1]["full_text"] == "无证据引用岗位\n要求 Python。"
    # Unusable evidence_refs (non-dict refs, non-str ids) exhaust the loop and
    # resolve to no source artifact id.
    assert candidates[2]["source_artifact_id"] is None


def test_tool_context_resolves_collapsed_old_artifact_for_tailoring(db_session) -> None:
    """A long discovery step must not strip the tailoring tool's target text.

    C008 shape: the newest artifact consumes the 48k projection budget, the
    older target collapses to an identifier-only pointer, and the tailoring
    brief is still built from the structured candidate's full job text.
    """
    user = User(
        id="user-a",
        account="user-a@example.test",
        nickname="user-a",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )
    db_session.add(user)
    db_session.commit()
    run, task, step = _create_running_step(db_session, user)
    old_evidence = run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/old",
        content_hash="f" * 64,
        content_json={
            "title": "AI Agent 开发工程师",
            "visible_text": "岗位要求 Python 与 RAG 开发经验。",
        },
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/recent",
        content_hash="g" * 64,
        content_json={"visible_text": "y" * 48_000},
    )
    run_repository.create_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        artifact_type="structured_job_details",
        source_url="https://jobs.example/old",
        content_hash="h" * 64,
        content_json={"candidates": [
            {
                "title": "AI Agent 开发工程师",
                "responsibilities": "负责 RAG 系统与 Agent 平台开发。",
                "requirements": "要求 Python 与 RAG 开发经验。",
                "evidence_refs": [{
                    "artifact_id": old_evidence.id,
                    "source_url": "https://jobs.example/old",
                    "content_hash": "f" * 64,
                }],
            }
        ]},
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)

    evidence = projected.context["observed_public_evidence"]
    assert len(evidence) == 2
    assert evidence[0]["artifact_id"] == old_evidence.id
    assert "visible_text" not in evidence[0]  # oldest collapsed to a pointer

    context = AgentRuntime._tool_context(
        user_id=user.id, run_id=run.id, task=projected, db=db_session
    )
    result = _brief(
        context,
        target_artifact_id=old_evidence.id,
        target_keywords=["Python", "RAG", "Agent"],
    )

    assert result.supported_keywords == ["python", "rag"]
    assert result.missing_keywords == ["agent"]
    assert result.source_url == "https://jobs.example/old"
    assert result.target_title == "AI Agent 开发工程师"
