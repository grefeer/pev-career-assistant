"""Compare agent_runtime vs deepagents_runtime on the same question set.

Runs against the real stack (docker compose: Redis + MySQL + LLM key).
Writes ``report.json`` and ``report.md`` under ``out_dir``: success
distribution (succeeded / waiting_user / failed), avg turns, tool calls,
replans, wall-clock and error codes, per question.

The legacy leg runs through the real AgentRunService so both sides execute
their full production code paths; the deepagents leg runs the harness with
a real ChatOpenAI (DeepSeek via the same environment provider as the
legacy gateway).  Live only — never unit-tested end to end.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import func, select

from backend.app.config import Settings
from backend.app.db.models import AgentEvent, AgentPlan, AgentStep, AgentTurn
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import build_agent_model_gateway
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
from backend.app.services.agent_runtime.service import AgentRunService
from backend.app.services.agent_runtime.verifier_agent import VerifierAgent
from backend.app.services.career_skills.registry import build_career_tool_registry
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.checkpoints.factory import create_checkpointer
from backend.app.services.deepagents_runtime.harness import DeepAgentsHarness
from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    build_job_discovery_tools,
)


@dataclass
class Question:
    id: str
    goal: str
    allowed_skills: list[str]
    context: dict


@dataclass
class RunMetrics:
    status: str
    steps: int
    turns: int
    tool_calls: int
    replans: int
    wall_clock_s: float
    error_code: str | None


def run_legacy_question(
    question: Question,
    *,
    settings: Settings,
    session_factory,
    runner=None,
) -> RunMetrics:
    """Run one question through the existing agent_runtime service.

    ``runner`` is an injectable seam for unit tests: ``(question, settings,
    session_factory) -> result-like`` with ``run_id`` / ``status`` /
    ``error_code`` attributes.  Defaults to the real service assembly (live
    eval only — the default path is unit-covered by monkeypatching the
    ``cr.*`` assembly names, see ``test_run_legacy_question_default_runner_path``).
    """
    if runner is None:

        def default_runner(question, settings, session_factory):
            gateway = build_agent_model_gateway(settings)
            tools = build_career_tool_registry()
            runtime = AgentRuntime(
                planner=PlannerAgent(gateway=gateway, tools=tools),
                executor=ExecutorAgent(gateway=gateway, tools=tools),
                verifier=VerifierAgent(gateway=gateway, tools=tools),
                agent_version="pev-1",
            )
            service = AgentRunService(settings, runtime=runtime)
            task = AgentTaskRequest(
                goal=question.goal,
                allowed_skills=question.allowed_skills,
                context=question.context,
            )
            with session_factory.begin() as db:  # commit so the counters can read it
                return service.create_run(db, user_id="eval-user", task=task)

        runner = default_runner
    started = time.monotonic()
    result = runner(question, settings, session_factory)
    elapsed = time.monotonic() - started
    with session_factory() as db:
        steps = db.scalar(
            select(func.count())
            .select_from(AgentStep)
            .where(AgentStep.run_id == result.run_id)
        ) or 0
        turns = db.scalar(
            select(func.count())
            .select_from(AgentTurn)
            .where(AgentTurn.run_id == result.run_id)
        ) or 0
        replans = max(
            0,
            (
                db.scalar(
                    select(func.count())
                    .select_from(AgentPlan)
                    .where(AgentPlan.run_id == result.run_id)
                )
                or 1
            )
            - 1,
        )
        events = db.scalars(
            select(AgentEvent).where(AgentEvent.run_id == result.run_id)
        )
        tool_calls = sum(
            1 for event in events if (event.payload_json or {}).get("tool")
        )
    return RunMetrics(
        status=result.status.value,
        steps=steps,
        turns=turns,
        tool_calls=tool_calls,
        replans=replans,
        wall_clock_s=round(elapsed, 2),
        error_code=result.error_code,
    )


def run_deepagents_question(
    question: Question, *, settings: Settings, run_id: str, harness=None
) -> RunMetrics:
    """Run one question through the deepagents harness (real model).

    ``harness`` is an injectable seam for unit tests: an object with
    ``run(request, *, run_id, budgets=None) -> final dict`` (keys
    ``run_status`` / ``budget`` / ``plan_json`` / ``error_code``).
    Defaults to real ChatOpenAI + DeepAgentsHarness (live eval only — the
    default path is unit-covered by monkeypatching ``cr.DeepAgentsHarness``
    and ``langchain_openai.ChatOpenAI``, see
    ``test_run_deepagents_question_default_harness_path``).

    Task 11 wiring: the harness receives a ``tool_factory`` (closure
    captures the run_id, which the harness cannot read from the workflow
    thread ContextVar — it binds the thread after the factory call) so
    job-discovery tools run with run-scoped output dirs (controller D1).
    Final-review wiring (I1/I4): the closure also captures ``settings``
    (spec §4.3 LLM-extraction gate) and the harness's own checkpointer
    (mid-crawl resume) — the same instance the harness graph uses.
    """
    if harness is None:
        from langchain_openai import ChatOpenAI

        def model_factory(role: str) -> ChatOpenAI:
            return ChatOpenAI(
                model=settings.agent_harness_model,
                temperature=0,
                max_tokens=4096 if role == "planner" else 2048,
            )

        checkpointer = create_checkpointer(settings)
        harness = DeepAgentsHarness(
            model_factory=model_factory,
            checkpointer=checkpointer,
            tool_factory=lambda skill: build_job_discovery_tools(
                skill,
                run_id=run_id,
                settings=settings,
                checkpointer=checkpointer,
            ),
        )
    request = AgentTaskRequest(
        goal=question.goal,
        allowed_skills=question.allowed_skills,
        context=question.context,
    )
    started = time.monotonic()
    final = harness.run(request, run_id=run_id)
    elapsed = time.monotonic() - started
    budgets = DeepAgentsBudgets.from_dict(final["budget"])
    plan_json = final.get("plan_json") or {}
    steps = len(plan_json.get("steps", [])) if isinstance(plan_json, dict) else 0
    return RunMetrics(
        status=final["run_status"] or "unknown",
        steps=steps,
        turns=budgets.turns_used,
        tool_calls=budgets.tool_calls_used,
        replans=budgets.replans_used,
        wall_clock_s=round(elapsed, 2),
        error_code=final.get("error_code"),
    )


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def summarize_comparison(
    *, legacy: list[RunMetrics], deepagents: list[RunMetrics]
) -> dict:
    """Aggregate per-runtime distributions and averages for the report."""

    def bucket(metrics: list[RunMetrics]) -> dict:
        statuses = [m.status for m in metrics]
        succeeded = statuses.count("succeeded")
        waiting_user = statuses.count("waiting_user")
        failed = statuses.count("failed")
        return {
            "succeeded": succeeded,
            "waiting_user": waiting_user,
            "failed": failed,
            # any other status (e.g. "unknown" from the harness seam when
            # run_status is falsy, or stray RunStatus values) closes the
            # tally: succeeded + waiting_user + failed + unknown == total
            # always (Task 6 review minor d)
            "unknown": len(metrics) - succeeded - waiting_user - failed,
            "total": len(metrics),
            "avg_steps": _avg([m.steps for m in metrics]),
            "avg_turns": _avg([m.turns for m in metrics]),
            "avg_tool_calls": _avg([m.tool_calls for m in metrics]),
            "avg_wall_clock_s": _avg([m.wall_clock_s for m in metrics]),
            "replan_total": sum(m.replans for m in metrics),
            "error_codes": sorted({m.error_code for m in metrics if m.error_code}),
        }

    return {"legacy": bucket(legacy), "deepagents": bucket(deepagents)}


def run_comparison(
    questions: list[Question], *, out_dir: Path, settings: Settings, session_factory
) -> dict:
    """Run both runtimes over the questions and write report.json + report.md.

    Per-question isolation (Task 6 review minor c): each leg of each
    question is wrapped in ``try/except`` — a failing leg is recorded as
    ``{"error": "<leg>: <exc-type>: <exc>"}`` on that question's
    ``per_question`` entry and the round continues; a single failing
    question never kills the round.  Surviving legs still enter the
    summary metrics; failed legs contribute no metrics.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy_metrics: list[RunMetrics] = []
    deepagents_metrics: list[RunMetrics] = []
    per_question: list[dict] = []
    for question in questions:
        entry: dict = {"id": question.id, "goal": question.goal}
        errors: list[str] = []
        try:
            deepagents_metrics.append(
                run_deepagents_question(
                    question, settings=settings, run_id=f"eval-{question.id}"
                )
            )
            entry["deepagents"] = asdict(deepagents_metrics[-1])
        except Exception as exc:
            errors.append(f"deepagents: {type(exc).__name__}: {exc}")
        try:
            legacy_metrics.append(
                run_legacy_question(
                    question, settings=settings, session_factory=session_factory
                )
            )
            entry["legacy"] = asdict(legacy_metrics[-1])
        except Exception as exc:
            errors.append(f"legacy: {type(exc).__name__}: {exc}")
        if errors:
            entry["error"] = "; ".join(errors)
        per_question.append(entry)
    summary = summarize_comparison(
        legacy=legacy_metrics, deepagents=deepagents_metrics
    )
    report = {"summary": summary, "per_question": per_question}
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "report.md").write_text(_render_markdown(report), encoding="utf-8")
    return report


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = ["# DeepAgents Runtime 对比评测", ""]
    for runtime in ("legacy", "deepagents"):
        bucket = summary[runtime]
        lines.append(f"## {runtime}")
        lines.append(
            f"- succeeded={bucket['succeeded']} waiting_user={bucket['waiting_user']} "
            f"failed={bucket['failed']} unknown={bucket['unknown']} "
            f"total={bucket['total']}"
        )
        lines.append(
            f"- avg_steps={bucket['avg_steps']} avg_turns={bucket['avg_turns']} "
            f"avg_tool_calls={bucket['avg_tool_calls']} "
            f"avg_wall_clock_s={bucket['avg_wall_clock_s']} replan_total={bucket['replan_total']}"
        )
        lines.append(f"- error_codes={bucket['error_codes']}")
        lines.append("")
    return "\n".join(lines)


