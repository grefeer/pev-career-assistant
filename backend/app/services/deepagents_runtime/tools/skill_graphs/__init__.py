"""Skill workflow subgraphs wrapped as @tools for the Executor."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.deepagents_runtime.budgets import DeepAgentsBudgets
from backend.app.services.deepagents_runtime.middleware import (
    active_budgets as _context_budgets,
    active_tracker as _context_tracker,
)
from backend.app.services.deepagents_runtime.tools.adapters import (
    DuplicateCallTracker,
    build_skill_tools,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
    resolve_run_output_dirs,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    run_skill_script,
)

_workflow_thread: ContextVar[str | None] = ContextVar(
    "deepagents_workflow_thread", default=None
)


@contextmanager
def workflow_thread_id(thread: str):
    """Bind the workflow subgraph thread for one executor invocation."""
    token = _workflow_thread.set(thread)
    try:
        yield
    finally:
        _workflow_thread.reset(token)


class _JsonPayload(BaseModel):
    payload: str


def build_job_discovery_tool(
    *,
    fetch_fn=None,
    script_runner=None,
    extract_fn=None,
    checkpointer: Any = None,
    settings=None,
    budgets: DeepAgentsBudgets | None = None,
    tracker: DuplicateCallTracker | None = None,
    candidates_dir=None,
    state_dir=None,
    evidence_dir=None,
    wechat_out_dir=None,
) -> StructuredTool:
    """Wrap the compiled job-discovery workflow as a single @tool.

    Thread = ``f"{run_id}:{step_index}:workflow"`` (bound by the harness via
    ``workflow_thread_id``), so a mid-crawl crash resumes from the last URL
    instead of re-fetching.  Returns a ToolObservation (the harness's
    ``_project_tool_observations`` validates every ToolMessage against that
    schema — a bare dict would be silently dropped as invalid).  ``settings``
    gates the optional LLM extraction (spec §4.3); ``budgets``/``tracker``
    enforce the spec §5 hard ceilings + duplicate-call dedup on the workflow
    path (when None they fall back to the harness-bound context vars, so the
    tool_factory seam needs no signature change; outside a harness invocation
    both stay None and the tool is unguarded, as before).  ``candidates_dir``
    points the per-page write/dedup phases at a run-scoped output directory
    (default: ``<state_dir>/output/candidates`` — derived from the resolved
    state dir so merged_final.json accumulates exactly where the next run's
    prior store reads it back); ``state_dir`` is the stable incremental
    store root (Task 10); ``evidence_dir`` pins the browse page-file root +
    coverage manifest (Task 11, must stay under ``output/evidence`` for the
    gate's containment check); ``wechat_out_dir`` pins the Task 9 OCR
    slice's working dir.  The input payload accepts a JSON array
    (single-shot) or an object ``{"urls": [...], "prior_metadata": {...}}``
    (incremental: state check/mark + merged accumulation;
    ``prior_metadata`` = ``file_id`` / ``sheet_id`` / ``update_time``).
    """

    graph = build_job_discovery_graph(
        fetch_fn=fetch_fn,
        script_runner=script_runner,
        extract_fn=extract_fn,
        settings=settings,
        candidates_dir=candidates_dir,
        state_dir=state_dir,
        evidence_dir=evidence_dir,
        wechat_out_dir=wechat_out_dir,
    ).compile(checkpointer=checkpointer)

    def _observe(status: str, **kwargs: Any) -> str:
        observation = ToolObservation(
            tool_name="run-job-discovery-workflow", status=status, **kwargs
        )
        return json.dumps(observation.model_dump(exclude_none=True), ensure_ascii=False)

    def run(payload: str) -> str:
        # same guard order as the adapters (adapters.py _handler): budget
        # first (hard ceiling), then payload parse, then duplicate dedup —
        # one agent tool call = one budget unit
        run_budgets = budgets or _context_budgets()
        if run_budgets is not None and not run_budgets.try_consume_tool():
            return _observe("failed", error_code="tool_budget_exhausted")
        run_tracker = tracker or _context_tracker()
        try:
            value = json.loads(payload)
            if isinstance(value, dict) and "payload" in value:
                # string-input path: StructuredTool passes the raw input
                # string positionally, so the {"payload": ...} wrapper JSON
                # arrives here instead of the decoded array.  The args_schema
                # (ToolNode) already strips that wrapper, so a dict here is
                # usually the incremental object {"urls": [...],
                # "prior_metadata": {...}} — never index "payload" blindly.
                value = json.loads(value["payload"])
            if isinstance(value, dict):
                # incremental input object: {"urls": [...], "prior_metadata": {...}}
                invoke: dict[str, Any] = {"urls": value.get("urls", [])}
                if value.get("prior_metadata") is not None:
                    invoke["prior_metadata"] = value["prior_metadata"]
            else:
                invoke = {"urls": value}
            if run_tracker is not None and run_tracker.is_duplicate(
                "run-job-discovery-workflow", invoke
            ):
                return _observe("failed", error_code="duplicate_tool_call")
            thread = _workflow_thread.get()
            config = {"configurable": {"thread_id": thread}} if thread else {}
            final = graph.invoke(invoke, config)
        except Exception as exc:  # noqa: BLE001 - fold into observation, never raise
            return _observe(
                "failed", error_code=f"workflow_error: {type(exc).__name__}"
            )
        per_url = final.get("per_url_results", [])
        # Task 11 output contract: the observation-level status stays
        # succeeded/failed (the ToolObservation schema rejects anything
        # else); the workflow status lives inside the output dict —
        # "blocked" when every URL was blocked (per-url error_code=blocked),
        # "succeeded" otherwise (per-url failures are recorded inside pages).
        # The observation-level error_code mirrors the all-blocked verdict so
        # the harness's _is_non_progress classifies the crawl as no-progress
        # (I3) — the blocked verdict stays nested in output either way.
        status = (
            "blocked"
            if per_url and all(r.get("error_code") == "blocked" for r in per_url)
            else "succeeded"
        )
        return _observe(
            "succeeded",
            error_code="blocked" if status == "blocked" else None,
            output={
                "status": status,
                "pages": final.get("pages", []),
                # Task 11: the merged/persisted candidates snapshot the
                # coverage gate read ("" when neither node produced one)
                "candidates_file": final.get("candidates_file") or "",
                # dedup node output (U10 + review I1-1): merged_count = the
                # deduplicate script's output_count when dedup ran; when the
                # batch fast-path skipped it (no per-page files, the node
                # default 0), len(candidates) is the honest real count —
                # never 0 alongside a non-empty candidates channel
                "merged_count": final.get("merged_count")
                or len(final.get("candidates", [])),
                # Task 11: observed browse terminal markers (never
                # synthesized), exactly what the coverage manifest recorded
                "terminal_evidence": final.get("terminal_evidence", []),
                "coverage": final.get("coverage", {"verified": False}),
                "per_url_results": per_url,
                "candidates": final.get("candidates", []),
                "dedup_stats": final.get("dedup_stats", {}),
                # Task 10: comparison-key map from the normalize node (keys
                # only — stored titles are never altered)
                "normalize_keys": final.get("normalize_keys", {}),
                # the graph's error channel surfaces here (empty when none)
                "errors": [final["error"]] if final.get("error") else [],
            },
        )

    return StructuredTool.from_function(
        func=run,
        name="run-job-discovery-workflow",
        description=(
            "按 SKILL.md 六阶段工作流批量处理招聘 URL：抓取页面、正则提取 JD"
            "（低置信才用 LLM）、校验、去重、覆盖门控。输入 JSON 数组"
            "（用户给出的官方招聘 URL），或增量对象 {\"urls\": [...], "
            "\"prior_metadata\": {file_id, sheet_id, update_time}}；返回 "
            "status + pages + candidates_file + merged_count + "
            "terminal_evidence + coverage + per_url_results + candidates + "
            "errors。"
        ),
        args_schema=_JsonPayload,
    )


def build_job_discovery_tools(
    skill_name: str,
    *,
    budgets=None,
    tracker=None,
    run_id: str | None = None,
    script_runner=None,
    checkpointer: Any = None,
    settings=None,
) -> list[Any]:
    """Harness tool_factory: job-discovery -> the workflow tool, else the
    existing career-skills tools.

    Used by ``compare_runner.run_deepagents_question`` (Task 11 wiring).
    For ``job-discovery`` returns a single ``build_job_discovery_tool`` with
    production defaults (real browse orchestration + real per-page
    extraction) and — when ``run_id`` is given — run-scoped output dirs so
    eval runs never touch the shared defaults (controller D1).
    ``script_runner`` is a hermeticity seam for tests (Task 11 review M1-2):
    default ``None`` resolves to the real ``run_skill_script``
    (byte-identical production shape); tests pass a fake so no skill script
    ever executes as a subprocess.  Final-review wiring (I1/I2/I4):
    ``settings`` threads the spec §4.3 LLM-extraction gate, ``budgets`` /
    ``tracker`` the spec §5 hard ceiling + duplicate-call dedup, and
    ``checkpointer`` the harness's RedisSaver so a mid-crawl crash resumes
    from the last URL — all default to None (in-memory, unguarded) so
    existing callers keep byte-identical behavior.  Other skills fall back
    to ``build_skill_tools`` (budgets/tracker forwarded as-is).
    """
    if skill_name == "job-discovery":
        dirs = resolve_run_output_dirs(run_id) if run_id else {}
        return [
            build_job_discovery_tool(
                fetch_fn=None,
                script_runner=script_runner or run_skill_script,
                extract_fn=None,
                budgets=budgets,
                tracker=tracker,
                checkpointer=checkpointer,
                settings=settings,
                **dirs,
            )
        ]
    return build_skill_tools(skill_name=skill_name, budgets=budgets, tracker=tracker)
