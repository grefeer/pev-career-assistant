from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from backend.app.db.models import (
    AgentEvent,
    AgentPlan,
    AgentRun,
    AgentStep,
    AgentTurn,
    User,
)
from backend.app.domain.agent_runtime import ComplexityLevel, RunStatus
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.eval.compare_runner import (
    Question,
    RunMetrics,
    _load_questions,
    main,
    run_comparison,
    run_deepagents_question,
    run_legacy_question,
    summarize_comparison,
)


def _question() -> Question:
    return Question(
        id="Q001",
        goal="帮我找后端岗位",
        allowed_skills=["job-discovery"],
        context={"profile": {}},
    )


class _FakeResult:
    def __init__(self, run_id, status, error_code=None):
        self.run_id = run_id
        self.status = status
        self.error_code = error_code


class _FakeHarness:
    def __init__(self, finals):
        self._finals = list(finals)

    def run(self, request, *, run_id, budgets=None):
        self.request = request
        self.run_id = run_id
        return self._finals.pop(0)


def test_summarize_comparison_computes_distribution_and_counts() -> None:
    legacy = [
        RunMetrics("succeeded", steps=2, turns=4, tool_calls=3, replans=0, wall_clock_s=10.0, error_code=None),
        RunMetrics("waiting_user", steps=1, turns=2, tool_calls=1, replans=0, wall_clock_s=5.0, error_code="blocked"),
    ]
    deepagents = [
        RunMetrics("succeeded", steps=2, turns=3, tool_calls=2, replans=0, wall_clock_s=8.0, error_code=None),
        RunMetrics("succeeded", steps=2, turns=5, tool_calls=4, replans=1, wall_clock_s=12.0, error_code=None),
    ]
    summary = summarize_comparison(legacy=legacy, deepagents=deepagents)
    assert summary["legacy"]["succeeded"] == 1
    assert summary["deepagents"]["succeeded"] == 2
    assert summary["deepagents"]["avg_turns"] == 4.0
    assert summary["deepagents"]["replan_total"] == 1
    assert summary["deepagents"]["avg_wall_clock_s"] == 10.0
    assert summary["legacy"]["error_codes"] == ["blocked"]


def test_summarize_comparison_empty_inputs() -> None:
    summary = summarize_comparison(legacy=[], deepagents=[])
    assert summary["legacy"]["avg_turns"] == 0.0
    assert summary["deepagents"]["total"] == 0


