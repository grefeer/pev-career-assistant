"""Compare agent_runtime vs deepagents_runtime on the same question set.

Runs against the real stack (docker compose: Redis + MySQL + LLM key).
Writes ``report.json`` and ``report.md`` under ``out_dir``: success
distribution (succeeded / waiting_user / failed), avg turns, tool calls,
replans, wall-clock and error codes, per question.

The comparison set is the redesign archive (spec §7): 21 kept Q-docs + 47
R-docs + 15 chain C-docs from ``tests/question/redesign``.  Ids resolve
root-first (``tests/question/{id}.json``) then fall back to
``tests/question/redesign/{id}.json``; chain docs expand into one
ChainQuestion whose links run under ``C###-L<n>`` ids with the
eval_runner contract (link N+1 only starts when N succeeded; the previous
link's collected artifact URLs become the next link's ``candidate_urls`` +
a ``chain_context`` note).

The legacy leg runs through the real AgentRuntime (the eval_runner live
path) so both sides execute their full production code paths; the
deepagents leg runs the harness with a real ChatOpenAI (DeepSeek via the
same environment provider as the legacy gateway).  Live only — never
unit-tested end to end.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import func, select

from backend.app.config import Settings
from backend.app.db.models import (
    AgentArtifact,
    AgentEvent,
    AgentPlan,
    AgentRun,
    AgentStep,
    AgentTurn,
)
from backend.app.services.agent_runtime.executor_agent import ExecutorAgent
from backend.app.services.agent_runtime.model_gateway import build_agent_model_gateway
from backend.app.services.agent_runtime.planner_agent import PlannerAgent
from backend.app.services.agent_runtime.runtime import AgentRuntime
from backend.app.services.agent_runtime.schemas import AgentTaskRequest
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
class ChainQuestion:
    """A chained question doc (``{"chain": [link...]}``), links as Questions.

    Each link is its own Question under ``C###-L<n>`` (the link's own
    ``meta.skills`` / ``profile``); the chain contract lives in
    ``run_chain_comparison`` (link N+1 only when N succeeded; N's collected
    artifact URLs become N+1's ``candidate_urls``).
    """

    id: str
    links: list[Question]


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
    Delegates to ``_run_legacy_link``, discarding the collected URLs.
    """
    metrics, _, _ = _run_legacy_link(
        question, settings=settings, session_factory=session_factory, runner=runner
    )
    return metrics


def _run_legacy_link(
    question: Question,
    *,
    settings: Settings,
    session_factory,
    runner=None,
    extra_context: dict | None = None,
) -> tuple[RunMetrics, list[str], str | None]:
    """Run one legacy question; also return its artifact URLs + summary.

    ``collected_urls`` = the run's persisted AgentArtifact source_urls
    (evidence-bound tool output only); ``summary`` = the run's
    AgentRun.final_summary.  Chain links hand both to the next link (URLs as
    its candidate set, summary quoted in the chain_context note).
    ``extra_context`` merges over the question's own context (candidate_urls
    inherited from a previous chain link).  Assembly/seam identical to
    ``run_legacy_question``, which delegates here.
    """
    if runner is None:

        def default_runner(question, settings, session_factory):
            if settings is not None:
                # mirror production runtime assembly (main.py lifespan): the
                # requests fast path falls back to a headless-Chromium render
                # on empty SPA/login shells, per the settings flag
                from backend.app.services.career_skills import job_discovery as jd_skill

                jd_skill.enable_playwright_fallback(
                    settings.job_discovery_playwright_fallback_enabled
                )
            gateway = build_agent_model_gateway(settings)
            tools = build_career_tool_registry()
            runtime = AgentRuntime(
                planner=PlannerAgent(gateway=gateway, tools=tools),
                executor=ExecutorAgent(gateway=gateway, tools=tools),
                verifier=VerifierAgent(gateway=gateway, tools=tools),
                agent_version="pev-1",
            )
            task = AgentTaskRequest(
                goal=question.goal,
                allowed_skills=question.allowed_skills,
                context={**(question.context or {}), **(extra_context or {})},
            )
            # plain session + explicit commit, not session_factory.begin():
            # the runtime commits/ends its own transaction internally, which
            # trips begin()'s exit with "closed transaction inside context
            # manager"; the commit here makes the run visible to the fresh
            # counter session below (the eval_runner live shape + commit)
            db = session_factory()
            try:
                result = runtime.run(db, user_id="eval-user", task=task)
                db.commit()
            finally:
                db.close()
            return result

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
        urls = db.scalars(
            select(AgentArtifact.source_url).where(
                AgentArtifact.run_id == result.run_id
            )
        ).all()
        summary = db.scalar(
            select(AgentRun.final_summary).where(AgentRun.id == result.run_id)
        )
    return (
        RunMetrics(
            status=result.status.value,
            steps=steps,
            turns=turns,
            tool_calls=tool_calls,
            replans=replans,
            wall_clock_s=round(elapsed, 2),
            error_code=result.error_code,
        ),
        [url for url in urls if url],
        summary,
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
    Delegates to ``_run_deepagents_link``, discarding the collected URLs.
    """
    metrics, _, _ = _run_deepagents_link(
        question, settings=settings, run_id=run_id, harness=harness
    )
    return metrics


def _run_deepagents_link(
    question: Question,
    *,
    settings: Settings,
    run_id: str,
    harness=None,
    extra_context: dict | None = None,
) -> tuple[RunMetrics, list[str], str | None]:
    """Run one deepagents question; also return its evidence URLs + summary.

    ``collected_urls`` = the run's in-memory evidence-store ``source_url``
    entries (only tool-produced evidence is stored; the compare runner wires
    no session_factory so nothing flushes to MySQL); ``summary`` = the
    graph's ``final_summary``.  Chain links hand both to the next link (URLs
    as its candidate set, summary quoted in the chain_context note).
    ``extra_context`` merges over the question's own context (candidate_urls
    inherited from a previous chain link).  Assembly/seam identical to
    ``run_deepagents_question``, which delegates here.

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
            # mirror the legacy gateway's provider wiring (model_gateway.py
            # build_agent_model_gateway): without an explicit base_url the
            # OpenAI-compatible client defaults to api.openai.com and
            # rejects the DeepSeek key with 401; get_api_key/get_base_url
            # resolve the same .env provider values as the legacy leg
            from backend.app.services.agent_runtime.model_gateway import (
                AgentModelGatewayConfigError,
            )
            from backend.app.services.agent_runtime.provider_config import (
                get_api_key,
                get_base_url,
            )

            api_key = get_api_key()
            if not api_key:
                raise AgentModelGatewayConfigError("missing_api_key")
            base_url = get_base_url()
            kwargs: dict[str, object] = {
                "model": settings.agent_harness_model,
                "temperature": 0,
                "request_timeout": 120,
                "max_retries": 2,
                "api_key": api_key,
                "base_url": base_url,
                "max_tokens": 4096 if role == "planner" else 2048,
            }
            if "deepseek" in base_url.lower() and settings.agent_harness_model.startswith(
                "deepseek-v4"
            ):
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            return ChatOpenAI(**kwargs)

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
        context={**(question.context or {}), **(extra_context or {})},
    )
    started = time.monotonic()
    final = harness.run(request, run_id=run_id)
    elapsed = time.monotonic() - started
    budgets = DeepAgentsBudgets.from_dict(final["budget"])
    plan_json = final.get("plan_json") or {}
    steps = len(plan_json.get("steps", [])) if isinstance(plan_json, dict) else 0
    urls = [
        item["source_url"]
        for item in final.get("evidence_store") or []
        if isinstance(item.get("source_url"), str)
    ]
    return (
        RunMetrics(
            status=final["run_status"] or "unknown",
            steps=steps,
            turns=budgets.turns_used,
            tool_calls=budgets.tool_calls_used,
            replans=budgets.replans_used,
            wall_clock_s=round(elapsed, 2),
            error_code=final.get("error_code"),
        ),
        urls,
        final.get("final_summary"),
    )


