"""Skill workflow subgraphs wrapped as @tools for the Executor."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
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
    candidates_dir=None,
    state_dir=None,
) -> StructuredTool:
    """Wrap the compiled job-discovery workflow as a single @tool.

    Thread = ``f"{run_id}:{step_index}:workflow"`` (bound by the harness via
    ``workflow_thread_id``), so a mid-crawl crash resumes from the last URL
    instead of re-fetching.  Returns a ToolObservation (the harness's
    ``_project_tool_observations`` validates every ToolMessage against that
    schema — a bare dict would be silently dropped as invalid).  ``settings``
    gates the optional LLM extraction (spec §4.3); ``candidates_dir`` points
    the per-page write/dedup phases at a run-scoped output directory;
    ``state_dir`` is the stable incremental store root (Task 10).  The input
    payload accepts a JSON array (single-shot) or an object
    ``{"urls": [...], "prior_metadata": {...}}`` (incremental: state
    check/mark + merged accumulation; ``prior_metadata`` = ``file_id`` /
    ``sheet_id`` / ``update_time``).
    """

    graph = build_job_discovery_graph(
        fetch_fn=fetch_fn,
        script_runner=script_runner,
        extract_fn=extract_fn,
        settings=settings,
        candidates_dir=candidates_dir,
        state_dir=state_dir,
    ).compile(checkpointer=checkpointer)

    def _observe(status: str, **kwargs: Any) -> str:
        observation = ToolObservation(
            tool_name="run-job-discovery-workflow", status=status, **kwargs
        )
        return json.dumps(observation.model_dump(exclude_none=True), ensure_ascii=False)

    def run(payload: str) -> str:
        try:
            value = json.loads(payload)
            if isinstance(value, dict):
                # string-input path: StructuredTool passes the raw input
                # string positionally, so the {"payload": ...} wrapper JSON
                # arrives here instead of the decoded array
                value = json.loads(value["payload"])
            if isinstance(value, dict):
                # incremental input object: {"urls": [...], "prior_metadata": {...}}
                invoke: dict[str, Any] = {"urls": value.get("urls", [])}
                if value.get("prior_metadata") is not None:
                    invoke["prior_metadata"] = value["prior_metadata"]
            else:
                invoke = {"urls": value}
            thread = _workflow_thread.get()
            config = {"configurable": {"thread_id": thread}} if thread else {}
            final = graph.invoke(invoke, config)
        except Exception as exc:  # noqa: BLE001 - fold into observation, never raise
            return _observe(
                "failed", error_code=f"workflow_error: {type(exc).__name__}"
            )
        return _observe(
            "succeeded",
            output={
                "per_url_results": final.get("per_url_results", []),
                "candidates": final.get("candidates", []),
                "coverage": final.get("coverage", {"verified": False}),
                # dedup node output (U10): merged_count = the deduplicate
                # script's output_count, dedup_stats = its stats dict; both
                # default to the no-merge sentinels when the node skipped
                # (batch fast-path wrote no per-page files)
                "merged_count": final.get("merged_count", 0),
                "dedup_stats": final.get("dedup_stats", {}),
                # Task 10: comparison-key map from the normalize node (keys
                # only — stored titles are never altered)
                "normalize_keys": final.get("normalize_keys", {}),
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
            "per_url_results + candidates + coverage + normalize_keys。"
        ),
        args_schema=_JsonPayload,
    )