def test_run_legacy_question_counts_db_rows(db_session) -> None:
    # Dev from the brief: the test DB enforces FKs, so the rows need real
    # parents (User -> AgentRun -> AgentPlan -> AgentStep); the brief's
    # `AgentStep(step_id=..., agent_role=...)` kwargs don't exist on the
    # real models (mirror eval_runner's user creation pattern instead).
    user = User(account="eval-1@eval.test", nickname="eval", password_hash="x")
    db_session.add(user)
    db_session.flush()
    db_session.add(
        AgentRun(
            id="legacy-1",
            user_id=user.id,
            goal="g",
            allowed_skills_json=["job-discovery"],
            context_summary_json={},
            budget_json={},
            agent_version="pev-1",
            status=RunStatus.succeeded,
        )
    )
    db_session.flush()
    # two plan revisions -> count 2 -> replans = 2 - 1 (the brief's single
    # revision=2 row would count as 1 and yield replans == 0)
    db_session.add_all(
        [
            AgentPlan(
                run_id="legacy-1", revision=1, complexity=ComplexityLevel.L1, plan_json={}
            ),
            AgentPlan(
                run_id="legacy-1", revision=2, complexity=ComplexityLevel.L1, plan_json={}
            ),
        ]
    )
    db_session.flush()
    plan = db_session.scalars(
        select(AgentPlan).where(AgentPlan.run_id == "legacy-1").order_by(AgentPlan.revision)
    ).first()
    db_session.add_all(
        [
            AgentStep(
                run_id="legacy-1",
                plan_id=plan.id,
                sequence=0,
                objective="目标",
                allowed_skills_json=["job-discovery"],
                status="succeeded",
            ),
            AgentTurn(run_id="legacy-1", role="executor", turn_index=0, decision_json={}),
            AgentTurn(run_id="legacy-1", role="executor", turn_index=1, decision_json={}),
            AgentEvent(
                run_id="legacy-1",
                sequence=0,
                event_type="tool_call",
                payload_json={"tool": "fetch-public-job-pages"},
            ),
            # payload_json={} -> `payload_json or {}` falsy branch (the
            # brief's payload_json=None would violate the NOT NULL column)
            AgentEvent(run_id="legacy-1", sequence=1, event_type="other", payload_json={}),
        ]
    )
    db_session.commit()

    def fake_runner(question, settings, session_factory):
        return _FakeResult(run_id="legacy-1", status=RunStatus.succeeded)

    metrics = run_legacy_question(
        _question(), settings=None, session_factory=lambda: db_session, runner=fake_runner
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 1
    assert metrics.turns == 2
    assert metrics.tool_calls == 1
    assert metrics.replans == 1  # plan_count 2 - 1


def test_run_legacy_question_empty_tables(db_session) -> None:
    def fake_runner(question, settings, session_factory):
        return _FakeResult(run_id="legacy-2", status=RunStatus.succeeded)

    metrics = run_legacy_question(
        _question(), settings=None, session_factory=lambda: db_session, runner=fake_runner
    )
    assert metrics.steps == 0  # `or 0` fallback
    assert metrics.replans == 0  # `or 1` fallback: max(0, 1 - 1)


def test_run_deepagents_question_parses_harness_output() -> None:
    # all four ceilings are required (no defaults on the real dataclass)
    budgets = DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=20, max_replans=2, max_wall_clock_seconds=600
    )
    harness = _FakeHarness(
        [
            {
                "run_status": "waiting_user",
                "budget": budgets.to_dict(),
                "plan_json": {"steps": [{"step_index": 0}, {"step_index": 1}]},
                "error_code": "stalled_no_progress",
            },
            # truthy non-dict plan_json -> isinstance(..., dict) false branch;
            # falsy run_status -> `or "unknown"` fallback branch
            {
                "run_status": "",
                "budget": budgets.to_dict(),
                "plan_json": ["not", "a", "dict"],
                "error_code": None,
            },
        ]
    )
    first = run_deepagents_question(
        _question(), settings=None, run_id="eval-Q001", harness=harness
    )
    assert first.status == "waiting_user"
    assert first.steps == 2
    assert first.turns == budgets.turns_used
    assert first.error_code == "stalled_no_progress"
    assert harness.run_id == "eval-Q001"
    assert harness.request.allowed_skills == ["job-discovery"]
    second = run_deepagents_question(
        _question(), settings=None, run_id="eval-Q001", harness=harness
    )
    assert second.status == "unknown"  # `or "unknown"` falsy branch
    assert second.steps == 0  # non-dict plan_json -> else branch


def test_run_comparison_writes_report_files(tmp_path, monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    def fake_legacy(q, *, settings, session_factory):
        return RunMetrics("succeeded", steps=1, turns=2, tool_calls=1, replans=0, wall_clock_s=1.0, error_code=None)

    def fake_deepagents(q, *, settings, run_id):
        return RunMetrics("succeeded", steps=1, turns=1, tool_calls=1, replans=0, wall_clock_s=0.5, error_code=None)

    monkeypatch.setattr(cr, "run_legacy_question", fake_legacy)
    monkeypatch.setattr(cr, "run_deepagents_question", fake_deepagents)
    report = run_comparison(
        [_question()], out_dir=tmp_path, settings=None, session_factory=None
    )
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "report.md").exists()
    assert report["summary"]["legacy"]["succeeded"] == 1
    assert "DeepAgents Runtime 对比评测" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_load_questions_skips_missing_docs() -> None:
    assert _load_questions(["NO_SUCH_QUESTION"]) == []