def _chain_metrics(metrics: list[RunMetrics]) -> RunMetrics | None:
    """Aggregate a chain leg's executed links into one chain-level metric.

    Status/error_code come from the LAST executed link (eval_runner chain
    semantics: the chain result reports the last link's outcome); the other
    counters sum across links.
    """
    if not metrics:
        return None
    last = metrics[-1]
    return RunMetrics(
        status=last.status,
        steps=sum(m.steps for m in metrics),
        turns=sum(m.turns for m in metrics),
        tool_calls=sum(m.tool_calls for m in metrics),
        replans=sum(m.replans for m in metrics),
        wall_clock_s=round(sum(m.wall_clock_s for m in metrics), 2),
        error_code=last.error_code,
    )


def _chain_extra(records: list[dict]) -> dict | None:
    """Build the extra context a chain link inherits from its predecessor.

    Mirrors eval_runner.run_chain: the previous link's collected URLs become
    ``candidate_urls``, plus a ``chain_context`` note quoting the previous
    summary — the chain answers with real evidence instead of a fresh
    session pretending earlier steps happened (the next link is a new
    session with no persisted evidence, so it must re-capture the candidates
    itself).
    """
    if not records:
        return None
    prev = records[-1]
    extra: dict | None = None
    prev_urls = prev["urls"]
    if prev_urls:
        extra = {"candidate_urls": prev_urls}
    summary = (prev.get("summary") or "").strip()
    if summary:
        note = (
            f"上一环节（{prev['id']}）已完成岗位收集，但本环节是全新会话，"
            f"上一环节的证据工件在当前会话中不存在，必须基于 "
            f"candidate_urls 中的 URL 重新抓取岗位页面获取 JD 证据后，"
            f"才能进行本环节的任务。上一环节成果参考：{summary[:200]}"
        )
        extra = {**(extra or {}), "chain_context": note}
    return extra