def _load_questions(ids: list[str]) -> list[Question]:
    """Load Q###.json / C###.json docs from tests/question (schema verified)."""
    question_dir = Path(__file__).resolve().parents[5] / "tests" / "question"
    all_ids = sorted(path.stem for path in question_dir.glob("*.json"))
    questions: list[Question] = []
    for qid in ids or all_ids:
        doc_path = question_dir / f"{qid}.json"
        if not doc_path.exists():
            print(f"SKIP {qid}: {doc_path.name} missing")
            continue
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        meta = doc.get("meta") or {}
        skills = meta.get("skills") or []
        if not skills or "chain" in doc:
            print(f"SKIP {qid}: needs meta.skills (chain docs run via eval_runner)")
            continue
        questions.append(
            Question(
                id=doc["id"],
                goal=doc["question"],
                allowed_skills=skills,
                context={"profile": doc.get("profile"), "meta": meta},
            )
        )
    return questions


def main(argv: list[str] | None = None) -> int:
    """CLI: ``--ids Q001,Q002 --out-dir <dir>`` (defaults to all questions)."""
    import argparse

    from backend.app.config import get_settings
    from backend.app.db.session import SessionLocal
    from backend.app.services.agent_runtime.provider_config import load_project_env

    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default="")
    parser.add_argument(
        "--out-dir", default="tests/question/eval_results/deepagents_round_1"
    )
    args = parser.parse_args(argv)
    ids = [value.strip() for value in args.ids.split(",") if value.strip()]

    load_project_env()  # ensure .env vars for settings + LLM key (mirrors eval_runner)
    questions = _load_questions(ids)
    if not questions:
        print("no questions loaded")
        return 1
    run_comparison(
        questions,
        out_dir=Path(args.out_dir),
        settings=get_settings(),
        session_factory=SessionLocal,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