def test_load_questions_skips_chain_docs(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    chain_doc = json.dumps(
        {"id": "Q001", "question": "g", "chain": [{"id": "Q001a"}], "meta": {"skills": ["job-discovery"]}},
        ensure_ascii=False,
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: chain_doc)
    assert _load_questions(["Q001"]) == []


def test_main_no_questions_returns_1(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.load_project_env", lambda: None
    )
    monkeypatch.setattr(cr, "_load_questions", lambda ids: [])
    assert main(["--ids", "Q001", "--out-dir", "unused"]) == 1


def test_main_runs_comparison(tmp_path, monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr
    from backend.app.config import Settings
    from backend.app.db import session as db_session_module

    calls = {}
    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.load_project_env", lambda: None
    )
    monkeypatch.setattr("backend.app.config.get_settings", lambda: Settings())
    monkeypatch.setattr(db_session_module, "SessionLocal", object())
    monkeypatch.setattr(cr, "_load_questions", lambda ids: [_question()])

    def fake_comparison(questions, *, out_dir, settings, session_factory):
        calls["questions"] = questions
        calls["out_dir"] = out_dir
        return {"summary": {}, "per_question": []}

    monkeypatch.setattr(cr, "run_comparison", fake_comparison)
    # trailing comma covers the ids-comprehension filter falsy branch
    assert main(["--ids", "Q001,", "--out-dir", str(tmp_path)]) == 0
    assert calls["questions"][0].id == "Q001"
    assert calls["out_dir"] == tmp_path


def test_run_legacy_question_default_runner_path(monkeypatch, db_session) -> None:
    import contextlib

    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    class _FakeAgentRunService:
        def __init__(self, settings, *, runtime):
            self.settings = settings
            self.runtime = runtime

        def create_run(self, db, *, user_id, task):
            assert user_id == "eval-user"
            assert task.goal == "帮我找后端岗位"
            return _FakeResult(run_id="legacy-default", status=RunStatus.succeeded)

    class _FakeSessionFactory:
        def __init__(self, session):
            self._session = session

        def __call__(self):
            return self._session

        def begin(self):
            return contextlib.nullcontext(self._session)

    # the default runner closure resolves these names in the cr module
    # namespace at call time, so patching them covers the real assembly
    monkeypatch.setattr(cr, "build_agent_model_gateway", lambda settings: object())
    monkeypatch.setattr(cr, "build_career_tool_registry", lambda: object())
    monkeypatch.setattr(cr, "PlannerAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "ExecutorAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "VerifierAgent", lambda gateway, tools: object())
    monkeypatch.setattr(cr, "AgentRuntime", lambda **kwargs: object())
    monkeypatch.setattr(cr, "AgentRunService", _FakeAgentRunService)
    metrics = run_legacy_question(
        _question(),
        settings=None,
        session_factory=_FakeSessionFactory(db_session),
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 0  # no rows for legacy-default


def test_run_deepagents_question_default_harness_path(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    class _Settings:
        agent_harness_model = "deepseek-chat"

    # all four ceilings are required (no defaults on the real dataclass)
    budgets = DeepAgentsBudgets(
        max_agent_turns=12, max_tool_calls=20, max_replans=2, max_wall_clock_seconds=600
    )
    final = {
        "run_status": "succeeded",
        "budget": budgets.to_dict(),
        # plan_json None -> `final.get(...) or {}` falsy branch
        "plan_json": None,
        "error_code": None,
    }

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class _FakeHarnessClass:
        def __init__(self, *, model_factory, checkpointer):
            # exercise both role branches of the default model_factory
            model_factory("planner")
            model_factory("executor")

        def run(self, request, *, run_id, budgets=None):
            return final

    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChatOpenAI)
    monkeypatch.setattr(cr, "DeepAgentsHarness", _FakeHarnessClass)
    monkeypatch.setattr(cr, "create_checkpointer", lambda settings: object())
    metrics = run_deepagents_question(
        _question(), settings=_Settings(), run_id="eval-default"
    )
    assert metrics.status == "succeeded"
    assert metrics.steps == 0
    assert metrics.turns == 0
    assert metrics.tool_calls == 0


def test_load_questions_empty_ids_uses_all_docs(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    good_doc = json.dumps(
        {"id": "Q001", "question": "帮我找后端岗位", "meta": {"skills": ["job-discovery"]}},
        ensure_ascii=False,
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: good_doc)
    questions = _load_questions([])  # ids falsy -> glob all docs branch
    assert questions  # tests/question has at least one .json doc
    assert questions[0].allowed_skills == ["job-discovery"]


def test_load_questions_skips_docs_without_skills(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    no_skills = json.dumps(
        {"id": "Q001", "question": "g", "meta": {}}, ensure_ascii=False
    )
    monkeypatch.setattr(cr.Path, "read_text", lambda self, **kwargs: no_skills)
    assert _load_questions(["Q001"]) == []


def test_main_entrypoint_guard_runs_cli(monkeypatch) -> None:
    """Cover the ``__main__`` guard (needed for the 100% branch gate).

    Re-executes the module file as ``__main__``; an unknown --ids makes the
    real CLI return 1 without touching the stack (no LLM / no DB).
    """
    import runpy
    import sys

    import backend.app.services.deepagents_runtime.eval.compare_runner as cr

    monkeypatch.setattr(
        "backend.app.services.agent_runtime.provider_config.load_project_env", lambda: None
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare_runner", "--ids", "NO_SUCH_QUESTION", "--out-dir", "unused"],
    )
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(cr.__file__), run_name="__main__")
    assert excinfo.value.code == 1