def _run_chain_leg(
    links: list[Question],
    *,
    run_link,
) -> dict:
    """Run one runtime's side of a chain (eval_runner contract).

    ``run_link(link, *, extra_context) -> (RunMetrics, urls, summary)`` is
    the leg's per-link runner (``_run_deepagents_link`` / ``_run_legacy_link``
    bound with the leg's own settings/session wiring).  Link N+1 only starts
    when N succeeded; N's collected URLs + summary become N+1's
    ``candidate_urls`` + ``chain_context`` note.  Returns the executed link
    records and their metrics (stops at the first non-succeeded link).
    """
    records: list[dict] = []
    metrics: list[RunMetrics] = []
    for link in links:
        extra_context = _chain_extra(records)
        link_metrics, urls, summary = run_link(
            link, extra_context=extra_context
        )
        metrics.append(link_metrics)
        records.append(
            {
                "id": link.id,
                "goal": link.goal,
                "metrics": asdict(link_metrics),
                "url_count": len(urls),
                "urls": urls,
                "summary": summary or "",
            }
        )
        if link_metrics.status != "succeeded":
            break
    return {"links": records, "metrics": metrics}


def run_chain_comparison(
    chain: ChainQuestion, *, settings: Settings, session_factory
) -> dict:
    """Run a chained question on both runtimes; return its report entry.

    Each leg follows the eval_runner chain contract independently (link N+1
    only when N succeeded on that leg, N's collected URLs become N+1's
    candidates).  Chain-level metrics aggregate over the executed links; a
    leg exception is recorded in ``error`` and the surviving leg still
    contributes its metrics.
    """
    errors: list[str] = []

    def deepagents_link(link, *, extra_context):
        return _run_deepagents_link(
            link,
            settings=settings,
            run_id=f"eval-{link.id}",
            extra_context=extra_context,
        )

    def legacy_link(link, *, extra_context):
        return _run_legacy_link(
            link,
            settings=settings,
            session_factory=session_factory,
            extra_context=extra_context,
        )

    try:
        deepagents_leg = _run_chain_leg(chain.links, run_link=deepagents_link)
    except Exception as exc:  # noqa: BLE001 - leg isolation per question
        errors.append(f"deepagents: {type(exc).__name__}: {exc}")
        deepagents_leg = {"links": [], "metrics": []}
    try:
        legacy_leg = _run_chain_leg(chain.links, run_link=legacy_link)
    except Exception as exc:  # noqa: BLE001 - leg isolation per question
        errors.append(f"legacy: {type(exc).__name__}: {exc}")
        legacy_leg = {"links": [], "metrics": []}
    d_links = deepagents_leg["links"]
    l_links = legacy_leg["links"]
    links = [
        {
            "id": chain.links[index].id,
            "goal": chain.links[index].goal,
            "deepagents": d_links[index]["metrics"] if index < len(d_links) else None,
            "deepagents_urls": d_links[index]["url_count"] if index < len(d_links) else 0,
            "legacy": l_links[index]["metrics"] if index < len(l_links) else None,
            "legacy_urls": l_links[index]["url_count"] if index < len(l_links) else 0,
        }
        for index in range(max(len(d_links), len(l_links)))
    ]
    entry: dict = {
        "id": chain.id,
        "type": "chain",
        "chain_length": len(chain.links),
        "goal": chain.links[0].goal if chain.links else "",
        "links": links,
    }
    deepagents_chain = _chain_metrics(deepagents_leg["metrics"])
    legacy_chain = _chain_metrics(legacy_leg["metrics"])
    if deepagents_chain is not None:
        entry["deepagents"] = asdict(deepagents_chain)
    if legacy_chain is not None:
        entry["legacy"] = asdict(legacy_chain)
    if errors:
        entry["error"] = "; ".join(errors)
    return entry


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
    questions: list[Question | ChainQuestion],
    *,
    out_dir: Path,
    settings: Settings,
    session_factory,
) -> dict:
    """Run both runtimes over the questions and write report.json + report.md.

    Plain questions run per-question; chain docs run via
    ``run_chain_comparison`` (per-link runs, link N+1 only when N succeeded)
    and count as one entry per leg with chain-level aggregated metrics.

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
    for item in questions:
        if isinstance(item, ChainQuestion):
            entry = run_chain_comparison(
                item, settings=settings, session_factory=session_factory
            )
            for leg_name, bucket in (
                ("deepagents", deepagents_metrics),
                ("legacy", legacy_metrics),
            ):
                metrics_dict = entry.get(leg_name)
                if metrics_dict is not None:
                    bucket.append(RunMetrics(**metrics_dict))
            per_question.append(entry)
            print(
                f"DONE {entry['id']}: chain deepagents={entry.get('deepagents', {}).get('status', 'leg-error')} "
                f"legacy={entry.get('legacy', {}).get('status', 'leg-error')}",
                flush=True,
            )
            continue
        entry: dict = {"id": item.id, "goal": item.goal}
        errors: list[str] = []
        try:
            deepagents_metrics.append(
                run_deepagents_question(
                    item, settings=settings, run_id=f"eval-{item.id}"
                )
            )
            entry["deepagents"] = asdict(deepagents_metrics[-1])
        except Exception as exc:
            errors.append(f"deepagents: {type(exc).__name__}: {exc}")
        try:
            legacy_metrics.append(
                run_legacy_question(
                    item, settings=settings, session_factory=session_factory
                )
            )
            entry["legacy"] = asdict(legacy_metrics[-1])
        except Exception as exc:
            errors.append(f"legacy: {type(exc).__name__}: {exc}")
        if errors:
            entry["error"] = "; ".join(errors)
        per_question.append(entry)
        print(
            f"DONE {entry['id']}: deepagents={entry.get('deepagents', {}).get('status', 'leg-error')} "
            f"legacy={entry.get('legacy', {}).get('status', 'leg-error')}",
            flush=True,
        )
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


def _seed_urls(qid: str) -> list[str]:
    """Question-seed URLs from the eval_runner seed bank (live eval only).

    The bank is probe-verified on the legacy side; seeding both legs from it
    keeps the comparison apples-to-apples with the archived baseline runs.
    Lazy import so unit tests that only exercise loading stay light.
    """
    from tests.question.eval_runner import SEED_URLS  # live eval only

    return list(SEED_URLS.get(qid, ([], ""))[0])


def _question_from_doc(doc: dict) -> Question:
    """One question doc -> Question (context carries profile/meta + seeds)."""
    meta = doc.get("meta") or {}
    context: dict = {"profile": doc.get("profile"), "meta": meta}
    urls = _seed_urls(doc["id"])
    if urls:
        context["candidate_urls"] = urls
    return Question(
        id=doc["id"],
        goal=doc["question"],
        allowed_skills=meta.get("skills") or [],
        context=context,
    )


def _chain_from_doc(cid: str, doc: dict) -> ChainQuestion | None:
    """A chain doc -> ChainQuestion; None when no link has usable skills.

    Each link becomes its own Question under ``C###-L<n>`` (the link's own
    ``meta.skills`` / ``profile``; link 1 seeds from the seed bank, links 2+
    inherit candidate_urls at run time).
    """
    links = doc.get("chain")
    if not isinstance(links, list) or not links:
        return None
    chain_links: list[Question] = []
    for index, link_doc in enumerate(links, start=1):
        if not ((link_doc.get("meta") or {}).get("skills")):
            return None
        chain_links.append(_question_from_doc({**link_doc, "id": f"{cid}-L{index}"}))
    return ChainQuestion(id=cid, links=chain_links)


def _load_questions(ids: list[str]) -> list[Question | ChainQuestion]:
    """Load Q/R/C docs, root-first + redesign fallback; chains expand.

    ``ids`` resolve root-first (``tests/question/{id}.json``) then fall back
    to ``tests/question/redesign/{id}.json``; chain docs expand into one
    ChainQuestion (each link its own Question under ``C###-L<n>``, running
    per the eval_runner chain contract).  An empty ``ids`` defaults to the
    root-directory glob only (backward compatible — the root set is
    Q001-Q151, no chains).
    """
    question_dir = Path(__file__).resolve().parents[5] / "tests" / "question"
    redesign_dir = question_dir / "redesign"
    all_ids = sorted(path.stem for path in question_dir.glob("*.json"))
    questions: list[Question | ChainQuestion] = []
    for qid in ids or all_ids:
        doc_path = question_dir / f"{qid}.json"
        if not doc_path.exists():
            doc_path = redesign_dir / f"{qid}.json"
        if not doc_path.exists():
            print(f"SKIP {qid}: {doc_path.name} missing")
            continue
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        if "chain" in doc:
            chain = _chain_from_doc(qid, doc)
            if chain is None:
                print(f"SKIP {qid}: chain with no usable links")
                continue
            questions.append(chain)
            continue
        if not ((doc.get("meta") or {}).get("skills")):
            print(f"SKIP {qid}: needs meta.skills")
            continue
        questions.append(_question_from_doc(doc))
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
